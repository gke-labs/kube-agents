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

"""hack/ci_pause_scheduled_audits.py, run as pause_scheduled_audits runs it: `python3 - <home> < script`.

Pinned: which jobs it pauses and which it leaves (plumbing, the oobe stage's audits, a job
already off), that it believes the store over Hermes' exit code, and that ci-eval-pr.sh calls
it once per lease before any unit.
"""

import ast
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "hack" / "ci_pause_scheduled_audits.py"
RUNNER = REPO / "hack" / "ci-eval-pr.sh"
STAGE = REPO / "agents" / "chat" / "scripts" / "oobe.py"
ROSTER_SOURCE = REPO / "agents" / "platform" / "cron" / "jobs.json"

# A stand-in for `hermes -p platform cron pause <id>` that writes what Hermes' pause_job writes.
# FAKE_HERMES_MODE=noop exits 0 and writes nothing; =fail exits 1.
FAKE_HERMES = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    mode = os.environ.get("FAKE_HERMES_MODE", "ok")
    if mode == "fail":
        print("boom", file=sys.stderr)
        sys.exit(1)
    assert sys.argv[1:4] == ["-p", "platform", "cron"] and sys.argv[4] == "pause", sys.argv
    if mode == "noop":
        sys.exit(0)
    path = os.environ["FAKE_ROSTER"]
    store = json.load(open(path))
    for job in store["jobs"]:
        if job["id"] == sys.argv[5]:
            job.update(enabled=False, state="paused", paused_at="2026-10-14T00:00:00+00:00")
    json.dump(store, open(path, "w"))
''')


class PauseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.roster = self.home / "profiles" / "platform" / "cron" / "jobs.json"
        self.roster.parent.mkdir(parents=True)
        self.shipped = self.home / "template-jobs.json"
        self.hermes = self.home / "hermes"
        self.hermes.write_text(FAKE_HERMES.format(python=sys.executable))
        self.hermes.chmod(self.hermes.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.tmp.cleanup()

    def write_roster(self, jobs, shipped=None):
        self.roster.write_text(json.dumps({"jobs": jobs}))
        self.shipped.write_text(json.dumps({"jobs": jobs if shipped is None else shipped}))

    def run_script(self, mode="ok", stage=STAGE):
        env = {
            **os.environ,
            "HERMES_BIN": str(self.hermes),
            "FAKE_ROSTER": str(self.roster),
            "FAKE_HERMES_MODE": mode,
            "OOBE_STAGE_SOURCE": str(stage),
            "PLATFORM_ROSTER_SOURCE": str(self.shipped),
        }
        result = subprocess.run(
            [sys.executable, "-", str(self.home)], input=SCRIPT.read_text(),
            capture_output=True, text=True, env=env, timeout=60,
        )
        store = {j["id"]: j for j in json.loads(self.roster.read_text())["jobs"]}
        return result, store

    def test_the_shipped_roster(self):
        # The real roster and the real stage: every model-run watchdog is paused except the
        # oobe stage's four, and no plumbing job is touched.
        shipped = json.loads(ROSTER_SOURCE.read_text())
        self.write_roster(shipped["jobs"])
        result, store = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        stage = set(ast.literal_eval(re.search(r"FIRST_RUN_AUDITS = (\([^)]*\))", STAGE.read_text(), re.S).group(1)))
        for job in shipped["jobs"]:
            jid, after = job["id"], store[job["id"]]
            if job.get("no_agent") or jid in stage or not job.get("enabled", True):
                self.assertEqual(after, job, jid)
            else:
                self.assertEqual(after["state"], "paused", jid)
                self.assertFalse(after["enabled"], jid)
        self.assertIn("gcp-networking-fabric-audit", result.stdout.split(";")[0])
        self.assertIn("compliance-audit (oobe stage)", result.stdout)

    def test_already_paused_and_disabled_jobs_are_left(self):
        self.write_roster([
            {"id": "a", "enabled": False, "state": "paused", "paused_at": "x"},
            {"id": "b", "enabled": False},
            {"id": "c", "enabled": True},
        ])
        result, store = self.run_script()
        self.assertEqual(result.returncode, 0)
        self.assertIn("paused c;", result.stdout)
        self.assertIn("a (already paused)", result.stdout)
        self.assertIn("b (disabled)", result.stdout)
        self.assertNotIn("paused_at", store["b"])

    def test_only_shipped_jobs_are_paused(self):
        # An id the image doesn't ship (an operator's job, or a stale one from an old roster)
        # is left alone, and no_agent is read from the image's copy.
        live = [{"id": "c", "enabled": True}, {"id": "ed776c6f21b8", "enabled": True},
                {"id": "gc", "enabled": True}]
        result, store = self.run_script_with(live, shipped=[{"id": "c"}, {"id": "gc", "no_agent": True}])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(store["c"]["state"], "paused")
        self.assertTrue(store["ed776c6f21b8"]["enabled"])
        self.assertTrue(store["gc"]["enabled"])
        self.assertIn("ed776c6f21b8 (not shipped)", result.stdout)

    def test_an_unreadable_shipped_roster_pauses_nothing(self):
        self.write_roster([{"id": "c", "enabled": True}])
        self.shipped.unlink()
        result, store = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertTrue(store["c"]["enabled"])

    def run_script_with(self, live, shipped):
        self.write_roster(live, shipped)
        return self.run_script()

    def test_the_store_is_believed_over_the_exit_code(self):
        self.write_roster([{"id": "c", "enabled": True}])
        result, _ = self.run_script(mode="noop")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED c (not paused in the store)", result.stdout)

    def test_a_failed_pause_is_named(self):
        self.write_roster([{"id": "c", "enabled": True}])
        result, _ = self.run_script(mode="fail")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED c (exit 1: boom)", result.stdout)

    def test_an_unreadable_stage_pauses_nothing(self):
        # Without the stage's list it can't tell which audits the stage owns, so it leaves all.
        self.write_roster([{"id": "c", "enabled": True}])
        result, store = self.run_script(stage=self.home / "missing.py")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(store["c"]["enabled"])
        self.assertIn("nothing paused", result.stdout)


class RunnerTest(unittest.TestCase):
    def test_called_once_per_lease_before_any_unit(self):
        text = RUNNER.read_text()
        calls = [m.start() for m in re.finditer(r'^pause_scheduled_audits "lease"$', text, re.M)]
        self.assertEqual(len(calls), 1)
        lease_reset = text.index('reset_agent_pulls "lease"')
        first_unit = re.search(r'^\s+run_one_unit "\$\{TASKS', text, re.M).start()
        self.assertLess(lease_reset, calls[0])
        self.assertLess(calls[0], first_unit)

    def test_the_function_pipes_this_script(self):
        body = RUNNER.read_text().split("pause_scheduled_audits() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('< "${SCRIPT_DIR}/ci_pause_scheduled_audits.py"', body)
        self.assertIn('"gke_${PROJECT_ID}_"*', body)
        self.assertIn("return 0", body)


if __name__ == "__main__":
    unittest.main()
