"""视觉标注与语义检索的单元测试（不依赖 AstrBot 运行时）。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import Query, Storage  # noqa: E402
from vision import VisionTag, parse_vision_json  # noqa: E402


def make_png(salt: int = 0) -> bytes:
    import struct
    import zlib

    pixel = bytes((0xFF, (salt * 37) % 256, (salt * 91) % 256))
    raw = b"\x00" + pixel * 4

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class ParseVisionJsonTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        tag = parse_vision_json(
            '{"emotion": "happy", "scene": "日常聊天", '
            '"caption": "一只猫", "keywords": ["猫", "可爱"]}'
        )
        self.assertEqual(tag.emotion, "happy")
        self.assertEqual(tag.scene, "日常聊天")
        self.assertEqual(tag.caption, "一只猫")
        self.assertEqual(tag.keywords, ["猫", "可爱"])

    def test_markdown_fence(self) -> None:
        tag = parse_vision_json(
            '```json\n{"emotion": "sad", "scene": "道歉安慰", '
            '"caption": "哭", "keywords": ["哭"]}\n```'
        )
        self.assertEqual(tag.emotion, "sad")
        self.assertEqual(tag.keywords, ["哭"])

    def test_json_with_extra_text(self) -> None:
        tag = parse_vision_json(
            '好的，我来分析：\n{"emotion": "surprised", "scene": "震惊无语", '
            '"caption": "啊", "keywords": []}\n希望有帮助！'
        )
        self.assertEqual(tag.emotion, "surprised")

    def test_invalid_returns_empty(self) -> None:
        self.assertTrue(parse_vision_json("").is_empty())
        self.assertTrue(parse_vision_json("no json here").is_empty())
        self.assertTrue(parse_vision_json("{broken").is_empty())
        self.assertTrue(parse_vision_json("[1,2,3]").is_empty())

    def test_keywords_as_string(self) -> None:
        tag = parse_vision_json('{"emotion": "x", "keywords": "单个"}')
        self.assertEqual(tag.keywords, ["单个"])

    def test_search_text(self) -> None:
        tag = VisionTag(
            emotion="happy", scene="日常", caption="笑脸", keywords=["笑"]
        )
        text = tag.search_text
        self.assertIn("happy", text)
        self.assertIn("笑脸", text)
        self.assertIn("笑", text)


class SemanticSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = Storage(Path(self._tmp.name))
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def _add(self, salt: int, **kwargs) -> int:
        asset = self.loop.run_until_complete(
            self.storage.add_bytes(make_png(salt), filename=f"a{salt}.png")
        )
        assert asset is not None
        self.loop.run_until_complete(
            self.storage.set_vision_tag(asset.id, **kwargs)
        )
        return asset.id

    def test_search_by_emotion(self) -> None:
        happy_id = self._add(1, emotion="happy", scene="日常聊天", caption="笑得很开心")
        self._add(2, emotion="sad", scene="道歉安慰", caption="哭了")
        results = self.storage.search_by_semantics("happy", 5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, happy_id)

    def test_search_by_chinese_scene(self) -> None:
        self._add(1, emotion="happy", scene="卖萌撒娇", caption="")
        self._add(2, emotion="angry", scene="嘲讽吐槽", caption="")
        results = self.storage.search_by_semantics("嘲讽", 5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].emotion, "angry")

    def test_search_by_keywords(self) -> None:
        self._add(1, emotion="happy", scene="庆祝", caption="", keywords=["生日", "蛋糕"])
        results = self.storage.search_by_semantics("生日", 5)
        self.assertEqual(len(results), 1)

    def test_search_no_match(self) -> None:
        self._add(1, emotion="happy", scene="日常", caption="笑")
        results = self.storage.search_by_semantics("完全不存在的词", 5)
        self.assertEqual(results, [])

    def test_search_skips_unanalyzed(self) -> None:
        asset = self.loop.run_until_complete(
            self.storage.add_bytes(make_png(9), filename="raw.png")
        )
        assert asset is not None
        results = self.storage.search_by_semantics("happy", 5)
        self.assertEqual(results, [])

    def test_list_by_emotion_is_case_insensitive(self) -> None:
        self._add(1, emotion="Happy", scene="日常", caption="")
        results = self.storage.list_by_emotion("happy", 5)
        self.assertEqual(len(results), 1)

    def test_pending_analysis_queue(self) -> None:
        self._add(1, emotion="happy")
        self.loop.run_until_complete(
            self.storage.add_bytes(make_png(7), filename="pending.png")
        )
        pending = self.storage.pending_analysis(10)
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.storage.pending_analysis_count(), 1)

    def test_mark_analysis_failed_removes_from_queue(self) -> None:
        asset = self.loop.run_until_complete(
            self.storage.add_bytes(make_png(8), filename="x.png")
        )
        assert asset is not None
        self.loop.run_until_complete(self.storage.mark_analysis_failed(asset.id))
        self.assertEqual(self.storage.pending_analysis_count(), 0)

    def test_emotion_summary(self) -> None:
        self._add(1, emotion="happy")
        self._add(2, emotion="happy")
        self._add(3, emotion="sad")
        summary = self.storage.emotion_summary()
        counts = {item["emotion"]: item["count"] for item in summary}
        self.assertEqual(counts.get("happy"), 2)
        self.assertEqual(counts.get("sad"), 1)

    def test_filter_by_emotion(self) -> None:
        self._add(1, emotion="happy")
        self._add(2, emotion="sad")
        page = self.storage.list_assets(Query(emotion="happy"))
        self.assertEqual(page.total, 1)
        self.assertEqual(page.items[0].emotion, "happy")

    def test_filter_by_emotion_is_case_insensitive(self) -> None:
        self._add(1, emotion="Happy")
        page = self.storage.list_assets(Query(emotion="happy"))
        self.assertEqual(page.total, 1)

    def test_filter_by_emotion_no_match(self) -> None:
        self._add(1, emotion="happy")
        page = self.storage.list_assets(Query(emotion="angry"))
        self.assertEqual(page.total, 0)

    def test_list_assets_all_analyzed(self) -> None:
        self._add(1, emotion="happy")
        self.loop.run_until_complete(
            self.storage.add_bytes(make_png(6), filename="raw2.png")
        )
        results = self.storage.list_assets_all_analyzed(10)
        self.assertEqual(len(results), 1)

    def test_asset_dict_contains_vision_fields(self) -> None:
        self._add(1, emotion="happy", scene="日常", caption="笑", keywords=["a"])
        page = self.storage.list_assets(Query())
        data = page.items[0].to_dict()
        self.assertEqual(data["emotion"], "happy")
        self.assertEqual(data["scene"], "日常")
        self.assertEqual(data["caption"], "笑")
        self.assertEqual(data["keywords"], ["a"])
        self.assertTrue(data["analyzed"])

    def test_migration_adds_columns_to_old_db(self) -> None:
        """老版本数据库没有视觉字段，初始化时应该自动补上。"""
        import sqlite3

        # 用独立目录，避免和 setUp 里建好的库冲突
        legacy_dir = Path(self._tmp.name) / "legacy"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        db_path = legacy_dir / "assets.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                mime TEXT NOT NULL,
                size INTEGER NOT NULL,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'image',
                sender_id TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                group_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()
        conn.close()

        # 用旧库目录建 Storage，应自动迁移
        migrated = Storage(legacy_dir)
        cols = {
            row["name"]
            for row in migrated._conn.execute("PRAGMA table_info(assets)")
        }
        self.assertIn("emotion", cols)
        self.assertIn("analyzed", cols)
        migrated.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
