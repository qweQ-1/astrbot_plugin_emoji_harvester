"""不依赖第三方库的轻量图片信息解析。"""

from __future__ import annotations


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """扫描 JPEG 的 SOF 段读取宽高。"""
    index = 2
    length = len(data)
    while index + 3 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        # 填充字节
        if marker in (0xFF, 0x00):
            index += 1
            continue
        if marker in (0xD8, 0xD9):
            index += 2
            continue
        if index + 4 > length:
            break
        seg_len = int.from_bytes(data[index + 2 : index + 4], "big")
        # SOF0..SOF15，排除 DHT(C4) / JPG(C8) / DAC(CC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if index + 9 <= length:
                height = int.from_bytes(data[index + 5 : index + 7], "big")
                width = int.from_bytes(data[index + 7 : index + 9], "big")
                return width, height
        if seg_len < 2:
            break
        index += 2 + seg_len
    return 0, 0


def _webp_size(data: bytes) -> tuple[int, int]:
    if len(data) < 30:
        return 0, 0
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        if len(data) >= 30:
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return width, height
        return 0, 0
    if chunk == b"VP8L":
        if len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return width, height
        return 0, 0
    return 0, 0
