"""表情包的视觉语义分析：给图片打「情绪 / 场景」标签。

思路：
- 视觉模型（GLM-5V 等）对每张图生成简短的结构化描述：
  ``情绪`` / ``场景`` / ``描述`` 三个维度。
- 描述存进数据库，供 LLM 工具按语义检索。

批量分析而非逐张实时分析：采集是持续行为，图库会变大，
所以在采集时把待分析的图累积起来，由后台任务按批处理。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

# 视觉分析提示词：要求模型输出严格的 JSON，方便解析
VISION_PROMPT = """你是一个表情包语义标注助手。请分析这张图片，输出 JSON，格式如下：

{"emotion": "主要情绪（happy/sad/angry/surprised/love/laugh/cry/smile/embarrassed/angry/question/mocking/uncomfortable/other）", "scene": "使用场景（日常聊天/嘲讽吐槽/卖萌撒娇/感谢赞美/道歉安慰/震惊无语/庆祝/工作汇报/其他）", "caption": "一句话中文描述（20字以内）", "keywords": ["关键词1","关键词2"]}

只输出 JSON，不要有任何其他文字。"""


@dataclass
class VisionTag:
    """一张图片的视觉标签。"""

    emotion: str = ""
    scene: str = ""
    caption: str = ""
    keywords: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.keywords is None:
            self.keywords = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "emotion": self.emotion,
            "scene": self.scene,
            "caption": self.caption,
            "keywords": self.keywords,
        }

    @property
    def search_text(self) -> str:
        """拼成用于检索的文本。"""
        parts = [self.emotion, self.scene, self.caption, " ".join(self.keywords)]
        return " ".join(part for part in parts if part)

    def is_empty(self) -> bool:
        return not (self.emotion or self.scene or self.caption)


def parse_vision_json(raw: str) -> VisionTag:
    """从模型输出里抠出 JSON 并解析。

    模型有时会带上 ```json 包裹或前后废话，这里做容错解析。
    """
    if not raw:
        return VisionTag()

    text = raw.strip()
    # 去掉 markdown 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # 抠出第一个完整的 JSON 对象
    start = text.find("{")
    if start < 0:
        return VisionTag()
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
        return VisionTag()

    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError:
        return VisionTag()

    if not isinstance(data, dict):
        return VisionTag()

    keywords = data.get("keywords")
    if isinstance(keywords, str):
        keywords = [keywords]
    if not isinstance(keywords, list):
        keywords = []

    return VisionTag(
        emotion=str(data.get("emotion") or "").strip(),
        scene=str(data.get("scene") or "").strip(),
        caption=str(data.get("caption") or "").strip(),
        keywords=[str(item).strip() for item in keywords if str(item).strip()][:8],
    )


def build_image_block(image_path: str, mime: str = "image/jpeg") -> dict[str, Any]:
    """构造 OpenAI 兼容的 image_url 内容块（base64 内联）。"""
    import base64
    from pathlib import Path

    path = Path(image_path)
    data = path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


def build_vision_messages(image_path: str, mime: str = "image/jpeg") -> list[dict[str, Any]]:
    """构造视觉分析请求的 messages。"""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_PROMPT},
                build_image_block(image_path, mime),
            ],
        }
    ]
