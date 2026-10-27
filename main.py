"""AstrBot 表情包 / 图片采集插件。

按 QQ 号锁定采集指定用户在群聊 / 私聊中发送的图片与表情包，
并提供 Web 图库界面（AstrBot Plugin View）进行浏览、检索与管理。

主要能力
--------
1. **按 QQ 号锁定**：配置 ``target_qq`` 列表，只有这些用户发出的图片才会被采集。
2. **多来源采集**：
   - ``image`` —— 普通图片消息（jpg/png/gif/webp…）
   - ``face``  —— QQ 自带系统表情（通过 ``get_emoji`` 接口取图）
   - ``mface`` —— QQ 商城表情包 / 动画表情（通过 ``get_mface`` 接口取原图）
3. **Web 图库界面**：插件自带 View，可在 AstrBot WebUI 的插件页里浏览、搜索、筛选、删除。
4. **指令操作**：``/图库`` 查看统计、``/图库 搜索 关键词``、``/图库 清空`` 等。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import request

try:  # 作为插件包加载
    from .ai import EmojiPicker, LLMClient, PickResult
    from .pluginlogic import (
        DEFAULT_ENABLED,
        FACE_URL_TEMPLATE,
        MFACE_ID_RE,
        MFACE_URL_TEMPLATE,
        PLUGIN_NAME,
        PLUGIN_VERSION,
        SOURCE_FACE,
        SOURCE_IMAGE,
        SOURCE_MFACE,
        build_face_url,
        build_mface_url,
        clamp_int,
        derive_filename,
        is_target_sender,
        parse_target_qq,
    )
    from .storage import Asset, Query, Storage, write_manifest
except ImportError:  # 独立运行单元测试时
    from ai import EmojiPicker, LLMClient, PickResult
    from pluginlogic import (
        DEFAULT_ENABLED,
        FACE_URL_TEMPLATE,
        MFACE_ID_RE,
        MFACE_URL_TEMPLATE,
        PLUGIN_NAME,
        PLUGIN_VERSION,
        SOURCE_FACE,
        SOURCE_IMAGE,
        SOURCE_MFACE,
        build_face_url,
        build_mface_url,
        clamp_int,
        derive_filename,
        is_target_sender,
        parse_target_qq,
    )
    from storage import Asset, Query, Storage, write_manifest


def _plugin_data_dir() -> Path:
    """返回插件数据目录：AstrBot 约定为 ``data/plugin_data/<插件名>``。"""
    base = Path(os.getcwd())
    candidate = base / "data" / "plugin_data" / PLUGIN_NAME
    if candidate.parent.exists():
        return candidate
    # 兼容部分部署下 cwd 不是 AstrBot 根目录的情况
    fallback = Path("/AstrBot/data/plugin_data") / PLUGIN_NAME
    if fallback.parent.exists():
        return fallback
    return candidate


class EmojiHarvesterPlugin(Star):
    """表情包采集与图库插件。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.storage = Storage(_plugin_data_dir())
        self.picker = EmojiPicker(self.storage, self._make_llm_client)
        self._collect_lock = asyncio.Lock()
        self._registered = False
        self._self_uin = ""
        """机器人自己的 QQ 号，用来拼 QQ 表情包的图片地址。"""
        self._analysis_task: asyncio.Task | None = None
        self._auto_send_history: list[float] = []
        """最近自动发送表情包的时间戳，用于限流。"""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """插件初始化：注册 Web API 路由。

        路由 **要** 带插件名前缀：前端 bridge 拼出的完整地址是
        ``/api/v1/plugins/extensions/<插件名>/<endpoint>``，
        后端把整段（含插件名）当作 plugin_path 来匹配路由。

        对应地，前端调用 bridge 时传的 endpoint **不能**带插件名，
        否则会拼成 ``/extensions/<插件名>/<插件名>/<endpoint>`` 匹配不到。
        """
        if not self._registered:
            self.context.register_web_api(
                route=f"/{PLUGIN_NAME}/emoji-harvester/api/assets",
                view_handler=self.api_assets,
                methods=["GET"],
                desc="图库素材列表（支持分页与筛选）",
            )
            self.context.register_web_api(
                route=f"/{PLUGIN_NAME}/emoji-harvester/api/stats",
                view_handler=self.api_stats,
                methods=["GET"],
                desc="图库统计信息",
            )
            self.context.register_web_api(
                route=f"/{PLUGIN_NAME}/emoji-harvester/api/senders",
                view_handler=self.api_senders,
                methods=["GET"],
                desc="图库中的发送者列表",
            )
            self.context.register_web_api(
                route=f"/{PLUGIN_NAME}/emoji-harvester/api/asset/<asset_id>",
                view_handler=self.api_asset_file,
                methods=["GET"],
                desc="获取单个素材的图片文件",
            )
            self.context.register_web_api(
                route=f"/{PLUGIN_NAME}/emoji-harvester/api/delete",
                view_handler=self.api_delete,
                methods=["POST"],
                desc="删除指定素材",
            )
            self.context.register_web_api(
                route=f"/{PLUGIN_NAME}/emoji-harvester/api/clear",
                view_handler=self.api_clear,
                methods=["POST"],
                desc="清空图库",
            )
            self.context.register_web_api(
                route=f"/{PLUGIN_NAME}/emoji-harvester/api/export",
                view_handler=self.api_export,
                methods=["GET"],
                desc="导出素材清单 JSON",
            )
            self.context.register_web_api(
                route=f"/{PLUGIN_NAME}/emoji-harvester/api/config",
                view_handler=self.api_config,
                methods=["GET"],
                desc="获取插件当前配置（脱敏）",
            )
            self.context.register_web_api(
                route=f"/{PLUGIN_NAME}/emoji-harvester/api/analyze",
                view_handler=self.api_analyze,
                methods=["POST"],
                desc="手动触发一批视觉分析",
            )
            self.context.register_web_api(
                route=f"/{PLUGIN_NAME}/emoji-harvester/api/emotions",
                view_handler=self.api_emotions,
                methods=["GET"],
                desc="情绪标签分布",
            )
            self._registered = True

        # LLM 工具：让主对话模型自己决定什么时候发表情包
        if self.tool_send_enabled and self.ai_enabled:
            try:
                from astrbot.api import llm_tool

                llm_tool(name="send_emoji")(self.send_emoji_tool)
                logger.info(f"[{PLUGIN_NAME}] 已注册 LLM 工具 send_emoji")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[{PLUGIN_NAME}] 注册 LLM 工具失败: {exc!s}")

        # 后台视觉分析任务
        if self.ai_enabled:
            self._start_analysis_task()

        logger.info(
            f"[{PLUGIN_NAME}] 初始化完成，监听 QQ: {self.target_qq_list}"
            f"，AI 模式: {self.ai_enabled}",
        )

    async def terminate(self) -> None:
        """插件卸载：关闭数据库连接、停止后台任务。"""
        task = getattr(self, "_analysis_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        self.storage.close()
        logger.info(f"[{PLUGIN_NAME}] 已卸载")

    def _start_analysis_task(self) -> None:
        """启动后台视觉分析任务。"""
        task = getattr(self, "_analysis_task", None)
        if task is not None and not task.done():
            return
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        logger.info(f"[{PLUGIN_NAME}] 视觉分析任务已启动")

    async def _analysis_loop(self) -> None:
        """后台循环：定期分析待处理的素材。"""
        # 先等一会儿再开始，避免和插件初始化抢资源
        await asyncio.sleep(5)
        while True:
            try:
                count = await self.picker.analyze_pending(self.vision_batch_size)
                if count:
                    logger.info(f"[{PLUGIN_NAME}] 完成 {count} 张素材的视觉分析")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[{PLUGIN_NAME}] 视觉分析出错: {exc!s}")
            await asyncio.sleep(self.vision_interval)

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001 - 配置异常不应让插件崩溃
            return default
        return default if value is None else value

    @property
    def target_qq_list(self) -> list[str]:
        """被锁定的 QQ 号列表。"""
        return parse_target_qq(self._cfg("target_qq", []))

    @property
    def collect_enabled(self) -> bool:
        return bool(self._cfg("collect_enabled", DEFAULT_ENABLED))

    @property
    def collect_face(self) -> bool:
        return bool(self._cfg("collect_face", True))

    @property
    def collect_mface(self) -> bool:
        return bool(self._cfg("collect_mface", True))

    @property
    def only_in_group(self) -> bool:
        """只采集群聊消息。"""
        return bool(self._cfg("only_in_group", False))

    @property
    def max_file_mb(self) -> int:
        return clamp_int(self._cfg("max_file_mb", 10), 10, 1, 100)

    @property
    def notify_on_collect(self) -> bool:
        return bool(self._cfg("notify_on_collect", False))

    @property
    def download_timeout(self) -> int:
        return clamp_int(self._cfg("download_timeout", 15), 15, 3, 120)

    # ------------------------------------------------------------------
    # AI 相关配置
    # ------------------------------------------------------------------
    @property
    def ai_enabled(self) -> bool:
        """是否启用 AI 能力（视觉分析 + 自动发表情包）。"""
        return bool(self._cfg("ai_enabled", False))

    @property
    def auto_send_enabled(self) -> bool:
        """自动模式：由插件自己判断要不要发表情包。"""
        return bool(self._cfg("auto_send_enabled", False))

    @property
    def tool_send_enabled(self) -> bool:
        """工具模式：把 send_emoji 工具交给主对话模型，让它自己调用。"""
        return bool(self._cfg("tool_send_enabled", True))

    @property
    def vision_api_base(self) -> str:
        return str(self._cfg("vision_api_base", "") or "").strip().rstrip("/")

    @property
    def vision_api_key(self) -> str:
        return str(self._cfg("vision_api_key", "") or "").strip()

    @property
    def vision_model(self) -> str:
        return str(self._cfg("vision_model", "") or "").strip()

    @property
    def provider_source_id(self) -> str:
        """复用 AstrBot 已有的 provider source（留空则用插件自己的配置）。"""
        return str(self._cfg("provider_source_id", "") or "").strip()

    @property
    def vision_batch_size(self) -> int:
        return clamp_int(self._cfg("vision_batch_size", 5), 5, 1, 20)

    @property
    def vision_interval(self) -> int:
        """后台分析任务的轮询间隔（秒）。"""
        return clamp_int(self._cfg("vision_interval", 60), 60, 15, 3600)

    @property
    def auto_send_cooldown(self) -> int:
        """自动发表情包的冷却时间（秒），防止刷屏。"""
        return clamp_int(self._cfg("auto_send_cooldown", 30), 30, 0, 3600)

    @property
    def auto_send_max_per_hour(self) -> int:
        """每小时最多自动发送多少张表情包。"""
        return clamp_int(self._cfg("auto_send_max_per_hour", 10), 10, 0, 200)

    def _resolve_api_credentials(self) -> tuple[str, str]:
        """解析出 (api_base, api_key)。

        优先用插件自己的配置；没配 ``vision_api_key`` 时，
        尝试复用 AstrBot 主配置里指定的 provider source
        （插件自己的 config 里没有 provider_sources，
        必须从 ``context.get_config()`` 拿主配置）。
        """
        if self.vision_api_base and self.vision_api_key:
            return self.vision_api_base, self.vision_api_key

        source_id = self.provider_source_id
        if not source_id:
            return "", ""

        sources = self._get_main_config_sources()
        for source in sources:
            if not isinstance(source, dict):
                continue
            if str(source.get("id")) != source_id:
                continue
            api_base = str(source.get("api_base") or "").strip().rstrip("/")
            api_key = ""
            keys = source.get("key") or []
            if isinstance(keys, str):
                keys = [keys]
            for item in keys:
                if isinstance(item, str) and item:
                    api_key = item
                    break
                if isinstance(item, dict):
                    candidate = item.get("key") or item.get("api_key")
                    if candidate:
                        api_key = str(candidate)
                        break
            if api_base and api_key:
                return api_base, api_key
        return "", ""

    def _get_main_config_sources(self) -> list[Any]:
        """从 AstrBot 主配置里取 provider_sources 列表。"""
        try:
            main_config = self.context.get_config()
        except Exception:  # noqa: BLE001
            return []
        try:
            sources = main_config.get("provider_sources") or []
        except Exception:  # noqa: BLE001
            return []
        return sources if isinstance(sources, list) else []

    def _make_llm_client(self) -> LLMClient | None:
        """构造 LLM 客户端；没配置好就返回 None。"""
        api_base, api_key = self._resolve_api_credentials()
        if not api_base or not api_key or not self.vision_model:
            return None
        return LLMClient(
            api_base=api_base,
            api_key=api_key,
            model=self.vision_model,
            timeout=clamp_int(self._cfg("vision_timeout", 60), 60, 10, 300),
        )

    def is_target(self, sender_id: str) -> bool:
        """判断发送者是否在锁定名单里。"""
        return is_target_sender(sender_id, self.target_qq_list)

    # ------------------------------------------------------------------
    # 采集入口
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, alias="emoji_harvester_collect")
    async def on_message(self, event: AstrMessageEvent) -> None:
        """监听所有消息：采集被锁定用户发的图片，并在自动模式下发表情包。"""
        # 1) 先记录机器人自己的 QQ 号（拼表情包图片地址要用）
        self_uin = str(getattr(event.message_obj, "self_id", "") or "").strip()
        if self_uin.isdigit():
            self._self_uin = self_uin

        # 2) 自动发表情包（与采集互不影响）
        if self.ai_enabled and self.auto_send_enabled:
            try:
                await self._auto_send_emoji(event)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[{PLUGIN_NAME}] 自动发表情包出错: {exc!s}")

        # 3) 采集
        if not self.collect_enabled:
            return
        targets = self.target_qq_list
        if not targets:
            return

        sender_id = event.get_sender_id()
        if not self.is_target(sender_id):
            return

        if self.only_in_group and not event.get_group_id():
            return

        try:
            collected = await self._collect_from_event(event)
        except Exception as exc:  # noqa: BLE001 - 采集失败不能影响正常对话
            logger.error(f"[{PLUGIN_NAME}] 采集失败: {exc!s}")
            return

        if collected and self.notify_on_collect:
            await event.send(
                f"📥 已采集 {collected} 张图片"
                f"（{self.storage.stats()['count']} 张在库）",
            )

    async def _collect_from_event(self, event: AstrMessageEvent) -> int:
        """从一条消息里提取并保存所有图片资源，返回新增数量。"""
        count = 0
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        group_id = event.get_group_id()
        session_id = event.get_session_id()

        # 1) 标准 Image 消息段
        for component in event.get_messages():
            from astrbot.core.message.components import Image

            if not isinstance(component, Image):
                continue
            data = await self._image_to_bytes(component)
            if not data:
                continue
            asset = await self.storage.add_bytes(
                data,
                filename=self._component_filename(component),
                source=SOURCE_IMAGE,
                sender_id=sender_id,
                sender_name=sender_name,
                group_id=group_id,
                session_id=session_id,
            )
            if asset is not None:
                count += 1

        # 2) raw_message 里的 face / mface（AstrBot 的 aiocqhttp 适配器会丢弃 mface 段）
        count += await self._collect_raw_faces(event, sender_id, sender_name, group_id, session_id)
        return count

    @staticmethod
    def _component_filename(component: Any) -> str:
        """给图片段推导一个可读的文件名。"""
        return derive_filename(
            getattr(component, "url", "") or "",
            getattr(component, "file", "") or "",
        )

    async def _image_to_bytes(self, component: Any) -> bytes | None:
        """把 Image 消息段转成原始字节。"""
        try:
            path = await component.convert_to_file_path()
        except Exception as exc:  # noqa: BLE001 - 下载失败属正常情况
            logger.debug(f"[{PLUGIN_NAME}] 图片下载失败: {exc!s}")
            return None

        file_path = Path(path)
        if not file_path.is_file():
            return None
        try:
            if file_path.stat().st_size > self.max_file_mb * 1024 * 1024:
                logger.debug(f"[{PLUGIN_NAME}] 跳过超大文件: {file_path.name}")
                return None
            return file_path.read_bytes()
        except OSError as exc:
            logger.debug(f"[{PLUGIN_NAME}] 读取文件失败: {exc!s}")
            return None

    async def _collect_raw_faces(
        self,
        event: AstrMessageEvent,
        sender_id: str,
        sender_name: str,
        group_id: str,
        session_id: str,
    ) -> int:
        """从 raw_message 中采集 face（系统表情）与 mface（商城表情包）。"""
        count = 0
        raw = getattr(event.message_obj, "raw_message", None)
        if raw is None:
            return 0
        segments = getattr(raw, "message", None)
        if not isinstance(segments, list):
            return 0

        for segment in segments:
            if not isinstance(segment, dict):
                continue
            seg_type = segment.get("type")
            data = segment.get("data") or {}
            if not isinstance(data, dict):
                continue

            if seg_type == "face" and self.collect_face:
                payload = await self._fetch_qq_emoji(data.get("id"))
                if payload:
                    count += await self._save_bytes(
                        payload,
                        filename=f"face_{data.get('id')}.gif",
                        source=SOURCE_FACE,
                        sender_id=sender_id,
                        sender_name=sender_name,
                        group_id=group_id,
                        session_id=session_id,
                    )
            elif seg_type == "mface" and self.collect_mface:
                # uin 用表情归属的账号：优先段里的字段，其次机器人自己
                uin = str(
                    data.get("uin")
                    or self._self_uin
                    or ""
                )
                payload = await self._fetch_mface(data, uin)
                if payload:
                    emoji_id = str(
                        data.get("emoji_id")
                        or data.get("emojiId")
                        or data.get("key")
                        or data.get("id")
                        or "mface"
                    )
                    count += await self._save_bytes(
                        payload,
                        filename=f"mface_{emoji_id}.gif",
                        source=SOURCE_MFACE,
                        sender_id=sender_id,
                        sender_name=sender_name,
                        group_id=group_id,
                        session_id=session_id,
                    )
        return count

    async def _save_bytes(
        self,
        data: bytes,
        *,
        filename: str,
        source: str,
        sender_id: str,
        sender_name: str,
        group_id: str,
        session_id: str,
    ) -> int:
        """保存字节流，成功返回 1，失败返回 0。"""
        asset = await self.storage.add_bytes(
            data,
            filename=filename,
            source=source,
            sender_id=sender_id,
            sender_name=sender_name,
            group_id=group_id,
            session_id=session_id,
        )
        if asset is None:
            return 0
        # 已存在（重复内容）也算成功，避免重复刷屏
        return 1

    # ------------------------------------------------------------------
    # QQ 表情接口
    # ------------------------------------------------------------------
    async def _http_get(self, url: str) -> bytes | None:
        """简单的 HTTP GET，返回字节。"""
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=self.download_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return None
                    return await response.read()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] HTTP 请求失败 {url}: {exc!s}")
            return None

    async def _fetch_qq_emoji(self, emoji_id: Any) -> bytes | None:
        """取 QQ 系统表情图片（``face`` 段）。

        系统表情 id 是纯数字，地址形如
        ``https://qzonestyle.gtimg.cn/qzone/em/e{id}.gif``。
        """
        url = build_face_url(emoji_id)
        if url is None:
            return None
        return await self._http_get(url)

    async def _fetch_mface(
        self,
        data: dict[str, Any],
        uin: str = "",
    ) -> bytes | None:
        """取 QQ 商城表情包原图（``mface`` 段）。

        不同 OneBot 实现的字段不一样，这里逐个兼容：

        - NapCat：``{"url": "https://..."}``，直接用 url。
        - SnowLuma / Lagrange：``{"emoji_id": "<32位HEX>", "key": "..."}``，
          没有 url，需要用 ``emoji_id`` 拼 ``p.qpic.cn`` 地址；
          该地址里的 uin 必须与表情归属的账号一致。
        - 老版本只有 ``key``：退回到 ``qzonestyle.gtimg.cn``。
        """
        # 1) 直接给了 url 的实现
        url = str(data.get("url") or "").strip()
        if url.startswith("http"):
            payload = await self._http_get(url)
            if payload:
                return payload

        # 2) 32 位十六进制 emoji_id -> p.qpic.cn
        emoji_id = str(
            data.get("emoji_id") or data.get("emojiId") or data.get("id") or ""
        ).strip()
        if MFACE_ID_RE.match(emoji_id):
            candidates = []
            primary = build_mface_url(emoji_id, uin)
            if primary:
                candidates.append(primary)
            # 表情可能归属于别的账号，退回到用 key 拼
            key = str(data.get("key") or "").strip()
            if key and key != emoji_id and uin:
                candidates.append(
                    MFACE_URL_TEMPLATE.format(uin=uin, emoji_id=key)
                )
            for candidate in candidates:
                payload = await self._http_get(candidate)
                if payload:
                    return payload

        # 3) 老的 key 形式 -> qzonestyle.gtimg.cn
        key = str(data.get("key") or "").strip()
        if key:
            return await self._http_get(FACE_URL_TEMPLATE.format(key))
        return None

    # ------------------------------------------------------------------
    # AI：发表情包
    # ------------------------------------------------------------------
    def _build_context(self, event: AstrMessageEvent) -> str:
        """把当前消息拼成给 AI 的情境描述。"""
        try:
            text = event.get_message_str().strip()
        except Exception:  # noqa: BLE001
            text = ""
        if not text:
            try:
                text = event.get_message_outline()
            except Exception:  # noqa: BLE001
                text = ""
        sender = event.get_sender_name() or event.get_sender_id()
        return f"群成员「{sender}」说：{text}"

    def _can_auto_send(self) -> bool:
        """自动发送的限流检查。"""
        now = time.time()
        history = [stamp for stamp in self._auto_send_history if now - stamp < 3600]
        self._auto_send_history = history
        limit = self.auto_send_max_per_hour
        if limit and len(history) >= limit:
            return False
        cooldown = self.auto_send_cooldown
        if cooldown and history and now - max(history) < cooldown:
            return False
        return True

    def _record_auto_send(self) -> None:
        self._auto_send_history.append(time.time())

    async def _send_asset_image(self, event: AstrMessageEvent, asset: Asset) -> bool:
        """把素材作为图片消息发出去。"""
        try:
            path = self.storage.media_path(asset.stored_name)
        except ValueError:
            return False
        if not path.is_file():
            return False

        from astrbot.core.message.components import Image

        try:
            await event.send(Image.fromFileSystem(str(path)))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 发送表情包失败: {exc!s}")
            return False
        return True

    async def send_emoji_tool(
        self,
        event: AstrMessageEvent,
        emotion: str = "",
        context: str = "",
    ):
        """从表情包图库里挑一张最贴合当前情绪的图片发出来。

        Args:
            emotion(string): 想要表达的情绪，例如 happy / laugh / surprise / sad / angry / love / cry / 尴尬 / 无语
            context(string): 当前聊天情境的简短描述，例如「用户分享了好消息」
        """
        if not self.ai_enabled:
            return "AI 模式未启用，无法发表情包。"

        query = " ".join(part for part in (emotion, context) if part).strip()
        if not query:
            query = self._build_context(event)

        result = await self.picker.pick(query)
        if result.asset is None:
            return f"没有找到合适的表情包（{result.reason}）。"

        sent = await self._send_asset_image(event, result.asset)
        if not sent:
            return "表情包文件缺失，发送失败。"

        suffix = f"（{result.reason}）" if result.reason else ""
        return f"已发送表情包 #{result.asset.id} {result.asset.filename}{suffix}"

    async def _auto_send_emoji(self, event: AstrMessageEvent) -> bool:
        """自动模式：判断该不该发表情包，是则发一张。"""
        if not (self.ai_enabled and self.auto_send_enabled):
            return False
        if not self._can_auto_send():
            return False

        context = self._build_context(event)
        try:
            result = await self.picker.decide(context)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 自动发表情包判断失败: {exc!s}")
            return False

        if not result.should_send or result.asset is None:
            logger.debug(f"[{PLUGIN_NAME}] 不自动发表情包：{result.reason}")
            return False

        sent = await self._send_asset_image(event, result.asset)
        if sent:
            self._record_auto_send()
            logger.info(
                f"[{PLUGIN_NAME}] 自动发送表情包 #{result.asset.id}（{result.reason}）",
            )
        return sent

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command("图库")
    async def cmd_gallery(self, event: AstrMessageEvent, *args: str) -> None:
        """图库指令：查看统计 / 搜索 / 清空。

        用法：
        - ``/图库`` 查看图库统计与最近收录
        - ``/图库 搜索 关键词`` 搜索素材
        - ``/图库 清空`` 清空全部素材
        """
        action = args[0] if args else "统计"
        if action in ("清空", "clear"):
            count = await self.storage.clear()
            await event.send(f"🗑️ 已清空图库，删除 {count} 条素材。")
            return

        if action in ("搜索", "search") and len(args) > 1:
            keyword = " ".join(args[1:])
            page = self.storage.list_assets(Query(keyword=keyword, page_size=5))
            if not page.items:
                await event.send(f"🔍 没有找到包含「{keyword}」的素材。")
                return
            lines = [f"🔍 找到 {page.total} 条素材（显示前 {len(page.items)} 条）："]
            for item in page.items:
                lines.append(
                    f"• #{item.id} {item.filename} "
                    f"({item.width}×{item.height}, {item.size // 1024}KB)",
                )
            await event.send("\n".join(lines))
            return

        stats = self.storage.stats()
        recent = self.storage.list_assets(Query(page_size=3))
        lines = [
            "🖼️ 表情包图库",
            f"• 总素材：{stats['count']} 张",
            f"• 动画表情：{stats['animated_count']} 张",
            f"• 占用空间：{stats['total_bytes'] / 1024 / 1024:.1f} MB",
            f"• 监听 QQ：{', '.join(self.target_qq_list) or '（未设置）'}",
        ]
        if recent.items:
            lines.append("最近收录：")
            for item in recent.items:
                lines.append(f"• #{item.id} {item.filename}")
        await event.send("\n".join(lines))

    @filter.command("发表情包")
    async def cmd_send_emoji(self, event: AstrMessageEvent, *args: str) -> None:
        """手动让 AI 挑一张表情包发出来。

        用法：
        - ``/发表情包`` 让 AI 根据当前消息挑一张
        - ``/发表情包 开心 分享了好消息`` 指定情绪和情境
        """
        if not self.ai_enabled:
            await event.send("❌ AI 模式未启用。")
            return
        emotion = args[0] if args else ""
        context = " ".join(args[1:]) if len(args) > 1 else ""
        result = await self.send_emoji_tool(event, emotion, context)
        await event.send(str(result))

    @filter.command("图库状态")
    async def cmd_gallery_status(self, event: AstrMessageEvent) -> None:
        """查看图库与 AI 标注状态。"""
        stats = self.storage.stats()
        pending = self.storage.pending_analysis_count()
        lines = [
            "🖼️ 表情包图库",
            f"• 素材总数：{stats['count']} 张",
            f"• 占用空间：{stats['total_bytes'] / 1024 / 1024:.1f} MB",
            f"• AI 模式：{'开启' if self.ai_enabled else '关闭'}",
            f"• 自动发表情包：{'开启' if self.auto_send_enabled else '关闭'}",
            f"• 待分析：{pending} 张",
        ]
        if self.ai_enabled:
            emotions = self.storage.emotion_summary()
            if emotions:
                top = "、".join(
                    f"{item['emotion']}({item['count']})" for item in emotions[:6]
                )
                lines.append(f"• 情绪分布：{top}")
        await event.send("\n".join(lines))

    @filter.command("添加图库目标")
    async def cmd_add_target(self, event: AstrMessageEvent, qq: str) -> None:
        """把某个 QQ 号加入采集名单。"""
        target = str(qq).strip()
        if not target:
            await event.send("❌ 请提供 QQ 号。")
            return
        current = self.target_qq_list
        if target in current:
            await event.send(f"ℹ️ QQ {target} 已在采集名单中。")
            return
        current.append(target)
        if not self._save_targets(current):
            await event.send("❌ 配置保存失败，请稍后重试。")
            return
        await event.send(f"✅ 已将 QQ {target} 加入采集名单。")

    @filter.command("移除图库目标")
    async def cmd_remove_target(self, event: AstrMessageEvent, qq: str) -> None:
        """把某个 QQ 号移出采集名单。"""
        target = str(qq).strip()
        current = self.target_qq_list
        if target not in current:
            await event.send(f"ℹ️ QQ {target} 不在采集名单中。")
            return
        current.remove(target)
        if not self._save_targets(current):
            await event.send("❌ 配置保存失败，请稍后重试。")
            return
        await event.send(f"✅ 已将 QQ {target} 移出采集名单。")

    def _save_targets(self, targets: list[str]) -> bool:
        """把采集名单写回插件配置文件。

        正常运行时 ``self.config`` 是 ``AstrBotConfig``（带 ``save_config``）；
        这里同时兼容普通 dict，方便单元测试。
        """
        try:
            self.config["target_qq"] = targets
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
            else:
                logger.warning(
                    "[%s] 配置对象不支持 save_config，名单仅在内存中生效",
                    PLUGIN_NAME,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 保存配置失败: {exc!s}")
            return False
        return True

    # ------------------------------------------------------------------
    # Web API
    # ------------------------------------------------------------------
    def _query_from_request(self) -> Query:
        params = request.query
        raw_page = params.get("page", "1")
        raw_size = params.get("page_size", "48")
        try:
            page = int(raw_page)
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(raw_size)
        except (TypeError, ValueError):
            page_size = 48
        return Query(
            keyword=str(params.get("keyword", "") or "").strip(),
            sender_id=str(params.get("sender_id", "") or "").strip(),
            source=str(params.get("source", "") or "").strip(),
            group_id=str(params.get("group_id", "") or "").strip(),
            emotion=str(params.get("emotion", "") or "").strip(),
            animated_only=str(params.get("animated", "")).lower() in ("1", "true", "yes"),
            static_only=str(params.get("static", "")).lower() in ("1", "true", "yes"),
            order=str(params.get("order", "new") or "new"),
            page=page,
            page_size=page_size,
        )

    async def api_assets(self) -> dict[str, Any]:
        """GET /emoji-harvester/api/assets"""
        query = self._query_from_request()
        page = self.storage.list_assets(query)
        return {
            "status": "ok",
            "message": "",
            "data": {
                **page.to_dict(),
                "items": [self._item_with_url(item) for item in page.items],
            },
        }

    def _item_with_url(self, item: Asset) -> dict[str, Any]:
        data = item.to_dict()
        # 完整地址与前端 bridge 拼出的形式保持一致
        data["url"] = (
            f"/api/v1/plugins/extensions/{PLUGIN_NAME}"
            f"/emoji-harvester/api/asset/{item.id}"
        )
        return data

    async def api_stats(self) -> dict[str, Any]:
        """GET /emoji-harvester/api/stats"""
        stats = self.storage.stats()
        stats["targets"] = self.target_qq_list
        stats["collect_enabled"] = self.collect_enabled
        return {"status": "ok", "message": "", "data": stats}

    async def api_senders(self) -> dict[str, Any]:
        """GET /emoji-harvester/api/senders"""
        return {
            "status": "ok",
            "message": "",
            "data": {"senders": self.storage.list_senders()},
        }

    async def api_asset_file(self, asset_id: str) -> Any:
        """GET /emoji-harvester/api/asset/<asset_id> —— 返回图片本体。"""
        from astrbot.api.web import file_response

        try:
            numeric_id = int(str(asset_id))
        except (TypeError, ValueError):
            return {"status": "error", "message": "非法的素材 ID", "data": {}}

        asset = self.storage.get_asset(numeric_id)
        if asset is None:
            return {"status": "error", "message": "素材不存在", "data": {}}

        try:
            path = self.storage.media_path(asset.stored_name)
        except ValueError:
            return {"status": "error", "message": "非法的素材路径", "data": {}}
        if not path.is_file():
            return {"status": "error", "message": "素材文件已丢失", "data": {}}
        # 注意：astrbot.api.web.file_response 的参数名是 content_type
        return file_response(path, content_type=asset.mime)

    async def api_delete(self) -> dict[str, Any]:
        """POST /emoji-harvester/api/delete —— body: {"id": 123}"""
        body = await request.json(default={}) or {}
        try:
            asset_id = int(body.get("id"))
        except (TypeError, ValueError):
            return {"status": "error", "message": "缺少合法的 id", "data": {}}
        ok = await self.storage.delete_asset(asset_id)
        if not ok:
            return {"status": "error", "message": "素材不存在", "data": {}}
        return {"status": "ok", "message": "已删除", "data": {"id": asset_id}}

    async def api_clear(self) -> dict[str, Any]:
        """POST /emoji-harvester/api/clear"""
        count = await self.storage.clear()
        return {"status": "ok", "message": f"已清空 {count} 条素材", "data": {"deleted": count}}

    async def api_export(self) -> dict[str, Any]:
        """GET /emoji-harvester/api/export —— 导出清单 JSON。"""
        manifest_path = self.storage.root / "manifest.json"
        write_manifest(self.storage, manifest_path)
        return {
            "status": "ok",
            "message": "",
            "data": {
                "path": str(manifest_path),
                "count": len(self.storage.export_manifest()),
            },
        }

    async def api_config(self) -> dict[str, Any]:
        """GET /emoji-harvester/api/config —— 返回脱敏后的配置。"""
        return {
            "status": "ok",
            "message": "",
            "data": {
                "target_qq": self.target_qq_list,
                "collect_enabled": self.collect_enabled,
                "collect_face": self.collect_face,
                "collect_mface": self.collect_mface,
                "only_in_group": self.only_in_group,
                "max_file_mb": self.max_file_mb,
                "notify_on_collect": self.notify_on_collect,
                "plugin_version": PLUGIN_VERSION,
                "ai_enabled": self.ai_enabled,
                "auto_send_enabled": self.auto_send_enabled,
                "tool_send_enabled": self.tool_send_enabled,
                "vision_model": self.vision_model,
                "has_api_key": bool(self._resolve_api_credentials()[1]),
                "pending_analysis": self.storage.pending_analysis_count(),
            },
        }

    async def api_analyze(self) -> dict[str, Any]:
        """POST /emoji-harvester/api/analyze —— 手动触发一批视觉分析。"""
        if not self.ai_enabled:
            return {
                "status": "error",
                "message": "AI 模式未启用",
                "data": {},
            }
        count = await self.picker.analyze_pending(self.vision_batch_size)
        return {
            "status": "ok",
            "message": f"已分析 {count} 张素材",
            "data": {
                "analyzed": count,
                "pending": self.storage.pending_analysis_count(),
            },
        }

    async def api_emotions(self) -> dict[str, Any]:
        """GET /emoji-harvester/api/emotions —— 情绪标签分布。"""
        return {
            "status": "ok",
            "message": "",
            "data": {
                "emotions": self.storage.emotion_summary(),
                "pending": self.storage.pending_analysis_count(),
            },
        }
