"""untrusted_text keeps printable text and drops every class the three callers used to drop on their own."""

import unittest

import untrusted_text

KEPT = ["a", "Z", "0", " ", "\t", "\n", "é", "日", "\U0001f600"]
DROPPED_CONTROLS = ["\x00", "\x1b", "\x7f", "\x85", "\x9f"]
DROPPED_INVISIBLE = ["​", "‏", "‪", "‮", "⁠", "⁯", "﻿", "­", "͏", "؜", "᠎"]
DROPPED_SEPARATORS = [" ", " ", "⠀"]
DROPPED_TAGS = ["\U000e0041", "\U000e007f", "\U0010ffff"]


class IsSafeCharTest(unittest.TestCase):
    def test_printable_text_tab_and_newline_are_kept(self):
        for ch in KEPT:
            self.assertTrue(untrusted_text.is_safe_char(ch), repr(ch))

    def test_controls_invisibles_separators_and_tags_are_dropped(self):
        for ch in DROPPED_CONTROLS + DROPPED_INVISIBLE + DROPPED_SEPARATORS + DROPPED_TAGS:
            self.assertFalse(untrusted_text.is_safe_char(ch), repr(ch))


class StripUnsafeTest(unittest.TestCase):
    def test_carriage_returns_ansi_sequences_and_unsafe_characters_go(self):
        dirty = "line\r\n\x1b[31mred\x1b[0m \x9b1mcsi​zero sep\U000e0041tag"
        self.assertEqual(untrusted_text.strip_unsafe(dirty), "line\nred csizerosep" + "tag")

    def test_empty_input_is_empty_output(self):
        self.assertEqual(untrusted_text.strip_unsafe(""), "")
        self.assertEqual(untrusted_text.strip_unsafe(None), "")


if __name__ == "__main__":
    unittest.main()
