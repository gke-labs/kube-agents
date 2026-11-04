#!/usr/bin/env python3
"""Tests for scripts/coverage_shard_attempts.py."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.coverage_shard_attempts import DATA_FILE, FAILED_FILE, keep_latest_attempts  # noqa: E402

SCRIPT = _REPO_ROOT / "scripts" / "coverage_shard_attempts.py"
#: What a shard's failing-file list holds when it went red, and when it went green.
RED = "tests/test_fleet_fixture_state.py"
GREEN = ""


class KeepLatestAttemptsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def shard(self, index: int, attempt: int, failed: str = GREEN) -> Path:
        path = self.root / f"python-coverage-shard-{index}-attempt-{attempt}"
        path.mkdir()
        (path / DATA_FILE).write_text(f"data {index}/{attempt}")
        (path / FAILED_FILE).write_text(failed)
        return path

    def names(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir())

    def failed(self, index: int) -> str:
        return (self.root / f"python-coverage-shard-{index}" / FAILED_FILE).read_text()

    def test_a_rerun_shard_replaces_its_red_attempt(self):
        """Run 14418: shard 2 went red on attempt 1 and green on attempt 2.
        The join on attempt 2 must read attempt 2's list, not attempt 1's."""
        self.shard(1, 1)
        self.shard(2, 1, RED)
        self.shard(2, 2)
        self.shard(3, 1)
        keep_latest_attempts(self.root)
        self.assertEqual(self.names(), [f"python-coverage-shard-{i}" for i in (1, 2, 3)])
        self.assertEqual(self.failed(2), GREEN)
        self.assertEqual((self.root / "python-coverage-shard-2" / DATA_FILE).read_text(), "data 2/2")

    def test_attempts_compare_as_numbers(self):
        self.shard(1, 9, RED)
        self.shard(1, 10)
        keep_latest_attempts(self.root)
        self.assertEqual(self.failed(1), GREEN)

    def test_a_red_latest_attempt_stays_red(self):
        self.shard(1, 1)
        self.shard(1, 2, RED)
        keep_latest_attempts(self.root)
        self.assertEqual(self.failed(1), RED)

    def test_one_attempt_per_shard_is_renamed_unchanged(self):
        for index in (1, 2, 3):
            self.shard(index, 1)
        keep_latest_attempts(self.root)
        self.assertEqual(self.names(), [f"python-coverage-shard-{i}" for i in (1, 2, 3)])

    def test_a_shard_directory_without_an_attempt_is_refused(self):
        """A name the shards no longer upload means the workflow and this script
        disagree; joining it could read stale data, so the join fails closed."""
        self.shard(1, 1)
        (self.root / "python-coverage-shard-2").mkdir()
        with self.assertRaises(ValueError):
            keep_latest_attempts(self.root)

    def test_a_missing_directory_is_refused(self):
        with self.assertRaises(FileNotFoundError):
            keep_latest_attempts(self.root / "absent")

    def test_other_entries_are_left_alone(self):
        self.shard(1, 1)
        (self.root / FAILED_FILE).write_text("")
        keep_latest_attempts(self.root)
        self.assertEqual(self.names(), [FAILED_FILE, "python-coverage-shard-1"])

    def test_the_command_line_exits_non_zero_on_a_refusal(self):
        (self.root / "python-coverage-shard-1").mkdir()
        done = subprocess.run([sys.executable, str(SCRIPT), str(self.root)], capture_output=True, text=True, check=False)
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("python-coverage-shard-1", done.stderr)

    def test_the_command_line_names_the_attempt_it_dropped(self):
        self.shard(1, 1, RED)
        self.shard(1, 2)
        done = subprocess.run([sys.executable, str(SCRIPT), str(self.root)], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("python-coverage-shard-1-attempt-1", done.stdout)
        self.assertEqual(self.failed(1), GREEN)


if __name__ == "__main__":
    unittest.main()
