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

"""`board_probe.py` reports whether a drift run's card exists and has finished.

The drift fixtures wait on this before the agent's turn starts, because an
agent left to poll a running card is stopped by the runtime's repetition filter
and reports a card it never saw finish. So the probe's two numbers decide
whether a case grades a finished triage or a half-done one, and a probe that
called a running card finished would put the old failure straight back.

Run as the fixture runs it -- a separate interpreter, insert id on argv, one
line on stdout -- against a board built here, with the URI overridden.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = REPO_ROOT / "bench" / "tf" / "modules" / "drift-inject" / "board_probe.py"
RUN = "eval-drift-policy-1792000000-123"
OTHER_RUN = "eval-drift-policy-1791000000-456"


class BoardProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "kanban.db"

    def tearDown(self):
        self.tmp.cleanup()

    def board(self, rows, with_status=True):
        conn = sqlite3.connect(self.db)
        if with_status:
            conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, status TEXT)")
            conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?)", rows)
        else:
            conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT)")
            conn.executemany("INSERT INTO tasks VALUES (?, ?, ?)", [r[:3] for r in rows])
        conn.commit()
        conn.close()

    def probe(self):
        env = dict(os.environ, DRIFT_BOARD_URI=f"file:{self.db}?mode=ro")
        out = subprocess.run(
            [sys.executable, str(PROBE), RUN], capture_output=True, text=True, env=env
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def card(self, status, run=RUN):
        return ("t_1", "Triage out-of-band change", f"... insertId={run} ...", status)

    def test_no_board_yet_reads_as_nothing_rather_than_a_traceback(self):
        self.assertEqual(self.probe(), "0 0")

    def test_a_running_card_is_found_and_not_finished(self):
        """The state the old fixture handed to the agent, and the one that failed."""
        self.board([self.card("running")])
        self.assertEqual(self.probe(), "1 0")

    def test_a_status_with_a_result_counts_as_finished(self):
        for status in ("done", "blocked", "archived"):
            with self.subTest(status=status):
                if self.db.exists():
                    self.db.unlink()
                self.board([self.card(status)])
                self.assertEqual(self.probe(), "1 1")

    def test_a_non_terminal_status_is_not_finished(self):
        for status in ("todo", "ready", "running", "in_progress"):
            with self.subTest(status=status):
                if self.db.exists():
                    self.db.unlink()
                self.board([self.card(status)])
                self.assertEqual(self.probe(), "1 0")

    def test_a_failed_or_cancelled_card_is_settled_but_not_a_result(self):
        """Terminal, so the wait ends -- but 2, not 1: there is no triage in it.

        Reporting it as finished would hand the agent a card with nothing to
        read, and the prompts look for running, done and blocked, so the agent
        would find nothing or the previous repetition's card.
        """
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                if self.db.exists():
                    self.db.unlink()
                self.board([self.card(status)])
                self.assertEqual(self.probe(), "1 2")

    def test_only_the_newest_matching_card_decides(self):
        """The prompt sends the agent to the newest card, so that is the one waited on."""
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, status TEXT, created_at INTEGER)")
        body = f"... insertId={RUN} ..."
        conn.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
            [("t_old", "Triage", body, "done", 100), ("t_new", "Triage", body, "running", 200)],
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.probe(), "2 0")

    def test_another_runs_finished_card_does_not_satisfy_this_run(self):
        """The board is shared across runs; only this run's insert id may count."""
        self.board([self.card("done", run=OTHER_RUN), self.card("running")])
        self.assertEqual(self.probe(), "1 0")

    def test_a_board_without_a_status_column_says_it_cannot_tell(self):
        """-1, not 0: the fixture must not wait 900s for a column that is not there."""
        self.board([self.card("done")], with_status=False)
        self.assertEqual(self.probe(), "1 -1")


if __name__ == "__main__":
    unittest.main()
