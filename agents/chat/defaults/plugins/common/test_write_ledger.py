"""Tests for the kanban card write ledger (write_ledger.py)."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import write_ledger

OPENED = (
    "Publishing 'fix/replicas' to acme/gitops...\n"
    "Opening a proposal for 'fix/replicas' onto 'main'...\n"
    "PR SUBMITTED SUCCESSFULLY! 🏆 URL: https://github.com/acme/gitops/pull/412\n"
    "https://github.com/acme/gitops/pull/412\n"
)
UPDATED = (
    "A proposal for 'fix/replicas' is already open; updating it in place.\n"
    "PR SUBMITTED SUCCESSFULLY! 🏆 URL: https://github.com/acme/gitops/pull/412\n"
)
RACED = (
    "Opening a proposal for 'fix/replicas' onto 'main'...\n"
    "A proposal for 'fix/replicas' was opened while this run worked; updating it.\n"
    "PR SUBMITTED SUCCESSFULLY! 🏆 URL: https://github.com/acme/gitops/pull/412\n"
)


def terminal(output, exit_code=0):
    return json.dumps({"output": output, "exit_code": exit_code, "error": None})


class WritesInTest(unittest.TestCase):

    def test_an_opened_pull_request(self):
        self.assertEqual(
            write_ledger.writes_in("terminal", terminal(OPENED)),
            [{"kind": "pull_request", "verb": "opened", "url": "https://github.com/acme/gitops/pull/412",
              "repo": "acme/gitops", "number": 412}],
        )

    def test_an_updated_pull_request(self):
        [write] = write_ledger.writes_in("terminal", terminal(UPDATED))
        self.assertEqual(write["verb"], "updated")

    def test_the_nearest_progress_line_decides(self):
        [write] = write_ledger.writes_in("terminal", terminal(RACED))
        self.assertEqual(write["verb"], "updated")

    def test_a_listed_url_is_no_write(self):
        # `prepare` prints an open proposal's URL; a read names it too.
        out = '{"workspace": "/w", "proposal": "https://github.com/acme/gitops/pull/412"}\n'
        self.assertEqual(write_ledger.writes_in("terminal", terminal(out)), [])

    def test_a_url_off_github_or_out_of_grammar_is_not_recorded(self):
        for url in (
            "https://evil.example/acme/gitops/pull/1",
            "https://github.com/acme/gitops/pull/1<!here>",
            "https://github.com/acme/gitops/issues/1",
            "https://github.com/a/b/pull/1/files",
        ):
            out = f"PR SUBMITTED SUCCESSFULLY! 🏆 URL: {url}\n"
            self.assertEqual(write_ledger.writes_in("terminal", terminal(out)), [], url)

    def test_another_tool_or_an_unreadable_result_is_no_write(self):
        self.assertEqual(write_ledger.writes_in("read_file", terminal(OPENED)), [])
        self.assertEqual(write_ledger.writes_in("terminal", "not json"), [])
        self.assertEqual(write_ledger.writes_in("terminal", json.dumps(["x"])), [])
        self.assertEqual(write_ledger.writes_in("terminal", {"output": OPENED})[0]["number"], 412)


class RecordTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.board = self.root / "kanban.db"

    def env(self, **extra):
        values = {"HERMES_KANBAN_DB": str(self.board), "HERMES_KANBAN_TASK": "t_abc123"}
        values.update(extra)
        patcher = mock.patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ledger(self, card="t_abc123"):
        path = self.root / "kanban-writes" / f"{card}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_a_worker_write_lands_in_its_cards_file(self):
        self.env()
        write_ledger.record("terminal", terminal(OPENED))
        [entry] = self.ledger()
        self.assertEqual((entry["card"], entry["verb"], entry["number"]), ("t_abc123", "opened", 412))
        self.assertIsInstance(entry["at"], float)

    def test_outside_a_worker_nothing_is_written(self):
        self.env(HERMES_KANBAN_TASK="")
        write_ledger.record("terminal", terminal(OPENED))
        self.assertFalse((self.root / "kanban-writes").exists())

    def test_a_card_id_outside_the_grammar_writes_nothing(self):
        self.env(HERMES_KANBAN_TASK="../escape")
        write_ledger.record("terminal", terminal(OPENED))
        self.assertFalse((self.root / "kanban-writes").exists())

    def test_the_file_is_capped(self):
        self.env()
        with mock.patch.object(write_ledger, "MAX_ENTRIES_PER_CARD", 2):
            for _ in range(3):
                write_ledger.record("terminal", terminal(OPENED))
        self.assertEqual(len(self.ledger()), 2)

    def test_a_failed_write_never_raises(self):
        self.env()
        failing = mock.patch.object(write_ledger.os, "makedirs", side_effect=OSError("read-only"))
        with failing, self.assertLogs("hermes.plugin.write_ledger", level="ERROR"):
            write_ledger.record("terminal", terminal(OPENED))


if __name__ == "__main__":
    unittest.main()
