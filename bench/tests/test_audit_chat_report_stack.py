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

"""The stack bench/tf/prebuilt/audit-chat-report: its scripts, and its apply.

An eval run pauses the clock-started audits before its cases run. `arm.py`
fires a paused job all the same (Hermes' `trigger_job` lifts the pause), records
that it was paused, and pauses it again when its wait is over or the stack is
torn down, so the lane's pause holds for the cases that follow. For a case
that asks for it, a fired run that does not finish in time is not a failure of
the stack. `store.sh` sets the stream's stored report and its digest record
aside, and puts back what the run did not replace. The apply's own script is
run here against stand-ins for `kubectl` and `gcloud`. How the wait and idle
steps read the scheduler's rows, and the fire step's refusal of a disabled
job, are tested in test_audit_chat_report_posted.py.
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_STACK = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "audit-chat-report"
_ARM = _STACK / "arm.py"
_STORE = _STACK / "store.sh"
_CASE = _REPO_ROOT / "bench" / "tasks" / "fleet-audit-scheduled-run-files-the-finding" / "task.yaml"
_AGENT_HOME = "/opt/data"
_MOVED = ".bench-moved"
_MAIN_TF = _STACK / "main.tf"
_VARIABLES_TF = _STACK / "variables.tf"
_UNFINISHED_VARIABLE = "unfinished_run_is_a_result"
_HEREDOC_RE = re.compile(r"command\s*=\s*<<-EOT\n(.*?)\n\s*EOT\n", re.S)
_APPLY_BLOCK = 0
_UNFINISHED_TERNARY_RE = re.compile(r'\$\{var\.' + _UNFINISHED_VARIABLE + r' \? "([^"]*)" : "([^"]*)"\}')
_RUN_WAIT = "5"
_STORE_DIR = "/store"
_DIGEST_FILE = "/digest"
# What Terraform would put in the apply script's other `${...}` places.
_INTERPOLATIONS = {
    "var.project_id": "example-project",
    "var.host_cluster_name": "host",
    "var.host_cluster_location": "us-east4",
    "var.agent_namespace": "kubeagents-system",
    "var.agent_deployment": "platform-agent-gateway",
    "var.agent_container": "platform-agent",
    "var.sandbox_pod": "platform-agent-shell-0",
    "var.sandbox_container": "shell",
    "var.audit_job": "compliance-audit",
    "var.own_wait": "1",
    "var.run_wait": _RUN_WAIT,
    "local.home": _AGENT_HOME,
    "local.python": "python3",
    "local.arm_b64": base64.b64encode(b"arm").decode(),
    "local.store_b64": base64.b64encode(b"store").decode(),
    "local.store": _STORE_DIR,
    "local.digest": _DIGEST_FILE,
    "local.pod_wait": "5",
}
# Stand-ins on PATH. `kubectl` writes the command line it was asked to run in
# the pod, everything after `--`, to $STUB_LOG, and fails the one whose words
# include $STUB_FAIL; `gcloud` succeeds.
_KUBECTL_STUB = """#!/bin/sh
in_pod=""
seen=0
for word in "$@"; do
  if [ "$seen" = 1 ]; then in_pod="$in_pod $word"; fi
  if [ "$word" = "--" ]; then seen=1; fi
