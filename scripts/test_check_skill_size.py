#!/usr/bin/env python3
"""Tests for scripts/check_skill_size.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.check_skill_size import HERMES_RESULT_CAP_CHARS, LIMIT_CHARS, REPO, oversized, skill_documents


class TestSkillSize(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel: str, size: int) -> Path:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * size, encoding="utf-8")
        return path

    def test_a_skill_md_over_the_limit_is_reported(self):
        big = self.write("agents/platform/skills/big/SKILL.md", 41)
        self.write("agents/platform/skills/small/SKILL.md", 40)
        self.assertEqual(oversized(self.repo, limit=40), [(big, 41)])

    def test_a_reference_counts_and_a_script_does_not(self):
        self.write("agents/platform/skills/s/SKILL.md", 1)
        ref = self.write("agents/platform/skills/s/references/step.md", 41)
        self.write("agents/platform/skills/s/scripts/README.md", 41)
        self.assertEqual(oversized(self.repo, limit=40), [(ref, 41)])

    def test_every_shipped_root_is_read_and_others_are_not(self):
        shipped = [
            self.write("agents/cluster/skills/a/SKILL.md", 41),
            self.write("a2a/persona/platform/skills/b/SKILL.md", 41),
            self.write("agentplugins/p/files/skills/c/SKILL.md", 41),
        ]
        self.write(".agents/skills/d/SKILL.md", 41)
        self.write("third_party/google-skills/e/SKILL.md", 41)
        self.assertEqual(sorted(p for p, _ in oversized(self.repo, limit=40)), sorted(shipped))

    def test_a_directory_without_skill_md_is_not_a_skill(self):
        self.write("agents/platform/skills/notes/big.md", 41)
        self.assertEqual(skill_documents(self.repo), [])

    def test_the_limit_stays_under_half_of_hermes_cap(self):
        # A document at the limit, wrapped in JSON and opened beside another, must still come back whole.
        self.assertLessEqual(LIMIT_CHARS, HERMES_RESULT_CAP_CHARS // 2)

    def test_the_repository_is_within_the_limit(self):
        self.assertEqual(oversized(REPO), [])


if __name__ == "__main__":
    unittest.main()
