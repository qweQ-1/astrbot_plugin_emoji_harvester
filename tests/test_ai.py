"""AI 挑选器（EmojiPicker）的单元测试。

用假 LLM 客户端，不依赖网络与 AstrBot 运行时。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import EmojiPicker, LLMClient, _extract_json, _is_analyzable_size  # noqa: E402
from storage import Query, Storage  # noqa: E402


def make_png(width: int = 100, height: int = 100, salt: int = 0) -> bytes:
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


class FakeClient(LLMClient):
    """按预设脚本返回内容的假客户端。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        super().__init__("http://fake", "key", "fake-model")
        self.responses = responses or []
        self.calls: list[list] = []

    async def chat(self, messages, *, max_tokens=512, temperature=0.7) -> str:
        self.calls.append(messages)
        if not self.responses:
            return ""
        return self.responses.pop(0)


class ExtractJsonTests(unittest.TestCase):
    def test_plain(self) -> None:
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self) -> None:
        self.assertEqual(_extract_json('```json\n{"a": 2}\n```'), {"a": 2})

    def test_with_text(self) -> None:
        self.assertEqual(_extract_json('好的：{"a": 3} 完毕'), {"a": 3})

    def test_nested(self) -> None:
        self.assertEqual(_extract_json('{"a": {"b": 1}}'), {"a": {"b": 1}})

    def test_invalid(self) -> None:
        self.assertEqual(_extract_json("nope"), {})
        self.assertEqual(_extract_json(""), {})
        self.assertEqual(_extract_json("{broken"), {})


class AnalyzableSizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_small_image_rejected(self) -> None:
        path = Path(self._tmp.name) / "small.png"
        path.write_bytes(make_png(20, 20))
        self.assertFalse(_is_analyzable_size(path))

    def test_large_image_accepted(self) -> None:
        path = Path(self._tmp.name) / "big.png"
        path.write_bytes(make_png(200, 200))
        self.assertTrue(_is_analyzable_size(path))

    def test_missing_file(self) -> None:
        self.assertFalse(_is_analyzable_size(Path(self._tmp.name) / "nope.png"))


class PickerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = Storage(Path(self._tmp.name))
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def _add_tagged(self, salt: int, **kwargs) -> int:
        asset = self.loop.run_until_complete(
            self.storage.add_bytes(make_png(200, 200, salt), filename=f"a{salt}.png")
        )
        assert asset is not None
        self.loop.run_until_complete(self.storage.set_vision_tag(asset.id, **kwargs))
        return asset.id

    def test_pick_returns_none_when_no_assets(self) -> None:
        picker = EmojiPicker(self.storage, lambda: FakeClient())
        result = self.loop.run_until_complete(picker.pick("开心"))
        self.assertIsNone(result.asset)
        self.assertFalse(result.should_send)

    def test_pick_uses_llm_choice(self) -> None:
        target = self._add_tagged(1, emotion="happy", scene="庆祝", caption="生日")
        self._add_tagged(2, emotion="sad", scene="道歉", caption="哭了")
        client = FakeClient(['{"id": %d, "reason": "合适"}' % target])
        picker = EmojiPicker(self.storage, lambda: client)
        result = self.loop.run_until_complete(picker.pick("生日快乐"))
        assert result.asset is not None
        self.assertEqual(result.asset.id, target)
        self.assertEqual(result.reason, "合适")

    def test_pick_falls_back_to_random_without_llm(self) -> None:
        self._add_tagged(1, emotion="happy", scene="庆祝", caption="生日")
        picker = EmojiPicker(self.storage, lambda: None)
        result = self.loop.run_until_complete(picker.pick("生日快乐"))
        self.assertIsNotNone(result.asset)
        self.assertIn("随机", result.reason)

    def test_pick_handles_empty_ai_response(self) -> None:
        self._add_tagged(1, emotion="happy", scene="庆祝", caption="生日")
        picker = EmojiPicker(self.storage, lambda: FakeClient(["{}"]))
        result = self.loop.run_until_complete(picker.pick("生日快乐"))
        self.assertIsNone(result.asset)
        self.assertFalse(result.should_send)

    def test_pick_handles_bad_id(self) -> None:
        self._add_tagged(1, emotion="happy", scene="庆祝", caption="生日")
        picker = EmojiPicker(self.storage, lambda: FakeClient(['{"id": "abc"}']))
        result = self.loop.run_until_complete(picker.pick("生日快乐"))
        self.assertIsNone(result.asset)

    def test_pick_handles_llm_exception(self) -> None:
        self._add_tagged(1, emotion="happy", scene="庆祝", caption="生日")

        class BrokenClient(FakeClient):
            async def chat(self, messages, *, max_tokens=512, temperature=0.7):
                raise RuntimeError("boom")

        picker = EmojiPicker(self.storage, lambda: BrokenClient())
        result = self.loop.run_until_complete(picker.pick("生日快乐"))
        self.assertIsNotNone(result.asset)
        self.assertIn("失败", result.reason)

    def test_decide_rejects_when_ai_says_no(self) -> None:
        self._add_tagged(1, emotion="happy", scene="庆祝", caption="生日")
        picker = EmojiPicker(
            self.storage, lambda: FakeClient(['{"send": false, "reason": "不适合"}'])
        )
        result = self.loop.run_until_complete(picker.decide("今天开会讨论财报"))
        self.assertFalse(result.should_send)
        self.assertIsNone(result.asset)
        self.assertIn("不适合", result.reason)

    def test_decide_accepts_and_matches_emotion(self) -> None:
        happy_id = self._add_tagged(1, emotion="happy", scene="庆祝", caption="生日")
        self._add_tagged(2, emotion="sad", scene="道歉", caption="哭了")
        client = FakeClient(
            [
                '{"send": true, "emotion": "happy", "reason": "对方很开心"}',
                '{"id": %d, "reason": "选它"}' % happy_id,
            ]
        )
        picker = EmojiPicker(self.storage, lambda: client)
        result = self.loop.run_until_complete(picker.decide("今天是好日子"))
        self.assertTrue(result.should_send)
        assert result.asset is not None
        self.assertEqual(result.asset.id, happy_id)

    def test_find_candidates_falls_back_to_all_analyzed(self) -> None:
        self._add_tagged(1, emotion="happy", scene="庆祝", caption="生日")
        picker = EmojiPicker(self.storage, lambda: FakeClient())
        candidates = picker.find_candidates("完全无关的词")
        self.assertEqual(len(candidates), 1)

    def test_system_emojis_excluded_from_search(self) -> None:
        self._add_tagged(
            1, emotion="system", scene="QQ系统表情", caption="QQ 自带系统表情"
        )
        picker = EmojiPicker(self.storage, lambda: FakeClient())
        candidates = picker.find_candidates("系统表情")
        self.assertEqual(len(candidates), 0)

    def test_analyze_pending_marks_small_images_as_system(self) -> None:
        asset = self.loop.run_until_complete(
            self.storage.add_bytes(make_png(20, 20), filename="tiny.png")
        )
        assert asset is not None
        client = FakeClient([])  # 不应该被调用
        picker = EmojiPicker(self.storage, lambda: client)
        count = self.loop.run_until_complete(picker.analyze_pending(5))
        self.assertEqual(count, 1)
        self.assertEqual(len(client.calls), 0)
        page = self.storage.list_assets(Query())
        self.assertEqual(page.items[0].emotion, "system")
        self.assertEqual(page.items[0].scene, "QQ系统表情")

    def test_analyze_pending_uses_llm_for_normal_images(self) -> None:
        asset = self.loop.run_until_complete(
            self.storage.add_bytes(make_png(200, 200), filename="normal.png")
        )
        assert asset is not None
        client = FakeClient(
            ['{"emotion": "happy", "scene": "日常", "caption": "笑", "keywords": ["a"]}']
        )
        picker = EmojiPicker(self.storage, lambda: client)
        count = self.loop.run_until_complete(picker.analyze_pending(5))
        self.assertEqual(count, 1)
        self.assertEqual(len(client.calls), 1)
        page = self.storage.list_assets(Query())
        self.assertEqual(page.items[0].emotion, "happy")

    def test_analyze_pending_marks_failed_on_exception(self) -> None:
        asset = self.loop.run_until_complete(
            self.storage.add_bytes(make_png(200, 200), filename="x.png")
        )
        assert asset is not None

        class BrokenClient(FakeClient):
            async def chat(self, messages, *, max_tokens=512, temperature=0.7):
                raise RuntimeError("boom")

        picker = EmojiPicker(self.storage, lambda: BrokenClient())
        count = self.loop.run_until_complete(picker.analyze_pending(5))
        self.assertEqual(count, 0)
        # 失败的图不应反复重试
        self.assertEqual(self.storage.pending_analysis_count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
