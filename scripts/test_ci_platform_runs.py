# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""hack/ci_platform_runs.py, run as wait_platform_runs runs it: `python3 - <args> < script`.

Pinned: which runs and marks hold a unit, that an unreadable store holds it rather than
reading as idle, and that the wait ends when the run does.
"""

import json
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "hack" / "ci_platform_runs.py"
AUDITS = ["fleet-wide-cost-analysis", "compliance-audit", "obtainability-audit", "stockout-prevention"]
MINUTE = 60
# The script's cutoff, read from source so the test follows it.
STALE_SECONDS = eval(re.search(r"^STALE_SECONDS = (.+)$", SCRIPT.read_text(), re.M).group(1))


def ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class PlatformRunsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._tmp.name)
        self.cron = self.home / "profiles" / "platform" / "cron"
        self.cron.mkdir(parents=True)
        self.db = self.cron / "executions.db"

    def tearDown(self):
        self._tmp.cleanup()

    def _rows(self, rows):
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE IF NOT EXISTS executions (id TEXT, job_id TEXT, status TEXT, claimed_at TEXT)")
            con.executemany("INSERT INTO executions VALUES (?, ?, ?, ?)", rows)

    def _roster(self, jobs):
        (self.cron / "jobs.json").write_text(json.dumps({"jobs": jobs}))

    def _wait(self, bound: int = 0, poll: int = 1, audits=AUDITS) -> str:
        done = subprocess.run(
            [sys.executable, "-", str(self.home), str(bound), str(poll), *audits],
            input=SCRIPT.read_text(), capture_output=True, text=True, check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_a_live_run_holds_and_a_finished_stale_or_other_one_does_not(self):
        self._rows(
            [
                ("1", "compliance-audit", "running", ago(STALE_SECONDS - MINUTE)),
                ("2", "obtainability-audit", "claimed", ago(0)),
                ("3", "stockout-prevention", "completed", ago(0)),
                ("4", "gce-compute-fleet-audit", "running", ago(0)),
                # Cut off by a gateway restart: still running, just past the cutoff.
                ("5", "fleet-wide-cost-analysis", "running", ago(STALE_SECONDS + MINUTE)),
            ]
        )
        self.assertEqual(self._wait(), "still going after 0s, the run goes ahead: compliance-audit, obtainability-audit")

    def test_a_mark_not_yet_claimed_holds(self):
        # The next profile-cron-tick starts it, so it is as good as running.
        self._roster(
            [
                {"id": "compliance-audit", "enabled": True, "next_run_at": ago(MINUTE)},
                {"id": "obtainability-audit", "enabled": True, "next_run_at": ago(-60 * MINUTE)},
                {"id": "stockout-prevention", "enabled": False, "next_run_at": ago(MINUTE)},
                {"id": "fleet-wide-cost-analysis", "enabled": True, "state": "paused", "next_run_at": ago(MINUTE)},
                {"id": "gce-compute-fleet-audit", "enabled": True, "next_run_at": ago(MINUTE)},
            ]
        )
        self.assertEqual(self._wait(), "still going after 0s, the run goes ahead: compliance-audit")

    def test_odd_timestamps_read_as_the_tick_reads_them(self):
        # A naive stamp is local time; one that does not parse is due; a missing one is not.
        naive_past = (datetime.now() - timedelta(minutes=1)).replace(microsecond=0).isoformat()
        self._roster(
            [
                {"id": "compliance-audit", "enabled": True, "next_run_at": naive_past},
                {"id": "obtainability-audit", "enabled": True, "next_run_at": "not a time"},
                {"id": "stockout-prevention", "enabled": True},
                {"id": "fleet-wide-cost-analysis", "enabled": True, "next_run_at": None},
            ]
        )
        self._rows([("1", "stockout-prevention", "running", "garbled")])
        self.assertEqual(
            self._wait(),
            "still going after 0s, the run goes ahead: compliance-audit, obtainability-audit, stockout-prevention",
        )

    def test_nothing_on_the_streams_asked_for_goes_straight_through(self):
        self._rows([("1", "compliance-audit", "running", ago(0))])
        self.assertEqual(self._wait(audits=["stockout-prevention"]), "none going")

    def test_no_store_is_nothing_going(self):
        self.assertEqual(self._wait(), "none going")

    def test_an_unreadable_store_holds(self):
        self.db.write_text("not a database")
        self.assertIn("still going after 0s, the run goes ahead: unreadable", self._wait())
        self.db.unlink()
        (self.cron / "jobs.json").write_text("{torn")
        self.assertIn("still going after 0s, the run goes ahead: unreadable", self._wait())

    def test_the_wait_ends_when_the_run_does(self):
        self._rows([("1", "compliance-audit", "running", ago(0))])

        def finish():
            time.sleep(0.5)
            with sqlite3.connect(self.db) as con:
                con.execute("UPDATE executions SET status = 'completed'")

        threading.Thread(target=finish).start()
        self.assertEqual(self._wait(bound=30), "ended after 1s")


if __name__ == "__main__":
    unittest.main()