done
cat >/dev/null
echo "$in_pod" >> "$STUB_LOG"
case "$in_pod " in *" $STUB_FAIL "*) exit 1 ;; esac
exit 0
"""
_GCLOUD_STUB = "#!/bin/sh\nexit 0\n"
_NO_FAILURE = "no-step-fails"
_JOB = "compliance-audit"
_FIRED_AT = "2026-11-05T06:20:00+00:00"
_CLAIMED_AT = "2026-11-05T06:20:10+00:00"
_STARTED_AT = "2026-11-05T06:20:11+00:00"
_FINISHED_AT = "2026-11-05T06:31:00+00:00"
_PAUSED_JOB = {"id": _JOB, "enabled": False, "state": "paused", "paused_at": "x"}
_RUNNING_JOB = {"id": _JOB, "enabled": True, "state": "scheduled"}


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArmTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        self.arm = _load(_ARM, "audit_chat_report_arm")
        self.cron = self.home / "profiles" / "platform" / "cron"
        self.cron.mkdir(parents=True)

    def _roster(self, job):
        (self.cron / "jobs.json").write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    def _state(self, **fields):
        (self.home / self.arm.STATE_NAME).write_text(
            json.dumps({"job_id": _JOB, "fired_at": _FIRED_AT, **fields}), encoding="utf-8"
        )

    def _read_state(self):
        return json.loads((self.home / self.arm.STATE_NAME).read_text(encoding="utf-8"))

    def _executions(self, *rows):
        conn = sqlite3.connect(self.cron / "executions.db")
        conn.execute("CREATE TABLE executions (job_id, status, claimed_at, started_at, finished_at, delivery_outcome)")
        conn.executemany("INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()

    def _finished_run(self):
        self._executions((_JOB, "completed", _CLAIMED_AT, _STARTED_AT, _FINISHED_AT, "delivered"))

    def test_fire_marks_a_paused_job_due_and_records_that_it_was_paused(self):
        self._roster(_PAUSED_JOB)
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(self.arm.subprocess, "run", return_value=done) as run, contextlib.redirect_stdout(io.StringIO()):
            self.arm.fire(str(self.home), "python3", _JOB)
        self.assertEqual(run.call_args.args[0][-1], _JOB)
        state = self._read_state()
        self.assertTrue(state["was_paused"])
        self.assertFalse(state["repaused"])
        self.assertIn("fired_at", state)

    def test_fire_records_that_a_running_job_was_not_paused(self):
        self._roster(_RUNNING_JOB)
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(self.arm.subprocess, "run", return_value=done), contextlib.redirect_stdout(io.StringIO()):
            self.arm.fire(str(self.home), "python3", _JOB)
        self.assertFalse(self._read_state()["was_paused"])

    def test_wait_pauses_the_job_again_when_the_run_has_ended(self):
        self._roster(_PAUSED_JOB)
        self._state(was_paused=True, repaused=False)
        self._finished_run()
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(self.arm.subprocess, "run", return_value=done) as run, contextlib.redirect_stdout(io.StringIO()):
            self.arm.wait(str(self.home), _JOB, "5")
        self.assertEqual(run.call_args.args[0][-3:], ["cron", "pause", _JOB])
        state = self._read_state()
        self.assertEqual(state["last_run_at"], _FINISHED_AT)
        self.assertEqual(state["delivery_outcome"], "delivered")
        self.assertTrue(state["repaused"])

    def test_wait_returns_the_finished_run_when_the_pause_cannot_be_put_back(self):
        self._roster(_PAUSED_JOB)
        self._state(was_paused=True, repaused=False)
        self._finished_run()
        err, out = io.StringIO(), io.StringIO()
        timed_out = subprocess.TimeoutExpired(cmd=["hermes"], timeout=self.arm.HERMES_TIMEOUT_S)
        with (
            mock.patch.object(self.arm.subprocess, "run", side_effect=timed_out),
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(out),
        ):
            self.arm.wait(str(self.home), _JOB, "5")
        self.assertIn(f"finished at {_FINISHED_AT}: completed, delivered", out.getvalue())
        self.assertIn(f"WARNING: could not pause {_JOB} again", err.getvalue())
        self.assertFalse(self._read_state()["repaused"])

    def test_wait_leaves_the_pause_owed_when_the_roster_reads_un_paused(self):
        """A tick that wrote the store back without the markers: `clean` has to try again."""
        self._roster(_RUNNING_JOB)
        self._state(was_paused=True, repaused=False)
        self._finished_run()
        err = io.StringIO()
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            mock.patch.object(self.arm.subprocess, "run", return_value=done),
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.arm.wait(str(self.home), _JOB, "5")
        self.assertIn(f"{_JOB} reads un-paused after the pause", err.getvalue())
        self.assertFalse(self._read_state()["repaused"])

    def test_wait_leaves_an_unfinished_run_to_the_case_that_asked_for_it(self):
        """No run claimed after the fire: the check finds no report, and the apply goes on."""
        self._roster(_PAUSED_JOB)
        self._state(was_paused=True, repaused=False)
        self._executions()
        err = io.StringIO()
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            mock.patch.object(self.arm.subprocess, "run", return_value=done) as run,
            mock.patch.object(self.arm.time, "sleep"),
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.arm.wait(str(self.home), _JOB, "0", unfinished_is_result=True)
        self.assertIn(f"{_JOB} did not finish a run within 0s", err.getvalue())
        state = self._read_state()
        self.assertNotIn("last_run_at", state)
        self.assertEqual(run.call_args.args[0][-3:], ["cron", "pause", _JOB])
        self.assertTrue(state["repaused"])

    def test_clean_pauses_the_job_again_only_when_that_is_owed(self):
        self._state(was_paused=True, repaused=False)
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(self.arm.subprocess, "run", return_value=done) as run, contextlib.redirect_stdout(io.StringIO()):
            self.arm.clean(str(self.home))
        self.assertEqual(run.call_args.args[0][-3:], ["cron", "pause", _JOB])
        self.assertFalse((self.home / self.arm.STATE_NAME).exists())
        not_owed = {"not paused": {"was_paused": False, "repaused": False}, "paused again": {"was_paused": True, "repaused": True}}
        for name, fields in not_owed.items():
            with self.subTest(state=name):
                self._state(**fields)
                with mock.patch.object(self.arm.subprocess, "run") as run:
                    self.arm.clean(str(self.home))
                run.assert_not_called()
                self.assertFalse((self.home / self.arm.STATE_NAME).exists())



class StoreTest(unittest.TestCase):
    """`store.sh` on a store with a report, its run ring and the digest record."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        self.repo = root / "reports" / _JOB / "owner" / "repo"
        (self.repo / "runs").mkdir(parents=True)
        self.latest = self.repo / "latest.json"
        self.latest.write_text("previous report", encoding="utf-8")
        (self.repo / "runs" / "previous.json").write_text("previous report", encoding="utf-8")
        self.digest = root / "scratch" / f"last_scheduled_digest_{_JOB}.json"
        self.digest.parent.mkdir()
        self.digest.write_text("previous digest", encoding="utf-8")
        self.store = root / "reports" / _JOB

    def _run(self, command):
        subprocess.run(["sh", str(_STORE), command, str(self.store), str(self.digest)], check=True)

    def _moved(self, path):
        return path.with_name(path.name + _MOVED)

    def test_move_sets_the_report_and_the_digest_record_aside(self):
        self._run("move")
        self.assertFalse(self.latest.exists())
        self.assertFalse(self.digest.exists())
        self.assertEqual(self._moved(self.latest).read_text(encoding="utf-8"), "previous report")
        self.assertEqual(self._moved(self.digest).read_text(encoding="utf-8"), "previous digest")

    def test_restore_puts_back_what_the_run_did_not_replace(self):
        self._run("move")
        self._run("restore")
        self.assertEqual(self.latest.read_text(encoding="utf-8"), "previous report")
        self.assertEqual(self.digest.read_text(encoding="utf-8"), "previous digest")
        self.assertFalse(self._moved(self.latest).exists())
        self.assertFalse(self._moved(self.digest).exists())

    def test_restore_keeps_what_the_run_wrote_and_drops_the_copies(self):
        self._run("move")
        self.latest.write_text("this run's report", encoding="utf-8")
        self.digest.write_text("this run's digest", encoding="utf-8")
        self._run("restore")
        self.assertEqual(self.latest.read_text(encoding="utf-8"), "this run's report")
        self.assertEqual(self.digest.read_text(encoding="utf-8"), "this run's digest")
        self.assertFalse(self._moved(self.latest).exists())
        self.assertFalse(self._moved(self.digest).exists())

    def test_a_missing_store_still_moves_and_restores_the_digest_record(self):
        missing = self.store.with_name("absent")
        subprocess.run(["sh", str(_STORE), "move", str(missing), str(self.digest)], check=True)
        self.assertFalse(self.digest.exists())
        subprocess.run(["sh", str(_STORE), "restore", str(missing), str(self.digest)], check=True)
        self.assertEqual(self.digest.read_text(encoding="utf-8"), "previous digest")


