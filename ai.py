"""AI 相关能力：视觉分析表情包 + 根据聊天情境挑选表情包。

两条路径：

1. **主动工具（AI 自主调用）**
   注册 ``send_emoji`` 工具给主对话模型，让它自己决定什么时候发、发哪张。
2. **自动模式（插件自己判断）**
   在收到消息时，插件自己用 LLM 判断「该不该发表情包」以及「发哪张」。

两者共用 ``EmojiPicker``，它负责：
- 用视觉模型给图库打标签（后台批量进行）
- 按语义检索候选表情包
- 调用 LLM 从候选里挑一张
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:  # 作为插件包加载
    from .storage import Asset, Storage
    from .vision import VisionTag, build_vision_messages, parse_vision_json
except ImportError:  # 独立运行测试时
    from storage import Asset, Storage
    from vision import VisionTag, build_vision_messages, parse_vision_json


# 挑选表情包时给 LLM 的提示词
PICK_PROMPT = """你是一个表情包助手。根据聊天情境，从候选表情包里挑出最合适的一张。

聊天情境：
{context}

候选表情包：
{candidates}

挑选规则：
1. 优先匹配情绪和场景都贴合的
2. 如果有多个合适的，随机选一个增加趣味性
3. 如果没有一个合适，就返回空 JSON

只输出 JSON，格式：{{"id": 123, "reason": "选它的原因（15字内）"}}
如果没有合适的，输出：{{}}"""

# 判断「该不该发表情包」的提示词
DECIDE_PROMPT = """判断在这个聊天情境下，机器人发送一张表情包是否合适。

聊天情境：
{context}

规则：
1. 表情包用来表达情绪、活跃气氛，不是每句话都要配
2. 适合：情绪强烈的反应（大笑、震惊、无语、感动、祝贺、嘲讽）
3. 不适合：严肃话题、工作汇报、技术讨论、需要准确信息的场合
4. 如果只是简单的寒暄或信息传递，不适合发表情包

只输出 JSON：{{"send": true/false, "emotion": "期望的情绪（英文）", "reason": "判断理由（20字内）"}}
如果没有合适表情包，send 为 false。"""


@dataclass
class PickResult:
    """挑选结果。"""

    asset: Asset | None = None
    reason: str = ""
    should_send: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset.id if self.asset else None,
            "filename": self.asset.filename if self.asset else "",
            "reason": self.reason,
            "should_send": self.should_send,
        }


class LLMClient:
    """极简的 OpenAI 兼容客户端。

    不走 AstrBot 的 provider 管理器，因为：
    - 视觉分析要用**特定**的视觉模型（主对话模型可能没有视觉能力）
    - 插件需要在后台批量跑视觉分析，用 provider 会污染对话上下文

    插件同时支持两种配置来源：
    - 插件自己的 ``vision_api_base`` / ``vision_api_key`` / ``vision_model``
    - 复用 AstrBot 已有的 provider source（用 ``provider_source_id`` 指定）
    """

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        *,
        timeout: int = 60,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> str:
        """发一次对话请求，返回纯文本内容。"""
        import aiohttp

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.api_base}/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                text = await response.text()
                if response.status != 200:
                    raise RuntimeError(
                        f"LLM 请求失败 HTTP {response.status}: {text[:200]}"
                    )
        data = json.loads(text)
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 256,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        """发一次对话请求并解析 JSON 返回值。"""
        raw = await self.chat(
            messages, max_tokens=max_tokens, temperature=temperature
        )
        return _extract_json(raw)


def _extract_json(raw: str) -> dict[str, Any]:
    """从模型输出里提取 JSON 对象。"""
    if not raw:
        return {}
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start < 0:
        return {}
    depth = 0
    end = -1
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end < 0:
        return {}
    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _is_analyzable_size(path: Path) -> bool:
    """判断图片尺寸是否适合做视觉分析。

    QQ 系统表情通常只有 20~24 像素，视觉模型会直接拒绝
    （``image_invalid``），反复重试纯属浪费。

    尺寸能读出来时只看尺寸；读不出来（比如 AVIF 这类格式）
    才退回用文件大小粗略判断。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False

    width, height = Storage.read_image_size(data)
    if width and height:
        if min(width, height) < 64:
            return False
        if max(width, height) > 4096:
            return False
        return True

    # 读不出尺寸时按文件大小粗略判断
    return len(data) >= 1024


def describe_candidates(assets: list[Asset]) -> str:
    """把候选素材拼成给 LLM 看的清单。"""
    lines = []
    for asset in assets:
        tags = []
        if asset.emotion:
            tags.append(f"情绪={asset.emotion}")
        if asset.scene:
            tags.append(f"场景={asset.scene}")
        if asset.caption:
            tags.append(f"描述={asset.caption}")
        suffix = f" [{'，'.join(tags)}]" if tags else ""
        lines.append(f"- id={asset.id}{suffix} ({asset.filename})")
    return "\n".join(lines)


