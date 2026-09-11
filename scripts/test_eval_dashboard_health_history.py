"""health_history.py appends one JSON Lines record per tick: health.json plus
`tick`, never rewriting what is there."""

import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

from eval_dashboard import health_history

T0 = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)


class Append(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def test_appends_the_object_plus_tick_and_creates_the_file(self):
        history = self.dir / "health-history.jsonl"
        health = {"schema_version": 1, "state": "OUTAGE", "failing_cases": ["a"], "generated_at": "2026-09-08T14:55:00+00:00"}
        health_history.append(history, health, T0)
        health_history.append(history, dict(health, state="GREEN"), T0.replace(minute=15))
        lines = history.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        first, second = (json.loads(line) for line in lines)
        self.assertEqual(first, dict(health, tick="2026-09-08T15:00:00+00:00"))
        self.assertEqual((second["state"], second["tick"]), ("GREEN", "2026-09-08T15:15:00+00:00"))
        self.assertTrue(history.read_text().endswith("\n"))

    def test_a_file_without_a_trailing_newline_is_not_corrupted(self):
        history = self.dir / "h.jsonl"
        history.write_text('{"state":"GREEN"}')
        health_history.append(history, {"state": "DEGRADED"}, T0)
        self.assertEqual([json.loads(l)["state"] for l in history.read_text().splitlines()], ["GREEN", "DEGRADED"])

    def test_main_reads_health_json_and_defaults_the_tick(self):
        health = self.dir / "health.json"
        health.write_text(json.dumps({"state": "GREEN"}))
        history = self.dir / "h.jsonl"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = health_history.main(["--health", str(health), "--history", str(history), "--now", "2026-09-08T15:00:00Z"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(history.read_text())["tick"], "2026-09-08T15:00:00+00:00")
        self.assertIn("appended GREEN", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(health_history.main(["--health", str(self.dir / "missing.json"), "--history", str(history)]), 1)


if __name__ == "__main__":
    unittest.main()