class CaseTest(unittest.TestCase):
    """What the scheduled-run case and its stack have to agree on by name."""

    def test_the_scheduled_run_cases_state_file_is_the_arms(self):
        arm = _load(_ARM, "audit_chat_report_arm_for_case")
        declared = re.search(r"^\s+since_state_file: (\S+)$", _CASE.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(declared, "the case names no since_state_file")
        self.assertEqual(declared.group(1), f"{_AGENT_HOME}/{arm.STATE_NAME}")

    def test_the_case_sets_the_unfinished_run_option_the_stack_declares(self):
        self.assertRegex(_CASE.read_text(encoding="utf-8"), rf"(?m)^\s+{_UNFINISHED_VARIABLE}: true$")
        self.assertIn(f'variable "{_UNFINISHED_VARIABLE}"', _VARIABLES_TF.read_text(encoding="utf-8"))


class ApplyTest(unittest.TestCase):
    """The apply's script, rendered as Terraform would and run against the stand-ins."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("kubectl", _KUBECTL_STUB), ("gcloud", _GCLOUD_STUB)):
            stub = bin_dir / name
            stub.write_text(body, encoding="utf-8")
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "in-pod.log"
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "STUB_LOG": str(self.log)}

    def _apply(self, unfinished_is_result, failing_step=_NO_FAILURE):
        """Run the apply script; return its exit status and the in-pod command lines, in order."""
        script = _HEREDOC_RE.findall(_MAIN_TF.read_text(encoding="utf-8"))[_APPLY_BLOCK]
        script = _UNFINISHED_TERNARY_RE.sub(lambda m: m.group(1 if unfinished_is_result else 2), script)
        for name, value in _INTERPOLATIONS.items():
            script = script.replace("${" + name + "}", value)
        self.assertNotRegex(script, r"\$\{(var|local)\.", "the apply script has an interpolation this test does not render")
        done = subprocess.run(
            ["bash", "-c", script], env={**self.env, "STUB_FAIL": failing_step},
            capture_output=True, text=True, check=False,
        )
        lines = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return done.returncode, [line.split() for line in lines]

    def _steps(self, commands):
        """`arm`'s steps are `python3 - <step> ...`; `store`'s are `sh -s <step> ...`."""
        return [words[2] for words in commands]

    def test_an_apply_fires_and_waits_and_cleans_nothing_after(self):
        status, commands = self._apply(unfinished_is_result=False)
        self.assertEqual(status, 0)
        self.assertEqual(self._steps(commands), ["clean", "idle", "move", "fire", "wait"])
        self.assertEqual(commands[-1][3:], [_AGENT_HOME, _INTERPOLATIONS["var.audit_job"], _RUN_WAIT])

    def test_the_unfinished_run_option_reaches_the_wait_step_as_its_word(self):
        """What the apply passes in the pod is what `arm.py` reads as the request."""
        arm = _load(_ARM, "audit_chat_report_arm_for_wiring")
        status, commands = self._apply(unfinished_is_result=True)
        self.assertEqual(status, 0)
        wait_words = commands[-1]
        self.assertEqual(wait_words[2], "wait")
        with mock.patch.object(arm.sys, "argv", ["arm.py", *wait_words[2:]]), mock.patch.object(arm, "wait") as wait:
            arm.main()
        wait.assert_called_once_with(_AGENT_HOME, _INTERPOLATIONS["var.audit_job"], _RUN_WAIT, unfinished_is_result=True)

    def test_a_failed_fire_cleans_the_arm_state_and_restores_the_store(self):
        """The destroy step is skipped on a failed apply, so the exit trap puts the pause and the files back."""
        for failing_step in ("fire", "wait"):
            with self.subTest(failing_step=failing_step):
                self.log.unlink(missing_ok=True)
                status, commands = self._apply(unfinished_is_result=False, failing_step=failing_step)
                self.assertNotEqual(status, 0)
                steps = self._steps(commands)
                self.assertEqual(steps[steps.index(failing_step) + 1:], ["clean", "restore"])
                self.assertEqual(commands[-1][3:], [_STORE_DIR, _DIGEST_FILE])

    def test_a_failure_before_the_fire_restores_the_store_and_cleans_nothing(self):
        status, commands = self._apply(unfinished_is_result=False, failing_step="move")
        self.assertNotEqual(status, 0)
        self.assertEqual(self._steps(commands), ["clean", "idle", "move", "restore"])


if __name__ == "__main__":
    unittest.main()
