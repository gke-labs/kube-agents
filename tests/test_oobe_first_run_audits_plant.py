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
arm.py and undoes it with disarm.py. Each runs here as it
does in the pod, `python3 - <args> < script`, against a stub `cron.jobs` over a
JSON job store and a stub `hermes` that files and archives cards. Pinned: what the
arm changes and records, that it puts back the `oobe` job only when the image ships
one, and that the disarm restores exactly what was there. The provisioners' bash
is syntax-checked as Terraform renders it, and the teardown is run against failing
stubs.
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
STACK = REPO / "bench" / "tf" / "prebuilt" / "oobe-first-run-audits"
OOBE_JOB = {"id": "oobe", "script": "oobe.py", "no_agent": True, "schedule": {"kind": "cron", "expr": "* * * * *"}}
OTHER_JOB = {"id": "profile-cron-tick", "schedule": {"kind": "cron", "expr": "* * * * *"}}
STAGE_SOURCE = REPO / "agents" / "chat" / "scripts" / "oobe.py"
STAGE_FIVE = ast.literal_eval(re.search(r"FIRST_RUN_AUDITS = (\([^)]*\))", STAGE_SOURCE.read_text(), re.S).group(1))
PAUSED = {"enabled": False, "state": "paused", "paused_at": "2026-11-12T00:00:00+00:00"}
HEREDOC = re.compile(r"command\s+=\s+<<-EOT\n(.*?)\n\s*EOT", re.S)


def render(script: str) -> str:
    """A provisioner's bash as Terraform hands it over, every interpolation a placeholder."""
    return re.sub(r"(?<!\$)\$\{[^}]*\}", "X", script).replace("$${", "${")


