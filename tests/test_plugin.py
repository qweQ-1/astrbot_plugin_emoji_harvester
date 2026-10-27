"""插件纯逻辑单元测试（不依赖 AstrBot 运行时）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pluginlogic as logic  # noqa: E402


class TargetQqTests(unittest.TestCase):
    def test_from_list(self) -> None:
        self.assertEqual(logic.parse_target_qq(["111", "222"]), ["111", "222"])

    def test_from_comma_string(self) -> None:
        self.assertEqual(
            logic.parse_target_qq("111，222, 333"), ["111", "222", "333"]
        )

    def test_from_semicolon_string(self) -> None:
        self.assertEqual(logic.parse_target_qq("111;222"), ["111", "222"])

    def test_dedupes(self) -> None:
        self.assertEqual(logic.parse_target_qq(["111", "111", "222"]), ["111", "222"])

    def test_strips_whitespace(self) -> None:
        self.assertEqual(logic.parse_target_qq([" 111 ", " 222"]), ["111", "222"])

    def test_empty_inputs(self) -> None:
        self.assertEqual(logic.parse_target_qq(None), [])
        self.assertEqual(logic.parse_target_qq([]), [])
        self.assertEqual(logic.parse_target_qq(""), [])
        self.assertEqual(logic.parse_target_qq(123), [])

    def test_non_string_items_are_stringified(self) -> None:
        self.assertEqual(logic.parse_target_qq([111, 222]), ["111", "222"])


class IsTargetTests(unittest.TestCase):
    def test_match(self) -> None:
        self.assertTrue(logic.is_target_sender("111", ["111", "222"]))

    def test_no_match(self) -> None:
        self.assertFalse(logic.is_target_sender("333", ["111", "222"]))

    def test_empty_sender(self) -> None:
        self.assertFalse(logic.is_target_sender("", ["111"]))

    def test_empty_targets(self) -> None:
        self.assertFalse(logic.is_target_sender("111", []))


class DeriveFilenameTests(unittest.TestCase):
    def test_from_url(self) -> None:
        self.assertEqual(
            logic.derive_filename("https://example.com/path/cat.png?x=1"), "cat.png"
        )

    def test_from_file_ref(self) -> None:
        self.assertEqual(
            logic.derive_filename("", "http://h/a/b/dog.jpg"), "dog.jpg"
        )

    def test_strips_fragment(self) -> None:
        self.assertEqual(
            logic.derive_filename("https://e.com/a.png#frag"), "a.png"
        )

    def test_fallback(self) -> None:
        self.assertEqual(logic.derive_filename("", ""), "image.bin")

    def test_no_extension_falls_back(self) -> None:
        self.assertEqual(logic.derive_filename("https://e.com/path", ""), "image.bin")


class FaceUrlTests(unittest.TestCase):
    def test_numeric_id(self) -> None:
        url = logic.build_face_url(360)
        assert url is not None
        self.assertIn("e360.gif", url)
        self.assertTrue(url.startswith("https://qzonestyle.gtimg.cn/"))

    def test_string_id(self) -> None:
        url = logic.build_face_url("401")
        assert url is not None
        self.assertIn("e401.gif", url)

    def test_rejects_non_numeric(self) -> None:
        self.assertIsNone(logic.build_face_url("abc"))
        self.assertIsNone(logic.build_face_url(None))
        self.assertIsNone(logic.build_face_url(""))

    def test_rejects_negative(self) -> None:
        self.assertIsNone(logic.build_face_url(-1))


class MfaceUrlTests(unittest.TestCase):
    REAL_ID = "27067CAEE52E4C03F3A3A57B7E54B1D4"

    def test_id_regex(self) -> None:
        self.assertTrue(logic.MFACE_ID_RE.match(self.REAL_ID))
        self.assertTrue(logic.MFACE_ID_RE.match(self.REAL_ID.lower()))
        self.assertFalse(logic.MFACE_ID_RE.match("123"))
        self.assertFalse(logic.MFACE_ID_RE.match("zzz" * 11))
        self.assertFalse(logic.MFACE_ID_RE.match(self.REAL_ID + "a"))

    def test_url_contains_uin_and_id(self) -> None:
        url = logic.build_mface_url(self.REAL_ID, "3665585796")
        assert url is not None
        self.assertIn("3665585796", url)
        self.assertIn(self.REAL_ID, url)
        self.assertTrue(url.startswith("https://p.qpic.cn/"))

    def test_requires_uin(self) -> None:
        self.assertIsNone(logic.build_mface_url(self.REAL_ID, ""))

    def test_rejects_bad_id(self) -> None:
        self.assertIsNone(logic.build_mface_url("nope", "3665585796"))
        self.assertIsNone(logic.build_mface_url(None, "3665585796"))


class ClampIntTests(unittest.TestCase):
    def test_clamps_high(self) -> None:
        self.assertEqual(logic.clamp_int(9999, 10, 1, 100), 100)

    def test_clamps_low(self) -> None:
        self.assertEqual(logic.clamp_int(0, 10, 1, 100), 1)

    def test_passes_through(self) -> None:
        self.assertEqual(logic.clamp_int(50, 10, 1, 100), 50)

    def test_invalid_uses_default(self) -> None:
        self.assertEqual(logic.clamp_int("abc", 10, 1, 100), 10)
        self.assertEqual(logic.clamp_int(None, 10, 1, 100), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
