"""存储层单元测试（不依赖 AstrBot 运行时）。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import Query, Storage  # noqa: E402


def make_png(width: int = 4, height: int = 3, salt: int = 0) -> bytes:
    """构造一张最小合法 PNG（用 zlib 压缩 IDAT）。

    ``salt`` 用于生成不同内容，避免被存储层的 sha256 去重合并。
    """
    import struct
    import zlib

    pixel = bytes((0xFF, (salt * 37) % 256, (salt * 91) % 256))
    raw = b"".join(b"\x00" + pixel * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_gif(width: int = 5, height: int = 6, salt: int = 0) -> bytes:
    header = b"GIF89a" + width.to_bytes(2, "little") + height.to_bytes(2, "little")
    header += b"\xf0\x00\x00"
    header += bytes((salt % 256, (salt * 3) % 256, (salt * 7) % 256))
    header += b"\xff\xff\xff"
    return (
        header
        + b"\x2c"
        + b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(2, "little")
        + (height - 1).to_bytes(2, "little")
        + b"\x00\x02\x02\x44\x01\x00\x3b"
    )


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.storage = Storage(self.root / "data")
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.loop.close)

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)

    def test_add_bytes_and_query(self) -> None:
        png = make_png(10, 20)
        asset = self.run_async(
            self.storage.add_bytes(
                png,
                filename="cat.png",
                source="image",
                sender_id="123456",
                sender_name="阿狸",
            )
        )
        self.assertIsNotNone(asset)
        assert asset is not None
        self.assertEqual(asset.width, 10)
        self.assertEqual(asset.height, 20)
        self.assertEqual(asset.mime, "image/png")
        self.assertEqual(asset.size, len(png))

        page = self.storage.list_assets(Query())
        self.assertEqual(page.total, 1)
        self.assertEqual(page.items[0].filename, "cat.png")
        self.assertEqual(page.items[0].sender_name, "阿狸")

    def test_duplicate_content_is_deduped(self) -> None:
        png = make_png()
        first = self.run_async(self.storage.add_bytes(png, filename="a.png"))
        second = self.run_async(self.storage.add_bytes(png, filename="b.png"))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        # 同一份内容 -> 同一条记录，不重复落盘
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.storage.list_assets(Query()).total, 1)

    def test_rejects_non_image(self) -> None:
        asset = self.run_async(
            self.storage.add_bytes(b"not an image at all", filename="x.bin")
        )
        self.assertIsNone(asset)

    def test_rejects_empty(self) -> None:
        self.assertIsNone(self.run_async(self.storage.add_bytes(b"", filename="x.png")))

    def test_rejects_oversize(self) -> None:
        from storage import MAX_FILE_BYTES

        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_FILE_BYTES + 10)
        self.assertIsNone(self.run_async(self.storage.add_bytes(big, filename="big.png")))

    def test_keyword_and_filter_query(self) -> None:
        self.run_async(
            self.storage.add_bytes(
                make_png(salt=1), filename="dog.png", sender_id="1", sender_name="小明"
            )
        )
        self.run_async(
            self.storage.add_bytes(
                make_png(salt=2), filename="cat.png", sender_id="2", sender_name="小红"
            )
        )
        self.assertEqual(self.storage.list_assets(Query(keyword="dog")).total, 1)
        self.assertEqual(self.storage.list_assets(Query(sender_id="2")).total, 1)
        self.assertEqual(self.storage.list_assets(Query(source="image")).total, 2)
        self.assertEqual(self.storage.list_assets(Query(source="face")).total, 0)

    def test_pagination(self) -> None:
        for index in range(7):
            self.run_async(
                self.storage.add_bytes(
                    make_png(salt=index + 1), filename=f"f{index}.png", sender_id="1"
                )
            )
        page1 = self.storage.list_assets(Query(page=1, page_size=3))
        page2 = self.storage.list_assets(Query(page=2, page_size=3))
        page3 = self.storage.list_assets(Query(page=3, page_size=3))
        self.assertEqual(page1.total, 7)
        self.assertEqual(len(page1.items), 3)
        self.assertEqual(len(page2.items), 3)
        self.assertEqual(len(page3.items), 1)
        ids = {item.id for item in page1.items + page2.items + page3.items}
        self.assertEqual(len(ids), 7)

    def test_order_modes(self) -> None:
        self.run_async(self.storage.add_bytes(make_png(2, 2), filename="small.png"))
        self.run_async(self.storage.add_bytes(make_png(20, 20), filename="big.png"))
        newest = self.storage.list_assets(Query(order="new"))
        oldest = self.storage.list_assets(Query(order="old"))
        biggest = self.storage.list_assets(Query(order="big"))
        smallest = self.storage.list_assets(Query(order="small"))
        self.assertEqual(biggest.items[0].filename, "big.png")
        self.assertEqual(smallest.items[0].filename, "small.png")
        self.assertNotEqual(newest.items[0].id, oldest.items[0].id)

    def test_delete_removes_file(self) -> None:
        asset = self.run_async(self.storage.add_bytes(make_png(), filename="x.png"))
        assert asset is not None
        path = self.storage.media_path(asset.stored_name)
        self.assertTrue(path.is_file())
        ok = self.run_async(self.storage.delete_asset(asset.id))
        self.assertTrue(ok)
        self.assertFalse(path.exists())
        self.assertEqual(self.storage.list_assets(Query()).total, 0)
        self.assertFalse(self.run_async(self.storage.delete_asset(asset.id)))

    def test_clear(self) -> None:
        for index in range(3):
            self.run_async(
                self.storage.add_bytes(make_png(salt=index + 1), filename=f"c{index}.png")
            )
        deleted = self.run_async(self.storage.clear())
        self.assertEqual(deleted, 3)
        self.assertEqual(self.storage.list_assets(Query()).total, 0)

    def test_media_path_blocks_traversal(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.media_path("../../etc/passwd")

    def test_stats_and_senders(self) -> None:
        self.run_async(
            self.storage.add_bytes(make_png(), filename="a.png", sender_id="1", sender_name="甲")
        )
        self.run_async(
            self.storage.add_bytes(make_gif(), filename="b.gif", sender_id="1", sender_name="甲", duration_ms=1200)
        )
        stats = self.storage.stats()
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["animated_count"], 1)
        self.assertEqual(stats["by_source"].get("image"), 2)
        senders = self.storage.list_senders()
        self.assertEqual(len(senders), 1)
        self.assertEqual(senders[0]["sender_id"], "1")
        self.assertEqual(senders[0]["count"], 2)

    def test_gif_dimensions(self) -> None:
        asset = self.run_async(
            self.storage.add_bytes(make_gif(5, 6), filename="g.gif", source="face")
        )
        assert asset is not None
        self.assertEqual((asset.width, asset.height), (5, 6))
        self.assertEqual(asset.mime, "image/gif")

    def test_mime_sniffed_from_content_not_filename(self) -> None:
        """QQ 表情包地址后缀不可靠：实际是 JPEG 却叫 .gif。

        必须按内容字节判定类型，否则浏览器会按错误 MIME 渲染。
        """
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 40
        asset = self.run_async(
            self.storage.add_bytes(jpeg, filename="mface_abc.gif", source="mface")
        )
        assert asset is not None
        self.assertEqual(asset.mime, "image/jpeg")
        # 落盘后缀应跟随真实类型
        self.assertTrue(asset.stored_name.endswith(".jpg"), asset.stored_name)
        self.assertTrue(self.storage.media_path(asset.stored_name).is_file())

    def test_png_content_with_wrong_extension(self) -> None:
        png = make_png(salt=9)
        asset = self.run_async(
            self.storage.add_bytes(png, filename="weird.bin", source="image")
        )
        assert asset is not None
        self.assertEqual(asset.mime, "image/png")
        self.assertTrue(asset.stored_name.endswith(".png"), asset.stored_name)

    def test_manifest_export(self) -> None:
        self.run_async(self.storage.add_bytes(make_png(), filename="m.png"))
        manifest = self.storage.export_manifest()
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["filename"], "m.png")


if __name__ == "__main__":
    unittest.main(verbosity=2)
