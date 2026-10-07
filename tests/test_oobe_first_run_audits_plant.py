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

"""The oobe-first-run-audits plant's in-pod scripts, run against stubs.

`bench/tf/prebuilt/oobe-first-run-audits` arms the first-run audits stage with
arm.py, undoes it with disarm.py, and waits on in_flight.py. Each runs here as it
does in the pod, `python3 - <args> < script`, against a stub `cron.jobs` over a
JSON job store and a stub `hermes` that files and archives cards. Pinned: what the
arm changes and records, that it puts back the `oobe` job only when the image ships
one, that the disarm restores exactly what was there, and which runs count as in
flight. The provisioners' bash is syntax-checked as Terraform renders it.
"""

import json
import os
import pathlib
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
STACK = REPO / "bench" / "tf" / "prebuilt" / "oobe-first-run-audits"
AUDITS = ["compliance-audit", "obtainability-audit", "fleet-wide-cost-analysis", "stockout-prevention"]
OOBE_JOB = {"id": "oobe", "script": "oobe.py", "no_agent": True, "schedule": {"kind": "cron", "expr": "* * * * *"}}
OTHER_JOB = {"id": "profile-cron-tick", "schedule": {"kind": "cron", "expr": "* * * * *"}}

CRON_JOBS_STUB = textwrap.dedent(
    """
    import contextlib, json, os
    STORE = os.environ["STUB_JOB_STORE"]

    @contextlib.contextmanager
    def _jobs_lock():
        yield

    def load_jobs():
        with open(STORE) as fh:
            return json.load(fh)

    def save_jobs(jobs):
        with open(STORE, "w") as fh:
            json.dump(jobs, fh)

    def compute_next_run(schedule):
        return "next:" + schedule["expr"]

    def remove_job(job_id):
        save_jobs([j for j in load_jobs() if j.get("id") != job_id])
    """
)