class EmojiPicker:
    """表情包挑选器。"""

    def __init__(
        self,
        storage: Storage,
        client_factory: Callable[[], LLMClient | None],
    ) -> None:
        self.storage = storage
        self.client_factory = client_factory
        self._analyzing = False
        self._analyzed_count = 0

    # ------------------------------------------------------------------
    # 视觉分析
    # ------------------------------------------------------------------
    async def analyze_pending(self, batch_size: int = 5) -> int:
        """分析待处理的素材，返回本次分析的数量。

        一次只处理一批，避免长时间占用事件循环。
        """
        if self._analyzing:
            return 0
        pending = self.storage.pending_analysis(batch_size)
        if not pending:
            return 0

        client = self.client_factory()
        if client is None:
            return 0

        self._analyzing = True
        count = 0
        try:
            for asset in pending:
                try:
                    path = self.storage.media_path(asset.stored_name)
                except ValueError:
                    continue
                if not path.is_file():
                    await self.storage.mark_analysis_failed(asset.id)
                    continue
                # 尺寸过小的图（QQ 系统表情）视觉模型不认，
                # 直接标成「系统表情」，不浪费重试
                if not _is_analyzable_size(path):
                    await self.storage.set_vision_tag(
                        asset.id,
                        emotion="system",
                        scene="QQ系统表情",
                        caption="QQ 自带系统表情",
                        keywords=["系统表情", "QQ"],
                    )
                    count += 1
                    continue
                try:
                    tag = await self.analyze_one(client, path, asset.mime)
                except Exception:  # noqa: BLE001 - 单张失败不影响整批
                    await self.storage.mark_analysis_failed(asset.id)
                    continue
                if tag.is_empty():
                    await self.storage.mark_analysis_failed(asset.id)
                    continue
                await self.storage.set_vision_tag(
                    asset.id,
                    emotion=tag.emotion,
                    scene=tag.scene,
                    caption=tag.caption,
                    keywords=tag.keywords,
                )
                count += 1
                self._analyzed_count += 1
        finally:
            self._analyzing = False
        return count

    async def analyze_one(
        self, client: LLMClient, path: Path, mime: str
    ) -> VisionTag:
        """分析单张图片。

        太小或太大的图直接放弃：QQ 系统表情只有 20~24 像素，
        视觉模型会返回 ``image_invalid``，反复重试没有意义。
        """
        if not _is_analyzable_size(path):
            return VisionTag()
        messages = build_vision_messages(str(path), mime)
        raw = await client.chat(messages, max_tokens=200, temperature=0.2)
        return parse_vision_json(raw)

    # ------------------------------------------------------------------
    # 挑选
    # ------------------------------------------------------------------
    def find_candidates(self, context: str, limit: int = 8) -> list[Asset]:
        """按语义检索候选素材。

        检索不到时退回到全部已分析素材（让 LLM 自己挑）。
        """
        candidates = self.storage.search_by_semantics(context, limit)
        if candidates:
            return candidates
        return self.storage.list_assets_all_analyzed(limit)

    async def pick(
        self,
        context: str,
        *,
        limit: int = 8,
        candidates: list[Asset] | None = None,
    ) -> PickResult:
        """根据聊天情境挑一张表情包。"""
        pool = (
            candidates if candidates is not None else self.find_candidates(context, limit)
        )
        if not pool:
            return PickResult(
                asset=None, reason="图库里还没有可用素材", should_send=False
            )

        client = self.client_factory()
        if client is None:
            # 没有配置 LLM 时，退化为随机挑一张
            chosen = random.choice(pool)
            return PickResult(asset=chosen, reason="未配置 AI，随机挑选")

        messages = [
            {
                "role": "user",
                "content": PICK_PROMPT.format(
                    context=context,
                    candidates=describe_candidates(pool),
                ),
            }
        ]
        try:
            data = await client.chat_json(messages, max_tokens=120, temperature=0.8)
        except Exception:  # noqa: BLE001 - LLM 失败时退化处理
            chosen = random.choice(pool)
            return PickResult(asset=chosen, reason="AI 调用失败，随机挑选")

        asset_id = data.get("id")
        if asset_id is None:
            return PickResult(
                asset=None, reason="AI 没有选中任何素材", should_send=False
            )
        try:
            wanted = int(asset_id)
        except (TypeError, ValueError):
            return PickResult(
                asset=None, reason="AI 返回的素材 ID 非法", should_send=False
            )

        for asset in pool:
            if asset.id == wanted:
                return PickResult(
                    asset=asset,
                    reason=str(data.get("reason") or "")[:40],
                )
        return PickResult(
            asset=None, reason="AI 返回的素材不在候选中", should_send=False
        )

    async def decide(
        self,
        context: str,
        *,
        candidates: list[Asset] | None = None,
    ) -> PickResult:
        """让 LLM 判断该不该发表情包，并顺带选一张。"""
        pool = (
            candidates if candidates is not None else self.find_candidates(context)
        )
        if not pool:
            return PickResult(asset=None, reason="没有可用素材", should_send=False)

        client = self.client_factory()
        if client is None:
            return PickResult(asset=None, reason="未配置 AI", should_send=False)

        messages = [
            {
                "role": "user",
                "content": DECIDE_PROMPT.format(context=context),
            }
        ]
        try:
            data = await client.chat_json(messages, max_tokens=120, temperature=0.4)
        except Exception:  # noqa: BLE001
            return PickResult(asset=None, reason="AI 调用失败", should_send=False)

        if not data.get("send"):
            return PickResult(
                asset=None,
                reason=str(data.get("reason") or "不适合发表情包")[:40],
                should_send=False,
            )

        emotion = str(data.get("emotion") or "").strip()
        if emotion:
            matched = self.storage.list_by_emotion(emotion, 8)
            if matched:
                pool = matched
        picked = await self.pick(context, candidates=pool)
        if picked.asset is None:
            return PickResult(
                asset=None,
                reason=str(data.get("reason") or "未选中素材")[:40],
                should_send=False,
            )
        return PickResult(
            asset=picked.asset,
            reason=str(data.get("reason") or picked.reason)[:40],
            should_send=True,
        )