def run_script_in_process(path: pathlib.Path, args: list[str], env: dict, extra_path: str | None = None) -> subprocess.CompletedProcess:
    """Run a script the way `python3 - <args> < script` does, in this process so coverage sees it.

    Its argv, environment and stdout/stderr are its own for the call, and a ``cron`` stub on
    ``extra_path`` is imported fresh. SystemExit becomes the return code, as the CLI's would be.
    """
    import contextlib
    import io
    import runpy

    saved_argv, saved_env, saved_path = sys.argv, dict(os.environ), list(sys.path)
    saved_mods = {name: mod for name, mod in sys.modules.items() if name == "cron" or name.startswith("cron.")}
    for name in saved_mods:
        del sys.modules[name]
    out, err, code = io.StringIO(), io.StringIO(), 0
    try:
        sys.argv = ["-", *args]
        os.environ.clear()
        os.environ.update(env)
        if extra_path:
            sys.path.insert(0, extra_path)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                runpy.run_path(str(path), run_name="__main__")
            except SystemExit as exc:
                if isinstance(exc.code, str):
                    err.write(exc.code + "\n")
                    code = 1
                else:
                    code = exc.code or 0
            except Exception:  # noqa: BLE001 - the interpreter would print it and exit 1
                import traceback

                traceback.print_exc(file=err)
                code = 1
    finally:
        sys.argv = saved_argv
        os.environ.clear()
        os.environ.update(saved_env)
        sys.path[:] = saved_path
        for name in [n for n in sys.modules if n == "cron" or n.startswith("cron.")]:
            del sys.modules[name]
        sys.modules.update(saved_mods)
    return subprocess.CompletedProcess(["-", *args], code, out.getvalue(), err.getvalue())

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

    # The pinned Hermes' contract (tests/test_bootstrap_ranking_plant.py, is_job_runnable).
    def is_job_runnable(job):
        return job.get("enabled", True) and job.get("state") != "paused" and not job.get("paused_at")
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
    if sys.argv[1:5] == ["-p", "platform", "cron", "resume"]:
        path = os.environ["STUB_PLATFORM_ROSTER"]
        store = json.load(open(path))
        late = os.environ.get("STUB_LATE_PAUSE", "")
        for job in store["jobs"]:
            if job["id"] == sys.argv[5]:
                job.update(enabled=True, state="scheduled", paused_at=None)
            elif job["id"] == late and not os.path.exists(path + ".late"):
                # A pause the eval run decided before the arm, landing after its first sweep.
                job.update(enabled=False, state="paused", paused_at="x")
                open(path + ".late", "w").close()
        json.dump(store, open(path, "w"))
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
        self.platform_roster = self.home / "profiles" / "platform" / "cron" / "jobs.json"
        self.platform_roster.parent.mkdir(parents=True)
        self.platform_roster.write_text(json.dumps({"jobs": [{"id": a, "enabled": True} for a in STAGE_FIVE]}))
        self.env = {
            **os.environ,
            "PYTHONPATH": str(self.stubs),
            "STUB_JOB_STORE": str(self.store),
            "STUB_HERMES_LOG": str(self.hermes_log),
            "STUB_PLATFORM_ROSTER": str(self.platform_roster),
            "OOBE_STAGE_SOURCE": str(STAGE_SOURCE),
            "OOBE_ARM_RESUME_SETTLE_SECONDS": "0",
        }

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return run_script_in_process(STACK / script, list(args), self.env, str(self.stubs))

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
        # The hand-off's record of the stand-in ranking card, which is all the stage reads it from.
        handoff = (self.home / ".bootstrap_handoff_filed").read_text()
        self.assertTrue(handoff.startswith(f"sweep={state['cards'][0]}\ntask_id={state['cards'][1]}\nfiled_at="))
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

    def test_arm_refuses_a_paused_or_disabled_job_and_changes_nothing(self):
        for job in ({**OOBE_JOB, "enabled": False}, {**OOBE_JOB, "state": "paused"}, {**OOBE_JOB, "paused_at": "x"}):
            with self.subTest(job=job):
                self.store.write_text(json.dumps([OTHER_JOB, job]))
                done = self._arm()
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("paused or disabled", done.stderr)
                self.assertFalse((self.home / ".bench-oobe.json").exists())
                self.assertFalse((self.home / ".bootstrap_scan_filed").exists())

    def _platform(self, jobs, recorded):
        self.platform_roster.write_text(json.dumps({"jobs": jobs}))
        (self.home / ".ci-paused-audits.json").write_text(json.dumps(recorded))

    def _platform_paused(self) -> set[str]:
        return {j["id"] for j in json.loads(self.platform_roster.read_text())["jobs"] if j.get("paused_at")}

    def test_arm_resumes_the_first_run_audits_the_eval_run_paused(self):
        first, second = STAGE_FIVE[:2]
        self._platform(
            [{"id": a, **PAUSED} for a in STAGE_FIVE] + [{"id": "other-audit", **PAUSED}],
            # The eval run paused all six; an operator paused nothing here.
            [*STAGE_FIVE, "other-audit"],
        )
        done = self._arm()
        self.assertEqual(done.returncode, 0, done.stderr)
        # The five the chain needs, and not the audit the eval run paused for #3020.
        self.assertEqual(self._platform_paused(), {"other-audit"})
        self.assertEqual(self._state()["resumed"], list(STAGE_FIVE))
        resumes = [c for c in self._hermes_calls() if c[:4] == ["-p", "platform", "cron", "resume"]]
        self.assertEqual([c[4] for c in resumes], list(STAGE_FIVE))
        self.assertIn("resumed " + first, done.stdout)
        self.assertNotIn(second + ", " + second, done.stdout)

    def test_arm_leaves_a_pause_the_eval_run_did_not_record(self):
        first = STAGE_FIVE[0]
        self._platform([{"id": first, **PAUSED}] + [{"id": a, "enabled": True} for a in STAGE_FIVE[1:]], [])
        done = self._arm()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self._platform_paused(), {first})
        self.assertFalse(any(c[:4] == ["-p", "platform", "cron", "resume"] for c in self._hermes_calls()))

    def test_arm_without_an_eval_record_resumes_nothing(self):
        self.platform_roster.write_text(json.dumps({"jobs": [{"id": a, **PAUSED} for a in STAGE_FIVE]}))
        done = self._arm()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self._platform_paused(), set(STAGE_FIVE))

    def test_arm_resumes_a_pause_that_lands_after_its_first_sweep(self):
        first, late = STAGE_FIVE[0], STAGE_FIVE[3]
        self._platform([{"id": first, **PAUSED}] + [{"id": a, "enabled": True} for a in STAGE_FIVE[1:]], list(STAGE_FIVE))
        self.env["STUB_LATE_PAUSE"] = late
        done = self._arm()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self._platform_paused(), set())
        self.assertIn(late, done.stdout)

    def test_arm_sweeps_again_even_when_the_first_sweep_found_nothing(self):
        late = STAGE_FIVE[0]
        self._platform([{"id": a, "enabled": True} for a in STAGE_FIVE], list(STAGE_FIVE))
        # The late pause lands during the settle, after a first sweep with nothing to resume.
        self.env["OOBE_ARM_RESUME_SETTLE_SECONDS"] = "2"
        stub = self.home / "late.py"
        stub.write_text(
            "import json, sys, time\n"
            f"time.sleep(1)\np = {str(self.platform_roster)!r}\n"
            "s = json.load(open(p))\n"
            f"[j.update(enabled=False, state='paused', paused_at='x') for j in s['jobs'] if j['id'] == {late!r}]\n"
            "json.dump(s, open(p, 'w'))\n"
        )
        lander = subprocess.Popen([sys.executable, str(stub)])
        try:
            done = self._arm()
        finally:
            lander.wait(timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self._platform_paused(), set())

    def test_arm_resumes_before_it_touches_the_stage(self):
        # The late pause is resumed by the second sweep, and that too comes before any card.
        first, late = STAGE_FIVE[0], STAGE_FIVE[3]
        self._platform([{"id": first, **PAUSED}] + [{"id": a, "enabled": True} for a in STAGE_FIVE[1:]], list(STAGE_FIVE))
        self.env["STUB_LATE_PAUSE"] = late
        done = self._arm()
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self._hermes_calls()
        self.assertIn(["-p", "platform", "cron", "resume", late], calls)
        last_resume = max(i for i, c in enumerate(calls) if c[:4] == ["-p", "platform", "cron", "resume"])
        first_card = min(i for i, c in enumerate(calls) if c[:2] == ["kanban", "create"])
        self.assertLess(last_resume, first_card)

    def test_arm_stops_when_a_resume_fails(self):
        self._platform([{"id": a, **PAUSED} for a in STAGE_FIVE], list(STAGE_FIVE))
        self.hermes.write_text("#!/bin/sh\necho nope >&2\nexit 1\n")
        done = self._arm()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("could not resume", done.stderr)
        # The state file is there, so the stack's exit trap disarms.
        self.assertTrue((self.home / ".bench-oobe.json").exists())

    def test_arm_refuses_when_already_armed(self):
        (self.home / ".bench-oobe.json").write_text("{}")
        done = self._arm()
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(self.hermes_log.exists())

    # --- disarm -----------------------------------------------------------------

    def test_disarm_restores_the_markers_and_removes_the_job_it_added(self):
        (self.home / ".bootstrap_scan_filed").write_text("task_id=t_real\nfiled_at=1\n")
        (self.home / ".bootstrap_handoff_filed").write_text("sweep=t_real\ntask_id=t_rank\nfiled_at=2\n")
        self.assertEqual(self._arm().returncode, 0)
        done = self._run("disarm.py", str(self.home), str(self.hermes))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.home / ".bootstrap_scan_filed").read_text(), "task_id=t_real\nfiled_at=1\n")
        self.assertEqual((self.home / ".bootstrap_handoff_filed").read_text(), "sweep=t_real\ntask_id=t_rank\nfiled_at=2\n")
        self.assertFalse((self.home / ".oobe_audits_fired").exists())
        self.assertNotIn("oobe", self._jobs())
        self.assertFalse((self.home / ".bench-oobe.json").exists())

    def test_disarm_removes_markers_that_were_not_there(self):
        self.assertEqual(self._arm().returncode, 0)
        (self.home / ".oobe_audits_fired").write_text('{"done": true}\n')
        self.assertEqual(self._run("disarm.py", str(self.home), str(self.hermes)).returncode, 0)
        self.assertFalse((self.home / ".bootstrap_scan_filed").exists())
        self.assertFalse((self.home / ".bootstrap_handoff_filed").exists())
        self.assertFalse((self.home / ".oobe_audits_fired").exists())

    def test_disarm_of_an_arm_that_predates_the_hand_off_record_leaves_it(self):
        # A state file written before arm.py recorded the hand-off marker: nothing to put back.
        self.assertEqual(self._arm().returncode, 0)
        state = self._state()
        del state["handoff_marker"]
        (self.home / ".bench-oobe.json").write_text(json.dumps(state))
        (self.home / ".bootstrap_handoff_filed").write_text("sweep=t_real\ntask_id=t_rank\n")
        self.assertEqual(self._run("disarm.py", str(self.home), str(self.hermes)).returncode, 0)
        self.assertEqual((self.home / ".bootstrap_handoff_filed").read_text(), "sweep=t_real\ntask_id=t_rank\n")

    def test_disarm_keeps_a_job_it_did_not_add(self):
        self.store.write_text(json.dumps([OTHER_JOB, OOBE_JOB]))
        self.assertEqual(self._arm().returncode, 0)
        self.assertEqual(self._run("disarm.py", str(self.home), str(self.hermes)).returncode, 0)
        self.assertIn("oobe", self._jobs())

    def test_disarm_puts_back_a_job_that_was_there_and_removed_itself(self):
        # A fresh install's own job, used up by the stage on the stand-in cards.
        self.store.write_text(json.dumps([OTHER_JOB, {**OOBE_JOB, "fire_claim": "x"}]))
        self.assertEqual(self._arm().returncode, 0)
        self.assertEqual(self._state()["job_present"]["id"], "oobe")
        self.store.write_text(json.dumps([OTHER_JOB]))
        done = self._run("disarm.py", str(self.home), str(self.hermes))
        self.assertEqual(done.returncode, 0, done.stderr)
        oobe = next(j for j in json.loads(self.store.read_text()) if j["id"] == "oobe")
        self.assertNotIn("fire_claim", oobe)
        self.assertEqual(oobe["next_run_at"], "next:* * * * *")

    def test_disarm_tolerates_a_job_that_already_removed_itself(self):
        self.assertEqual(self._arm().returncode, 0)
        self.store.write_text(json.dumps([OTHER_JOB]))
        self.assertEqual(self._run("disarm.py", str(self.home), str(self.hermes)).returncode, 0)

    def test_a_card_left_unarchived_is_recorded_and_archived_by_the_disarm(self):
        failing = self.hermes.with_name("hermes-no-archive")
        failing.write_text(HERMES_STUB + "if sys.argv[1:3] == ['kanban', 'archive']:\n    sys.exit(1)\n")
        failing.chmod(failing.stat().st_mode | stat.S_IXUSR)
        done = self._run("arm.py", str(self.home), str(failing), "20261006200000", str(self.shipped))
        self.assertNotEqual(done.returncode, 0)
        cards = self._state()["cards"]
        self.assertEqual(len(cards), 1)
        self.hermes_log.unlink()
        self.assertEqual(self._run("disarm.py", str(self.home), str(self.hermes)).returncode, 0)
        self.assertIn(["kanban", "archive", cards[0]], self._hermes_calls())

    def test_disarm_with_nothing_armed_changes_nothing(self):
        (self.home / ".bootstrap_scan_filed").write_text("task_id=t_real\n")
        done = self._run("disarm.py", str(self.home), str(self.hermes))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.home / ".bootstrap_scan_filed").read_text(), "task_id=t_real\n")

    # --- the install's own stage -------------------------------------------------

    def test_own_stage_is_pending_while_the_job_waits_on_its_scan(self):
        self.store.write_text(json.dumps([OTHER_JOB, OOBE_JOB]))
        self.assertEqual(self._run("own_stage.py", str(self.home)).stdout.strip(), "pending")
        (self.home / ".oobe_audits_fired").write_text('{"fired": ["compliance-audit"]}')
        self.assertEqual(self._run("own_stage.py", str(self.home)).stdout.strip(), "pending")

    def test_own_stage_is_clear_once_done_on_an_install_nobody_spoke_to(self):
        # No report claimed, so the job stays for good: done is the end of the install's own stage.
        self.store.write_text(json.dumps([OTHER_JOB, OOBE_JOB]))
        (self.home / ".oobe_audits_fired").write_text('{"done": true}')
        self.assertEqual(self._run("own_stage.py", str(self.home)).stdout.strip(), "clear")

    def test_own_stage_waits_for_a_done_job_to_retire_once_the_report_was_claimed(self):
        # A tick is about to remove the job; arming then would record it as present while that
        # tick takes it away.
        self.store.write_text(json.dumps([OTHER_JOB, OOBE_JOB]))
        (self.home / ".oobe_audits_fired").write_text('{"done": true}')
        (self.home / ".bootstrap_completed").write_text("")
        self.assertEqual(self._run("own_stage.py", str(self.home)).stdout.strip(), "pending")
        self.store.write_text(json.dumps([OTHER_JOB]))
        self.assertEqual(self._run("own_stage.py", str(self.home)).stdout.strip(), "clear")

    def test_own_stage_is_clear_for_a_job_that_cannot_run(self):
        # A disabled or paused job never finishes, so the apply would wait out own_wait for nothing.
        for job in ({**OOBE_JOB, "enabled": False}, {**OOBE_JOB, "state": "paused"}, {**OOBE_JOB, "paused_at": "x"}):
            with self.subTest(job=job):
                self.store.write_text(json.dumps([OTHER_JOB, job]))
                self.assertEqual(self._run("own_stage.py", str(self.home)).stdout.strip(), "clear")

    def _fresh(self) -> str:
        return self._run("own_stage.py", str(self.home), "fresh").stdout.strip()

    def test_a_first_run_recorded_within_a_day_is_fresh(self):
        import time
        (self.home / ".oobe_audits_fired").write_text(json.dumps({"done": True, "at": time.time() - 3600}))
        self.assertEqual(self._fresh(), "fresh")
        # A stage that skipped the audits on this install is this install's first run too.
        (self.home / ".oobe_audits_fired").write_text(json.dumps({"done": True, "skipped": True, "at": time.time()}))
        self.assertEqual(self._fresh(), "fresh")

    def test_a_first_run_from_long_ago_or_none_is_stale(self):
        import time
        (self.home / ".oobe_audits_fired").write_text(json.dumps({"done": True, "at": time.time() - 25 * 3600}))
        self.assertEqual(self._fresh(), "stale")
        (self.home / ".oobe_audits_fired").write_text(json.dumps({"done": True}))
        self.assertEqual(self._fresh(), "stale")
        (self.home / ".oobe_audits_fired").unlink()
        self.assertEqual(self._fresh(), "stale")

    def test_a_disarm_puts_back_the_record_and_its_age(self):
        # The arm removes the record and the disarm restores it whole, `at` included, so a
        # long-lived install reads as stale again after a stand-in run.
        import time
        old = time.time() - 30 * 86400
        (self.home / ".oobe_audits_fired").write_text(json.dumps({"done": True, "at": old}))
        self.store.write_text(json.dumps([OTHER_JOB, OOBE_JOB]))
        self.assertEqual(self._arm().returncode, 0)
        self.assertEqual(self._run("disarm.py", str(self.home), str(self.hermes)).returncode, 0)
        self.assertEqual(self._fresh(), "stale")

    def test_a_fresh_first_run_ends_the_apply_before_any_arm(self):
        apply = HEREDOC.findall((STACK / "main.tf").read_text())[0]
        fresh = apply.index('agent_py "${local.home}" fresh)"')
        self.assertLess(apply.index("# ---- 2. Wait for the install's own first-run stage"), fresh)
        exit_at = apply.index("exit 0", fresh)
        self.assertLess(exit_at, apply.index("arming=1"))
        self.assertLess(exit_at, apply.index("'${local.arm_b64}'"))

    # --- the chain wait ----------------------------------------------------------

    def test_the_wait_is_skipped_on_an_image_without_the_job(self):
        bare = self.shipped.with_name("bare.json")
        bare.write_text(json.dumps({"jobs": [OTHER_JOB]}))
        self.assertIn("ships no oobe job", self._arm(bare).stdout)
        self.assertIn('no_job = "ships no oobe job"', (STACK / "main.tf").read_text())

    # --- the provisioners -------------------------------------------------------

    def test_the_provisioners_parse_as_bash(self):
        scripts = HEREDOC.findall((STACK / "main.tf").read_text())
        # The apply and the teardown; a pattern that stops matching would otherwise check nothing.
        self.assertEqual(len(scripts), 2)
        for script in scripts:
            done = subprocess.run(["bash", "-n"], input=render(script), capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_teardown_still_disarms_when_the_credentials_cannot_be_fetched(self):
        # Every step is best effort: a failed get-credentials warns and the disarm is still
        # tried; a failed disarm warns and the teardown ends. The runner, not the teardown,
        # waits for the audits the stage started.
        teardown = HEREDOC.findall((STACK / "main.tf").read_text())[1]
        self.assertNotIn("set -e", teardown)
        self.assertNotIn("busy", teardown)
        bin_dir = pathlib.Path(self._tmp.name) / "bin"
        bin_dir.mkdir()
        calls = pathlib.Path(self._tmp.name) / "calls"
        for tool, code in (("gcloud", 1), ("kubectl", 1)):
            stub = bin_dir / tool
            stub.write_text(f'#!/bin/sh\necho {tool} >> "{calls}"\ncat > /dev/null\nexit {code}\n')
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        done = subprocess.run(
            ["bash", "-c", render(teardown)],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        )
        self.assertEqual(calls.read_text().split(), ["gcloud", "kubectl"], done.stderr)
        self.assertIn("could not fetch credentials", done.stderr)
        self.assertIn("could not disarm", done.stderr)


if __name__ == "__main__":
    unittest.main()
