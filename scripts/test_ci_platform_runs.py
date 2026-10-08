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

Pinned: which runs, marks and pending `oobe` stage audits hold a unit, that a failed read
holds it rather than reading as idle, that the wait ends when the run does, and that
wait_platform_runs tries a failed exec again inside its bound.
"""

import json
import os
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

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "hack" / "ci_platform_runs.py"
RUNNER = REPO / "hack" / "ci-eval-pr.sh"
STAGE = REPO / "agents" / "chat" / "scripts" / "oobe.py"
AUDITS = ["fleet-wide-cost-analysis", "compliance-audit", "obtainability-audit", "stockout-prevention"]
MINUTE = 60
FINISH_AFTER_SECONDS = 3
# The script's cutoff, read from source so the test follows it.
STALE_SECONDS = eval(re.search(r"^STALE_SECONDS = (.+)$", SCRIPT.read_text(), re.M).group(1))


# The runner's own readonly lines the function reads, lifted so a rename there fails here; each
# test then overrides the wait and poll so it runs in seconds.
RUNNER_CONSTANTS = (
    "EVAL_SANDBOX_EXEC_TIMEOUT",
    "EVAL_SANDBOX_EXEC_ROUND_TRIP_SECONDS",
    "EVAL_GATEWAY_CONTAINER",
    "EVAL_GATEWAY_PYTHON",
    "EVAL_GATEWAY_HOME",
    "EVAL_PLATFORM_RUN_WAIT_SECONDS",
    "EVAL_PLATFORM_RUN_POLL_SECONDS",
)


def runner_constants() -> list[str]:
    text = RUNNER.read_text()
    lines = [re.search(rf"^readonly {name}=.*$", text, re.M).group(0).replace("readonly ", "", 1) for name in RUNNER_CONSTANTS]
    return lines


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
            env={**os.environ, "OOBE_STAGE_SOURCE": str(STAGE)},
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
            # Well past the child's start-up, so its first read sees the run going.
            time.sleep(FINISH_AFTER_SECONDS)
            with sqlite3.connect(self.db) as con:
                con.execute("UPDATE executions SET status = 'completed'")

        threading.Thread(target=finish).start()
        self.assertRegex(self._wait(bound=30), r"^ended after \d+s$")


    # --- an oobe stage the eval stack left armed ----------------------------

    def _stage(self, state=None, job=True, armed=True, oobe=None):
        (self.home / "cron").mkdir(exist_ok=True)
        if armed:
            (self.home / ".bench-oobe.json").write_text("{}")
        jobs = [oobe or {"id": "oobe"}] if job else []
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs + [{"id": "bootstrap-inventory-scan"}]}))
        if state is not None:
            (self.home / ".oobe_audits_fired").write_text(json.dumps(state))

    def test_a_fresh_installs_own_stage_holds_only_the_audit_it_marks_next(self):
        # Still waiting on its scan: nothing held. Under way: only the next audit.
        self._stage(armed=False)
        self.assertEqual(self._wait(), "none going")
        self._stage({"fired": [], "current": None, "at": 1}, armed=False)
        self.assertEqual(self._wait(), "still going after 0s, the run goes ahead: fleet-wide-cost-analysis (oobe stage)")
        self._stage({"fired": ["fleet-wide-cost-analysis"], "current": None}, armed=False)
        self.assertEqual(self._wait(), "still going after 0s, the run goes ahead: compliance-audit (oobe stage)")

    def test_while_an_audit_awaits_its_run_the_one_after_it_is_next(self):
        # The state the stage keeps from cost's mark to compliance's, with no mark in the store
        # yet: cost is held (recorded before the store has it) and so is compliance.
        self._stage({"fired": ["fleet-wide-cost-analysis"], "current": {"job": "fleet-wide-cost-analysis", "marked_at": 1}}, armed=False)
        self.assertEqual(
            self._wait(),
            "still going after 0s, the run goes ahead: compliance-audit (oobe stage), fleet-wide-cost-analysis (oobe stage)",
        )

    def test_a_roster_whose_jobs_are_not_a_list_holds(self):
        self._stage({"fired": []}, armed=False)
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": {"oobe": {}}}))
        self.assertIn("still going after 0s, the run goes ahead: unreadable", self._wait())

    def test_a_paused_or_disabled_stage_job_holds_nothing(self):
        for job in ({"id": "oobe", "enabled": False}, {"id": "oobe", "state": "paused"}, {"id": "oobe", "paused_at": "x"}):
            with self.subTest(job=job):
                self._stage(oobe=job)
                self.assertEqual(self._wait(), "none going")

    def test_an_audit_the_stage_gave_up_on_no_longer_holds(self):
        self._stage({"fired": [], "gave_up": ["compliance-audit"], "current": None})
        self.assertEqual(self._wait(audits=["compliance-audit"]), "none going")

    def test_a_stage_not_yet_started_holds_every_audit_it_runs(self):
        self._stage()
        self.assertEqual(
            self._wait(audits=["compliance-audit", "gce-compute-fleet-audit"]),
            "still going after 0s, the run goes ahead: compliance-audit (oobe stage)",
        )

    def test_an_audit_the_stage_has_marked_and_left_no_longer_holds(self):
        # Cost's run has ended (current cleared), compliance held: only the two it has still
        # to mark hold.
        self._stage({"fired": ["fleet-wide-cost-analysis"], "held": {"compliance-audit": "disabled"}, "current": None})
        self.assertEqual(
            self._wait(),
            "still going after 0s, the run goes ahead: obtainability-audit (oobe stage), stockout-prevention (oobe stage)",
        )

    def test_an_armed_stage_holds_every_audit_it_has_still_to_mark(self):
        self._stage({"fired": ["fleet-wide-cost-analysis"], "current": {"job": "fleet-wide-cost-analysis", "marked_at": 1}})
        self.assertEqual(
            self._wait(),
            "still going after 0s, the run goes ahead: compliance-audit (oobe stage), fleet-wide-cost-analysis (oobe stage), "
            "obtainability-audit (oobe stage), stockout-prevention (oobe stage)",
        )

    def test_a_done_or_absent_stage_holds_nothing(self):
        self._stage({"done": True})
        self.assertEqual(self._wait(), "none going")
        self._stage(job=False)
        (self.home / ".oobe_audits_fired").unlink()
        self.assertEqual(self._wait(), "none going")

    def test_a_corrupt_stage_marker_holds(self):
        self._stage()
        (self.home / ".oobe_audits_fired").write_text("{torn")
        self.assertIn("still going after 0s, the run goes ahead: unreadable", self._wait())

    # --- the runner's call -----------------------------------------------------

    def test_a_failed_exec_is_tried_again_inside_the_bound(self):
        fn = re.search(r"^wait_platform_runs\(\) \{.*?^\}$", RUNNER.read_text(), re.S | re.M).group(0)
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        count = self.home / "count"
        kubectl = bin_dir / "kubectl"
        # Fails twice, then answers as the script would.
        kubectl.write_text(
            f'#!/bin/sh\ncat > /dev/null\nn=$(($(cat "{count}" 2>/dev/null || echo 0) + 1))\necho "$n" > "{count}"\n'
            '[ "$n" -ge 3 ] && { echo "none going"; exit 0; }\necho "error: pod not running"; exit 1\n'
        )
        kubectl.chmod(0o755)
        body = "\n".join(
            [
                "set -euo pipefail",
                f'SCRIPT_DIR="{REPO / "hack"}"',
                *runner_constants(),
                "EVAL_PLATFORM_RUN_WAIT_SECONDS=60 EVAL_PLATFORM_RUN_POLL_SECONDS=0",
                "PROJECT_ID=proj AGENT_CLUSTER_CONTEXT=gke_proj_us_c TARGET_NAMESPACE=ns AGENT_SERVICE_NAME=platform-agent",
                fn,
                'wait_platform_runs "case rep 1" "compliance-audit"',
            ]
        )
        done = subprocess.run(
            ["bash", "-c", body], capture_output=True, text=True, check=False,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(count.read_text().strip(), "3")
        self.assertEqual(done.stderr.count("trying again"), 2, done.stderr)
        self.assertIn("Platform runs (case rep 1) on compliance-audit: none going", done.stdout)

    def test_an_exec_that_keeps_failing_stops_at_the_bound(self):
        fn = re.search(r"^wait_platform_runs\(\) \{.*?^\}$", RUNNER.read_text(), re.S | re.M).group(0)
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        kubectl = bin_dir / "kubectl"
        kubectl.write_text('#!/bin/sh\ncat > /dev/null\necho "error: forbidden"\nexit 1\n')
        kubectl.chmod(0o755)
        body = "\n".join(
            [
                "set -euo pipefail",
                f'SCRIPT_DIR="{REPO / "hack"}"',
                *runner_constants(),
                "EVAL_PLATFORM_RUN_WAIT_SECONDS=2 EVAL_PLATFORM_RUN_POLL_SECONDS=1",
                "PROJECT_ID=proj AGENT_CLUSTER_CONTEXT=gke_proj_us_c TARGET_NAMESPACE=ns AGENT_SERVICE_NAME=platform-agent",
                fn,
                'wait_platform_runs "case rep 1" "compliance-audit"',
                'echo "returned $?"',
            ]
        )
        done = subprocess.run(
            ["bash", "-c", body], capture_output=True, text=True, check=False, timeout=60,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        )
        self.assertIn("returned 0", done.stdout, done.stderr)
        self.assertIn("stopped waiting on compliance-audit after 2s", done.stderr)


if __name__ == "__main__":
    unittest.main()
