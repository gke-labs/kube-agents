"""The SOP line-number generator: only the pins move, and they move to the truth.

``generate_sop_geography.py`` rewrites two numbers inside each governance cron
prompt of a hand-maintained JSON file. These cases hold it to the properties
that make that safe: the section is measured the way the geography test
measures it (a ``###`` inside a fence is not a heading), a prompt that pins
nothing is left alone, nothing outside the digits changes, an SOP the prompt
cannot cite is refused rather than guessed at, and the committed tree is
already current.

Run:
  python3 -m unittest discover -s scripts -p 'test_generate_sop_geography.py' -v
"""

from __future__ import annotations

import difflib
import re
import tempfile
import unittest
from pathlib import Path

import generate_sop_geography as gen

SOP_NAME = "sample_sop.md"

# A section 2 that opens on line 8 and runs to line 13: the fenced ``### 2.``
# on line 4 is a shell comment and must not count, and the ``### 3.`` on line
# 14 closes it.
SOP = "\n".join(
    [
        "# SOP: Sample",  # 1
        "",  # 2
        "```bash",  # 3
        "### 2. not a heading",  # 4
        "```",  # 5
        "### 1. Enumerate",  # 6
        "text",  # 7
        "### 2. Checks",  # 8
        "",  # 9
        "#### 2.1 A (`a`)",  # 10
        "",  # 11
        "#### 2.2 B (`b`)",  # 12
        "",  # 13
        "### 3. Emit",  # 14
        "done",  # 15
    ]
)
SOP_LINES = 15

ROSTER = """{
  "jobs": [
    {
      "id": "sample-audit",
      "prompt": "Read the SOP at 'governance/sample_sop.md' — all 999 lines of it. Its two checks are section 2, lines 1-2, so read on.",
      "skills": ["fleet-audit"],
      "enabled": true
    },
    {
      "id": "no-pins",
      "prompt": "Read 'governance/sample_sop.md' and do what it says.",
      "skills": [],
      "enabled": false
    }
  ]
}
"""


class RewriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sop_dir = Path(self.tmp.name)
        (self.sop_dir / SOP_NAME).write_text(SOP, encoding="utf-8")

    def test_a_fenced_heading_is_not_a_section(self):
        self.assertEqual((8, 13), gen.section_span(SOP.splitlines(), "2"))

    def test_the_last_section_runs_to_the_end_of_the_file(self):
        self.assertEqual((14, SOP_LINES), gen.section_span(SOP.splitlines(), "3"))

    def test_only_the_digits_change(self):
        new = gen.rewrite_roster(ROSTER, self.sop_dir)
        self.assertIn(f"all {SOP_LINES} lines of it", new)
        self.assertIn("are section 2, lines 8-13", new)
        # Strip every digit from both and the two texts are the same bytes:
        # nothing moved but the numbers, and only inside the prompt.
        self.assertEqual(re.sub(r"\d", "", ROSTER), re.sub(r"\d", "", new))
        changed = [
            line
            for line in difflib.unified_diff(ROSTER.splitlines(), new.splitlines(), n=0)
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
        self.assertEqual(2, len(changed), changed)
        self.assertTrue(all('"prompt"' in line for line in changed), changed)

    def test_a_prompt_without_pins_is_left_alone(self):
        new = gen.rewrite_roster(ROSTER, self.sop_dir)
        self.assertIn("Read 'governance/sample_sop.md' and do what it says.", new)
        self.assertEqual(["sample-audit"], gen.stale_prompts(ROSTER, new))

    def test_a_current_roster_is_a_no_op(self):
        once = gen.rewrite_roster(ROSTER, self.sop_dir)
        self.assertEqual(once, gen.rewrite_roster(once, self.sop_dir))
        self.assertEqual([], gen.stale_prompts(once, once))

    def test_a_section_the_sop_lacks_is_refused(self):
        roster = ROSTER.replace("section 2, lines 1-2", "section 7, lines 1-2")
        with self.assertRaisesRegex(ValueError, "0 sections headed '### 7. '"):
            gen.rewrite_roster(roster, self.sop_dir)

    def test_a_duplicated_section_is_refused(self):
        (self.sop_dir / SOP_NAME).write_text(SOP + "\n### 2. Again\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "2 sections headed '### 2. '"):
            gen.rewrite_roster(ROSTER, self.sop_dir)

    def test_a_prompt_with_only_a_length_pin_is_refused(self):
        roster = ROSTER.replace(" Its two checks are section 2, lines 1-2, so read on.", "")
        with self.assertRaisesRegex(ValueError, "no checks-section span"):
            gen.rewrite_roster(roster, self.sop_dir)

    def test_a_prompt_with_only_a_span_pin_is_refused(self):
        roster = ROSTER.replace(" — all 999 lines of it.", ".")
        with self.assertRaisesRegex(ValueError, "no length"):
            gen.rewrite_roster(roster, self.sop_dir)

    def test_a_pinned_prompt_that_names_no_sop_is_refused(self):
        roster = ROSTER.replace("'governance/sample_sop.md'", "the SOP")
        with self.assertRaisesRegex(ValueError, "does not name"):
            gen.rewrite_roster(roster, self.sop_dir)

    def test_a_missing_sop_is_refused(self):
        roster = ROSTER.replace("sample_sop.md", "gone_sop.md")
        with self.assertRaisesRegex(ValueError, "gone_sop.md"):
            gen.rewrite_roster(roster, self.sop_dir)


class CommittedTreeTest(unittest.TestCase):
    def test_the_committed_roster_is_current(self):
        """An SOP edit without `make docs-generate` fails here, not at 06:20."""
        text = gen.ROSTER.read_text(encoding="utf-8")
        self.assertEqual([], gen.stale_prompts(text, gen.rewrite_roster(text)))

    def test_every_pinned_prompt_names_an_sop_in_the_governance_directory(self):
        text = gen.ROSTER.read_text(encoding="utf-8")
        pinned = [line for line in text.split("\n") if gen.TOTAL_RE.search(line)]
        self.assertTrue(pinned, "no prompt pins an SOP; this test guards nothing")
        for line in pinned:
            ref = gen.SOP_REF_RE.search(line)
            self.assertIsNotNone(ref, line)
            self.assertTrue((gen.SOP_DIR / ref.group(1)).is_file(), ref.group(1))


if __name__ == "__main__":
    unittest.main()
