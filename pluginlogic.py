"""插件的纯逻辑部分：配置解析、目标判定、文件名推导、QQ 表情地址构造。

这一层刻意不依赖 AstrBot 运行时，方便在没有 AstrBot 的环境里跑单元测试。
"""

from __future__ import annotations

import re
from typing import Any

# 插件唯一识别名
PLUGIN_NAME = "astrbot_plugin_emoji_harvester"
PLUGIN_VERSION = "v1.0.0"

# QQ 表情图地址模板
# - 系统表情（face）：qzonestyle.gtimg.cn 用数字 id + .gif
FACE_URL_TEMPLATE = "https://qzonestyle.gtimg.cn/qzone/em/e{}.gif"
# - 商城表情包（mface）：p.qpic.cn 用 32 位十六进制 emoji_id，
#   路径里必须带上表情所属的 QQ 号（uin），换 uin 会 404。
MFACE_URL_TEMPLATE = (
    "https://p.qpic.cn/qq_expression/{uin}/{uin}_0_0_0_{emoji_id}_0_0/0"
)
# mface 段的 emoji_id 必须是 32 位十六进制（QQ 协议强制校验）
MFACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

# 采集来源类型
SOURCE_IMAGE = "image"
SOURCE_FACE = "face"
SOURCE_MFACE = "mface"

# 采集开关的默认行为
DEFAULT_ENABLED = True


def parse_target_qq(raw: Any) -> list[str]:
    """把配置里的监听名单解析成去重后的 QQ 号列表。

    兼容三种写法：列表、逗号分隔字符串（含中文逗号）、None。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.replace("，", ",").replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        return []

    result: list[str] = []
    for part in parts:
        text = str(part).strip()
        if text and text not in result:
            result.append(text)
    return result


def is_target_sender(sender_id: str, targets: list[str]) -> bool:
    """判断发送者是否在锁定名单里。"""
    if not sender_id:
        return False
    if not targets:
        return False
    return str(sender_id) in targets


def derive_filename(url: str = "", file_ref: str = "") -> str:
    """从图片段的 url / file 字段推导一个可读的文件名。"""
    for candidate in (url, file_ref):
        if not candidate:
            continue
        text = str(candidate)
        if "://" in text:
            text = text.split("://", 1)[1]
        text = text.split("?", 1)[0].split("#", 1)[0]
        name = text.rsplit("/", 1)[-1]
        if "." in name:
            return name
    return "image.bin"


def build_face_url(emoji_id: Any) -> str | None:
    """系统表情地址；id 不是纯数字时返回 None。"""
    if emoji_id is None:
        return None
    text = str(emoji_id).strip()
    if not text.isdigit():
        return None
    return FACE_URL_TEMPLATE.format(text)


def build_mface_url(emoji_id: Any, uin: str = "") -> str | None:
    """商城表情包地址。

    ``emoji_id`` 必须是 32 位十六进制；``uin`` 是表情归属的 QQ 号，
    缺失时地址会 404。
    """
    text = str(emoji_id or "").strip()
    if not MFACE_ID_RE.match(text):
        return None
    if not uin:
        return None
    return MFACE_URL_TEMPLATE.format(uin=uin, emoji_id=text)


def clamp_int(value: Any, default: int, low: int, high: int) -> int:
    """把配置里的数值安全地夹到 [low, high]。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))