HERMES_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    log = os.environ["STUB_HERMES_LOG"]
    with open(log, "a") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")
    if sys.argv[1:3] == ["kanban", "create"]:
        n = sum(1 for _ in open(log))
        print("Created\\n" + json.dumps({"id": "t_%d" % n}))
    """
)


class PlantScriptsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.home = root / "data"
        self.home.mkdir()
        stub_pkg = root / "stubs" / "cron"
        stub_pkg.mkdir(parents=True)
        (stub_pkg / "__init__.py").write_text("")
        (stub_pkg / "jobs.py").write_text(CRON_JOBS_STUB)
        self.stubs = root / "stubs"
        self.store = root / "jobs.json"
        self.store.write_text(json.dumps([OTHER_JOB]))
        self.shipped = root / "shipped.json"
        self.shipped.write_text(json.dumps({"jobs": [OTHER_JOB, OOBE_JOB]}))
        self.hermes = root / "hermes"
        self.hermes.write_text(HERMES_STUB)
        self.hermes.chmod(self.hermes.stat().st_mode | stat.S_IXUSR)
        self.hermes_log = root / "hermes.log"
        self.env = {
            **os.environ,
            "PYTHONPATH": str(self.stubs),
            "STUB_JOB_STORE": str(self.store),
            "STUB_HERMES_LOG": str(self.hermes_log),
        }

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-", *args],
            input=(STACK / script).read_text(),
            capture_output=True, text=True, env=self.env, check=False,
        )

    def _arm(self, shipped: pathlib.Path | None = None) -> subprocess.CompletedProcess:
        return self._run("arm.py", str(self.home), str(self.hermes), "20261006200000", str(shipped or self.shipped))

    def _jobs(self) -> list[str]:
        return [j["id"] for j in json.loads(self.store.read_text())]

    def _hermes_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.hermes_log.read_text().splitlines()]

    def _state(self) -> dict:
        return json.loads((self.home / ".bench-oobe.json").read_text())

    # --- arm ------------------------------------------------------------------

    def test_arm_points_the_scan_marker_at_an_archived_sweep_and_files_the_ranking_card_after_it(self):
        (self.home / ".bootstrap_scan_filed").write_text("task_id=t_real\nfiled_at=1\n")
        (self.home / ".oobe_audits_fired").write_text('{"done": true}\n')
        done = self._arm()
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self._hermes_calls()
        creates = [c for c in calls if c[:2] == ["kanban", "create"]]
        archives = [c for c in calls if c[:2] == ["kanban", "archive"]]
        self.assertEqual(len(creates), 2)
        for create in creates:
            self.assertIn("--initial-status", create)
            self.assertNotIn("--assignee", create)
        sweep_key = creates[0][creates[0].index("--idempotency-key") + 1]
        ranking_key = creates[1][creates[1].index("--idempotency-key") + 1]
        self.assertFalse(sweep_key.startswith("bootstrap-inventory-"))
        self.assertTrue(ranking_key.startswith("bootstrap-inventory-prioritize-"))
        state = self._state()
        self.assertEqual([a[2] for a in archives], state["cards"])
        marker = (self.home / ".bootstrap_scan_filed").read_text()
        self.assertTrue(marker.startswith(f"task_id={state['cards'][0]}\nfiled_at="))
        self.assertFalse((self.home / ".oobe_audits_fired").exists())
        self.assertEqual(state["scan_marker"], "task_id=t_real\nfiled_at=1\n")
        self.assertEqual(state["audits_marker"], '{"done": true}\n')

    def test_arm_puts_back_the_shipped_job_with_a_next_run(self):
        done = self._arm()
        self.assertEqual(done.returncode, 0, done.stderr)
        jobs = json.loads(self.store.read_text())
        oobe = next(j for j in jobs if j["id"] == "oobe")
        self.assertEqual(oobe["next_run_at"], "next:* * * * *")
        self.assertTrue(self._state()["job_added"])

    def test_arm_on_an_image_without_the_job_puts_nothing_back(self):
        bare = self.shipped.with_name("bare.json")
        bare.write_text(json.dumps({"jobs": [OTHER_JOB]}))
        done = self._arm(bare)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("oobe", self._jobs())
        self.assertIn("ships no oobe job", done.stdout)
        self.assertFalse(self._state()["job_added"])

    def test_arm_leaves_a_job_already_there(self):
        self.store.write_text(json.dumps([OTHER_JOB, OOBE_JOB]))
        done = self._arm()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self._jobs().count("oobe"), 1)
        self.assertFalse(self._state()["job_added"])

    def test_arm_refuses_when_already_armed(self):
        (self.home / ".bench-oobe.json").write_text("{}")
        done = self._arm()
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(self.hermes_log.exists())

    # --- disarm -----------------------------------------------------------------

    def test_disarm_restores_both_markers_and_removes_the_job_it_added(self):
        (self.home / ".bootstrap_scan_filed").write_text("task_id=t_real\nfiled_at=1\n")
        self.assertEqual(self._arm().returncode, 0)
        done = self._run("disarm.py", str(self.home))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.home / ".bootstrap_scan_filed").read_text(), "task_id=t_real\nfiled_at=1\n")
        self.assertFalse((self.home / ".oobe_audits_fired").exists())
        self.assertNotIn("oobe", self._jobs())
        self.assertFalse((self.home / ".bench-oobe.json").exists())

    def test_disarm_removes_markers_that_were_not_there(self):
        self.assertEqual(self._arm().returncode, 0)
        (self.home / ".oobe_audits_fired").write_text('{"done": true}\n')
        self.assertEqual(self._run("disarm.py", str(self.home)).returncode, 0)
        self.assertFalse((self.home / ".bootstrap_scan_filed").exists())
        self.assertFalse((self.home / ".oobe_audits_fired").exists())

    def test_disarm_keeps_a_job_it_did_not_add(self):
        self.store.write_text(json.dumps([OTHER_JOB, OOBE_JOB]))
        self.assertEqual(self._arm().returncode, 0)
        self.assertEqual(self._run("disarm.py", str(self.home)).returncode, 0)
        self.assertIn("oobe", self._jobs())

    def test_disarm_tolerates_a_job_that_already_removed_itself(self):
        self.assertEqual(self._arm().returncode, 0)
        self.store.write_text(json.dumps([OTHER_JOB]))
        self.assertEqual(self._run("disarm.py", str(self.home)).returncode, 0)

    def test_disarm_with_nothing_armed_changes_nothing(self):
        (self.home / ".bootstrap_scan_filed").write_text("task_id=t_real\n")
        done = self._run("disarm.py", str(self.home))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.home / ".bootstrap_scan_filed").read_text(), "task_id=t_real\n")

    # --- in flight --------------------------------------------------------------

    def test_in_flight_counts_only_running_first_run_audits(self):
        db = self.home / "profiles" / "platform" / "cron"
        db.mkdir(parents=True)
        with sqlite3.connect(db / "executions.db") as con:
            con.execute("CREATE TABLE executions (id TEXT, job_id TEXT, status TEXT, claimed_at TEXT)")
            con.executemany(
                "INSERT INTO executions VALUES (?, ?, ?, ?)",
                [
                    ("1", "compliance-audit", "running", "x"),
                    ("2", "obtainability-audit", "claimed", "x"),
                    ("3", "stockout-prevention", "completed", "x"),
                    ("4", "gce-compute-fleet-audit", "running", "x"),
                ],
            )
        done = self._run("in_flight.py", str(self.home), *AUDITS)
        self.assertEqual(done.stdout.strip(), "2", done.stderr)

    def test_in_flight_with_no_store_is_zero(self):
        self.assertEqual(self._run("in_flight.py", str(self.home), *AUDITS).stdout.strip(), "0")

    # --- oobe ran ----------------------------------------------------------------

    def _ledger(self, rows):
        cron = self.home / "cron"
        cron.mkdir(exist_ok=True)
        with sqlite3.connect(cron / "executions.db") as con:
            con.execute("CREATE TABLE IF NOT EXISTS executions (id TEXT, job_id TEXT, status TEXT, claimed_at TEXT)")
            con.executemany("INSERT INTO executions VALUES (?, ?, ?, ?)", rows)

    def test_oobe_ran_counts_only_runs_that_ended_since_the_arm(self):
        self.assertEqual(self._arm().returncode, 0)
        armed = self._state()["applied_at"]
        self._ledger([
            ("1", "oobe", "completed", "2026-01-01T00:00:00+00:00"),
            ("2", "oobe", "running", "2999-01-01T00:00:00+00:00"),
            ("3", "profile-cron-tick", "completed", "2999-01-01T00:00:00+00:00"),
        ])
        self.assertEqual(self._run("oobe_ran.py", str(self.home)).stdout.strip(), "0")
        self._ledger([("4", "oobe", "completed", armed)])
        self.assertEqual(self._run("oobe_ran.py", str(self.home)).stdout.strip(), "1")

    def test_oobe_ran_with_no_state_prints_nothing(self):
        done = self._run("oobe_ran.py", str(self.home))
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")

    def test_the_wait_is_skipped_on_an_image_without_the_job(self):
        bare = self.shipped.with_name("bare.json")
        bare.write_text(json.dumps({"jobs": [OTHER_JOB]}))
        self.assertIn("ships no oobe job", self._arm(bare).stdout)
        self.assertIn('no_job   = "ships no oobe job"', (STACK / "main.tf").read_text())

    # --- the provisioners -------------------------------------------------------

    def test_the_provisioners_parse_as_bash(self):
        text = (STACK / "main.tf").read_text()
        for script in re.findall(r"command\s+=\s+<<-EOT\n(.*?)\n\s*EOT", text, re.S):
            rendered = re.sub(r"(?<!\$)\$\{[^}]*\}", "X", script).replace("$${", "${")
            done = subprocess.run(["bash", "-n"], input=rendered, capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_stack_names_the_audits_the_stage_starts(self):
        stage = (REPO / "agents" / "chat" / "scripts" / "oobe.py").read_text()
        for audit in AUDITS:
            self.assertIn(f'"{audit}"', stage)
        self.assertIn(" ".join(AUDITS), (STACK / "main.tf").read_text())


if __name__ == "__main__":
    unittest.main()
