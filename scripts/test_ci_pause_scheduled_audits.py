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

"""hack/ci_pause_scheduled_audits.py, run as scheduled_audits runs it: `python3 - <home> [--resume] < script`.

Pinned: which jobs it pauses and which it leaves (plumbing, the oobe stage's audits, a job
already off), that it believes the store over Hermes' exit code, its exit codes, that --resume
undoes exactly what it paused, and that ci-eval-pr.sh pauses at lease time before any unit and
again right before the fan-out, and resumes from the EXIT trap.
"""

import ast
import json
import os
import pathlib
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

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
    assert sys.argv[1:4] == ["-p", "platform", "cron"] and sys.argv[4] in ("pause", "resume"), sys.argv
    if mode == "noop":
        sys.exit(0)
    if mode.startswith("lose-first:") and sys.argv[5] == mode.split(":", 1)[1]:
        # A concurrent writer (a starting container's cron merge) rewrites the
        # store over the first pause of this job: Hermes says yes, no marker lands.
        flag = os.environ["FAKE_ROSTER"] + ".lost"
        if not os.path.exists(flag):
            open(flag, "w").close()
            sys.exit(0)
    home = os.path.join(os.path.dirname(os.environ["FAKE_ROSTER"]), "..", "..", "..")
    if mode.startswith("drop-record-at:") and sys.argv[4] == "pause" and sys.argv[5] == mode.split(":", 1)[1]:
        # A resume ran between the record's write and this pause, and removed it.
        try:
            os.remove(os.path.join(home, ".ci-paused-audits.json"))
        except FileNotFoundError:
            pass
    if mode.startswith("block-record-at:") and sys.argv[4] == "pause" and sys.argv[5] == mode.split(":", 1)[1]:
        record = os.path.join(home, ".ci-paused-audits.json")
        os.remove(record)
        os.mkdir(record)
        os.mkdir(record + ".tmp")
    if mode.startswith("late-pause:") and sys.argv[4] == "resume":
        # A watch pass still under way pauses this job while the resume runs.
        late = mode.split(":", 1)[1]
        store = json.load(open(os.environ["FAKE_ROSTER"]))
        for job in store["jobs"]:
            if str(job["id"]) == late:
                job.update(enabled=False, state="paused", paused_at="2026-10-14T00:00:00+00:00")
        json.dump(store, open(os.environ["FAKE_ROSTER"], "w"))
    if mode == "arm-on-first":
        # The oobe-first-run-audits stack arms while this pause is in flight: the pause still
        # lands, and the state file is there for every later one.
        open(os.path.join(os.path.dirname(os.environ["FAKE_ROSTER"]), "..", "..", "..", ".bench-oobe.json"), "w").close()
    if mode.startswith("die-at:") and sys.argv[5] == mode.split(":", 1)[1]:
        import signal
        os.kill(os.getppid(), signal.SIGKILL)  # the pod going away mid-loop
        sys.exit(1)
    path = os.environ["FAKE_ROSTER"]
    store = json.load(open(path))
    for job in store["jobs"]:
        if str(job["id"]) == sys.argv[5]:
            if sys.argv[4] == "pause":
                job.update(enabled=False, state="paused", paused_at="2026-10-14T00:00:00+00:00")
            else:
                job.update(enabled=True, state="scheduled", paused_at=None)
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

    def run_script(self, mode="ok", stage=STAGE, resume=False, chain=False):
        env = {
            **os.environ,
            "HERMES_BIN": str(self.hermes),
            "FAKE_ROSTER": str(self.roster),
            "FAKE_HERMES_MODE": mode,
            "OOBE_STAGE_SOURCE": str(stage),
            "PLATFORM_ROSTER_SOURCE": str(self.shipped),
        }
        result = subprocess.run(
            [sys.executable, "-", str(self.home), *(["--resume"] if resume else []), *(["--chain"] if chain else [])],
            input=SCRIPT.read_text(),
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
        self.assertEqual(result.returncode, 3)
        self.assertTrue(store["c"]["enabled"])

    def test_a_restarted_pause_reads_as_already_paused(self):
        # After a gateway restart the merge leaves enabled: true beside the markers. A second
        # run (a retry, a later lease) must see that as paused, as Hermes does.
        self.write_roster([{"id": "c", "enabled": True, "state": "paused", "paused_at": "x"}])
        result, _ = self.run_script(mode="fail")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("c (already paused)", result.stdout)

    def test_a_numeric_id_is_paused_as_a_string(self):
        self.write_roster([{"id": 5, "enabled": True}])
        result, store = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(store[5]["state"], "paused")

    def test_resume_undoes_exactly_what_it_paused(self):
        self.write_roster([{"id": "c", "enabled": True},
                           {"id": "theirs", "enabled": False, "state": "paused", "paused_at": "x"}])
        self.run_script()
        record = self.home / ".ci-paused-audits.json"
        self.assertEqual(json.loads(record.read_text()), ["c"])
        result, store = self.run_script(resume=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("resumed c", result.stdout)
        self.assertTrue(store["c"]["enabled"])
        # A pause someone else made is not ours to lift.
        self.assertEqual(store["theirs"]["state"], "paused")
        self.assertFalse(record.exists())

    def test_a_retry_after_a_partial_pause_still_resumes_everything(self):
        # Attempt 1 pauses c and dies at d, before its readback; the record written
        # before acting still names c, so the retry's view of c as "already paused"
        # doesn't lose it from --resume.
        self.write_roster([{"id": "c", "enabled": True}, {"id": "d", "enabled": True}])
        first, store = self.run_script(mode="die-at:d")
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(store["c"]["state"], "paused")
        record = self.home / ".ci-paused-audits.json"
        self.assertEqual(json.loads(record.read_text()), ["c", "d"])
        result, _ = self.run_script()
        self.assertIn("paused d;", result.stdout)
        self.assertIn("c (already paused)", result.stdout)
        result, store = self.run_script(resume=True)
        self.assertIn("resumed c, d", result.stdout)
        self.assertTrue(store["c"]["enabled"] and store["d"]["enabled"])

    def test_a_job_recorded_but_never_paused_is_harmless(self):
        self.write_roster([{"id": "c", "enabled": True}])
        (self.home / ".ci-paused-audits.json").write_text('["c", "gone"]')
        result, store = self.run_script(resume=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("resumed nothing", result.stdout)
        self.assertFalse((self.home / ".ci-paused-audits.json").exists())

    def test_resume_names_a_job_the_store_still_shows_paused(self):
        self.write_roster([{"id": "c", "enabled": False, "state": "paused", "paused_at": "x"}])
        (self.home / ".ci-paused-audits.json").write_text('["c"]')
        result, _ = self.run_script(mode="noop", resume=True)
        self.assertEqual(result.returncode, 3)
        self.assertIn("FAILED c (still paused in the store)", result.stdout)

    def test_resume_with_no_record_does_nothing(self):
        self.write_roster([{"id": "c", "enabled": False, "state": "paused", "paused_at": "x"}])
        result, store = self.run_script(resume=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("nothing to resume", result.stdout)
        self.assertEqual(store["c"]["state"], "paused")

    def run_script_with(self, live, shipped):
        self.write_roster(live, shipped)
        return self.run_script()

    def test_the_store_is_believed_over_the_exit_code(self):
        self.write_roster([{"id": "c", "enabled": True}])
        result, _ = self.run_script(mode="noop")
        # Worth another try, not decided: the caller's next attempt settles the
        # gateway and waits first, which is what outlasts another writer.
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED c (not paused in the store)", result.stdout)

    def test_a_failed_pause_is_named(self):
        self.write_roster([{"id": "c", "enabled": True}])
        result, _ = self.run_script(mode="fail")
        # Hermes refused it: a retry would only repeat that, so the verdict is final.
        self.assertEqual(result.returncode, 3)
        self.assertIn("FAILED c (exit 1: boom)", result.stdout)

    def test_a_pause_lost_to_a_concurrent_writer_is_paused_again(self):
        # eval-next 2110626938950258691: the gateway was replaced mid-pause and the
        # new container's cron merge dropped one marker. The read-back finds it
        # and the next round pauses it again.
        self.write_roster([{"id": "c", "enabled": True}, {"id": "d", "enabled": True}])
        result, store = self.run_script(mode="lose-first:d")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(store["d"]["state"], "paused")
        self.assertIn("paused c, d;", result.stdout)

    def test_a_stale_rewrite_after_the_first_clean_read_is_caught(self):
        # The incident's writer reads its copy before the pause and writes it after:
        # the first read-back is clean, and the marker goes a second later.
        self.write_roster([{"id": "c", "enabled": True}, {"id": "d", "enabled": True}])
        stale = self.roster.read_text()

        def late_writer():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    jobs = {j["id"]: j for j in json.loads(self.roster.read_text())["jobs"]}
                except (OSError, ValueError):
                    jobs = {}
                if jobs.get("d", {}).get("state") == "paused":
                    time.sleep(1)
                    self.roster.write_text(stale)
                    return
                time.sleep(0.05)

        writer = threading.Thread(target=late_writer)
        writer.start()
        result, _ = self.run_script()
        writer.join()
        # Read after the writer is done: the store the run left is what the cases get.
        store = {j["id"]: j for j in json.loads(self.roster.read_text())["jobs"]}
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(store["d"].get("state"), "paused", "the late rewrite dropped d's marker")
        self.assertEqual(store["c"].get("state"), "paused")

    def test_an_unreadable_stage_pauses_nothing(self):
        # Without the stage's list it can't tell which audits the stage owns, so it leaves all.
        self.write_roster([{"id": "c", "enabled": True}])
        result, store = self.run_script(stage=self.home / "missing.py")
        self.assertEqual(result.returncode, 3)
        self.assertTrue(store["c"]["enabled"])
        self.assertIn("nothing paused", result.stdout)


STAGE_FIVE = ast.literal_eval(re.search(r"FIRST_RUN_AUDITS = (\([^)]*\))", STAGE.read_text(), re.S).group(1))
NOW = datetime.now(timezone.utc)
FUTURE = (NOW + timedelta(hours=6)).isoformat()
PAST = (NOW - timedelta(minutes=1)).isoformat()


class ChainTest(unittest.TestCase):
    """The first-run stage's audits: paused once the stage has had its run of each (#5570)."""

    setUp, tearDown = PauseTest.setUp, PauseTest.tearDown
    write_roster, run_script = PauseTest.write_roster, PauseTest.run_script

    def write_stage(self, fired=(), current=None, done=False):
        state = {"fired": list(fired), "done": done}
        if current:
            state["current"] = {"job": current, "marked_at": NOW.timestamp()}
        (self.home / ".oobe_audits_fired").write_text(json.dumps(state))

    def write_runs(self, rows):
        db = self.home / "profiles" / "platform" / "cron" / "executions.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE IF NOT EXISTS executions (job_id TEXT, status TEXT, claimed_at TEXT)")
        conn.executemany("INSERT INTO executions VALUES (?, ?, ?)", rows)
        conn.commit()
        conn.close()

    def five(self, **overrides):
        jobs = [{"id": a, "enabled": True, "next_run_at": FUTURE} for a in STAGE_FIVE]
        for job in jobs:
            job.update(overrides.get(job["id"], {}))
        return jobs + [{"id": "other-audit", "enabled": True, "next_run_at": FUTURE}]

    def test_nothing_of_the_stages_is_paused_before_it_starts(self):
        self.write_roster(self.five())
        result, store = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for audit in STAGE_FIVE:
            self.assertTrue(store[audit]["enabled"], audit)
        self.assertEqual(store["other-audit"]["state"], "paused")

    def test_an_audit_whose_run_has_ended_is_paused_and_the_rest_are_left(self):
        first, second, third = STAGE_FIVE[:3]
        self.write_roster(self.five())
        self.write_stage(fired=[first, second, third], current=third)
        self.write_runs([(first, "completed", PAST), (second, "running", PAST)])
        result, store = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(store[first]["state"], "paused")
        # Under way, and the mark awaiting its run: the stage still waits on both.
        self.assertTrue(store[second]["enabled"])
        self.assertTrue(store[third]["enabled"])
        for audit in STAGE_FIVE[3:]:
            self.assertTrue(store[audit]["enabled"], audit)
        self.assertIn(f"{second} (oobe stage)", result.stdout)
        self.assertIn(first, json.loads((self.home / ".ci-paused-audits.json").read_text()))

    def test_a_run_left_at_running_long_ago_does_not_hold_the_pause(self):
        first = STAGE_FIVE[0]
        self.write_roster(self.five())
        self.write_stage(fired=[first])
        self.write_runs([(first, "running", (NOW - timedelta(hours=3)).isoformat())])
        _, store = self.run_script()
        self.assertEqual(store[first]["state"], "paused")

    def test_a_due_mark_is_left_for_the_tick_to_claim(self):
        # A case that marked it due (`cron.jobs.trigger_job`) after the chain waits on that run.
        first = STAGE_FIVE[0]
        self.write_roster(self.five(**{first: {"next_run_at": PAST}}))
        self.write_stage(fired=[first], done=False)
        result, store = self.run_script()
        self.assertTrue(store[first]["enabled"])
        self.assertIn(f"{first} (oobe stage)", result.stdout)

    def test_every_audit_is_paused_once_the_chain_is_done(self):
        self.write_roster(self.five())
        self.write_stage(fired=STAGE_FIVE, done=True)
        self.write_runs([(a, "completed", PAST) for a in STAGE_FIVE])
        result, store = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for audit in STAGE_FIVE:
            self.assertEqual(store[audit]["state"], "paused", audit)

    def test_a_done_stage_left_on_its_last_audit_still_has_it_paused(self):
        # The stage keeps `current` after it is done (seen on a live install, 2026-11-12).
        self.write_roster(self.five())
        self.write_stage(fired=STAGE_FIVE, current=STAGE_FIVE[-1], done=True)
        self.write_runs([(a, "completed", PAST) for a in STAGE_FIVE])
        _, store = self.run_script(chain=True)
        for audit in STAGE_FIVE:
            self.assertEqual(store[audit]["state"], "paused", audit)

    def test_a_done_stage_has_the_audits_it_never_fired_paused_too(self):
        # A stage skipped whole writes no `fired`; one that gave up on an audit leaves it out.
        for state in ({"done": True, "skipped": True, "reason": "x"},
                      {"done": True, "fired": list(STAGE_FIVE[:2]), "gave_up": list(STAGE_FIVE[2:])}):
            with self.subTest(state=state):
                self.write_roster(self.five())
                (self.home / ".oobe_audits_fired").write_text(json.dumps(state))
                _, store = self.run_script(chain=True)
                for audit in STAGE_FIVE:
                    self.assertEqual(store[audit]["state"], "paused", audit)

    def test_a_stage_under_way_leaves_what_it_has_not_reached(self):
        self.write_roster(self.five())
        self.write_stage(fired=STAGE_FIVE[:1], done=False)
        _, store = self.run_script(chain=True)
        self.assertEqual(store[STAGE_FIVE[0]]["state"], "paused")
        for audit in STAGE_FIVE[1:]:
            self.assertTrue(store[audit]["enabled"], audit)

    def test_none_is_paused_while_the_stack_has_the_stage_armed(self):
        self.write_roster(self.five())
        self.write_stage(fired=STAGE_FIVE, done=True)
        (self.home / ".bench-oobe.json").write_text("{}")
        result, store = self.run_script()
        for audit in STAGE_FIVE:
            self.assertTrue(store[audit]["enabled"], audit)
            self.assertIn(f"{audit} (oobe stage armed)", result.stdout)
        # Not even recorded as intended: arm.py resumes what the record names.
        self.assertFalse(set(STAGE_FIVE) & set(json.loads((self.home / ".ci-paused-audits.json").read_text())))

    def test_an_arm_mid_run_stops_the_pauses_after_it(self):
        # The first pause was decided and made before the arm; arm.py resumes that one.
        self.write_roster(self.five())
        self.write_stage(fired=STAGE_FIVE, done=True)
        result, store = self.run_script(mode="arm-on-first", chain=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        paused_now = [a for a in STAGE_FIVE if store[a].get("state") == "paused"]
        self.assertEqual(paused_now, [STAGE_FIVE[0]])
        for audit in STAGE_FIVE[1:]:
            self.assertIn(f"{audit} (oobe stage armed)", result.stdout)

    def test_an_unreadable_marker_pauses_none_of_the_five(self):
        self.write_roster(self.five())
        (self.home / ".oobe_audits_fired").write_text("{not json")
        result, store = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for audit in STAGE_FIVE:
            self.assertTrue(store[audit]["enabled"], audit)
        self.assertEqual(store["other-audit"]["state"], "paused")

    def test_chain_touches_only_the_five(self):
        # A case that triggered another job pauses it again itself; a pause here could drop its run.
        self.write_roster(self.five())
        self.write_stage(fired=STAGE_FIVE, done=True)
        result, store = self.run_script(chain=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(store["other-audit"]["enabled"])
        self.assertNotIn("other-audit", result.stdout)
        for audit in STAGE_FIVE:
            self.assertEqual(store[audit]["state"], "paused", audit)

    def test_the_resume_lifts_the_five_too(self):
        self.write_roster(self.five())
        self.write_stage(fired=STAGE_FIVE, done=True)
        self.run_script(chain=True)
        result, store = self.run_script(resume=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for audit in STAGE_FIVE:
            self.assertTrue(store[audit]["enabled"], audit)

    def test_a_pause_after_a_resume_removed_the_record_is_recorded_again(self):
        self.write_roster(self.five())
        self.write_stage(fired=STAGE_FIVE, done=True)
        result, store = self.run_script(mode=f"drop-record-at:{STAGE_FIVE[2]}", chain=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(store[STAGE_FIVE[2]]["state"], "paused")
        self.assertIn(STAGE_FIVE[2], json.loads((self.home / ".ci-paused-audits.json").read_text()))

    def test_a_record_that_cannot_be_written_again_is_named(self):
        self.write_roster(self.five())
        self.write_stage(fired=STAGE_FIVE, done=True)
        # The first write lands; the record is then made a directory, so the rewrite fails.
        result, store = self.run_script(mode=f"block-record-at:{STAGE_FIVE[1]}", chain=True)
        self.assertEqual(store[STAGE_FIVE[1]]["state"], "paused")
        self.assertIn(f"record not written again after pausing {STAGE_FIVE[1]}", result.stdout)

    def test_a_pause_landing_during_the_resume_keeps_the_record_for_the_retry(self):
        first, late = STAGE_FIVE[:2]
        self.write_roster(self.five(**{first: {"enabled": False, "state": "paused", "paused_at": "x"}}))
        (self.home / ".ci-paused-audits.json").write_text(json.dumps([first, late]))
        result, store = self.run_script(mode=f"late-pause:{late}", resume=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(store[first]["enabled"])
        self.assertIn(late, json.loads((self.home / ".ci-paused-audits.json").read_text()))
        # scheduled_audits retries; the retry resumes it and the record goes.
        result, store = self.run_script(resume=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(store[late]["enabled"])
        self.assertFalse((self.home / ".ci-paused-audits.json").exists())

    def test_its_paths_are_the_stage_and_the_stacks(self):
        text = SCRIPT.read_text()
        stage = STAGE.read_text()
        self.assertIn('AUDITS_MARKER = ".oobe_audits_fired"', stage)
        self.assertIn('STAGE_MARKER = os.path.join(home, ".oobe_audits_fired")', text)
        arm = (REPO / "bench" / "tf" / "prebuilt" / "oobe-first-run-audits" / "arm.py").read_text()
        self.assertIn('STATE = os.path.join(home, ".bench-oobe.json")', arm)
        self.assertIn('STACK_STATE = os.path.join(home, ".bench-oobe.json")', text)
        # arm.py resumes from this script's record.
        self.assertIn('RECORD = os.path.join(home, ".ci-paused-audits.json")', text)
        self.assertIn('CI_PAUSED = os.path.join(home, ".ci-paused-audits.json")', arm)
        runs = (REPO / "hack" / "ci_platform_runs.py").read_text()
        self.assertEqual(
            re.search(r"^STALE_SECONDS = (.+)$", text, re.M).group(1),
            re.search(r"^STALE_SECONDS = (.+)$", runs, re.M).group(1),
        )
        hermes_timeout = int(re.search(r"^HERMES_TIMEOUT_SECONDS = (\d+)$", text, re.M).group(1))
        settle = int(re.search(r'"OOBE_ARM_RESUME_SETTLE_SECONDS", "(\d+)"', arm).group(1))
        self.assertGreater(settle, hermes_timeout)


class RestartTest(unittest.TestCase):
    """The pause outlives a gateway restart: the entrypoint's merge keeps the pause markers.

    profile_scaffold.merge_cron_store puts back every key the image ships, `enabled` among
    them, and keeps every key it doesn't. The image ships no `state` or `paused_at`, so a
    paused job comes out `enabled: true` with its markers, which Hermes' due scan refuses
    (`_has_pause_marker`) and disables again. A merge rule that dropped unshipped keys would
    undo the pause silently; this fails first.
    """

    def test_merge_keeps_the_pause_markers(self):
        sys.path.insert(0, str(REPO / "agents" / "platform" / "scripts"))
        try:
            import profile_scaffold
        finally:
            sys.path.pop(0)
        shipped = json.loads(ROSTER_SOURCE.read_text())
        job = next(j for j in shipped["jobs"] if j["id"] == "gcp-networking-fabric-audit")
        live = {"jobs": [{**job, "enabled": False, "state": "paused",
                          "paused_at": "2026-10-14T15:02:02+00:00"}]}
        merged = profile_scaffold.merge_cron_store(shipped, live)
        after = next(j for j in merged["jobs"] if j["id"] == job["id"])
        self.assertTrue(after["enabled"])
        self.assertEqual(after["state"], "paused")
        self.assertEqual(after["paused_at"], "2026-10-14T15:02:02+00:00")
        for shipped_job in shipped["jobs"]:
            for key in ("state", "paused_at"):
                self.assertNotIn(key, shipped_job, f"{shipped_job['id']}: the image now ships a pause key; the merge would reset it")


class ReadyGatewayPodTest(unittest.TestCase):
    """ready_gateway_pod, run from the runner's own text against a fake kubectl."""

    def run_helper(self, replicas, ready, wait=0, kind="deployment", starting=(), unready=(), api_down=0):
        # `starting`: pods whose PID 1 is still the entrypoint (Ready on HA all the same).
        # `unready`: live pods not Ready yet (a surge pod). The phase filter is the
        # server's; ReadyGatewayPodTemplateTest checks the template itself.
        # `api_down`: workload lookups that fail with a non-NotFound error first.
        pids = "".join(
            f"  *exec\\ pod/{n}\\ *) printf '/bin/bash\\0/usr/local/bin/agent-entrypoint\\0' ;;\n" for n in starting)
        listing = "".join(f"pod/{n} R" + chr(92) + "n" for n in ready) + "".join(f"pod/{n} N" + chr(92) + "n" for n in unready)
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp) / "kubectl"
            counter = pathlib.Path(tmp) / "down"
            counter.write_text(str(api_down))
            fake.write_text(textwrap.dedent(f"""\
                #!/bin/sh
                case "$*" in
                  *-gateway\\ -o\\ name*)
                    n=$(cat {counter}); if [ "$n" -gt 0 ]; then echo $((n - 1)) > {counter}; echo "Unable to connect to the server" >&2; exit 1; fi ;;
                esac
                case "$*" in
                  *get\\ {kind}/platform-agent-gateway\\ -o\\ name*) echo {kind}.apps/platform-agent-gateway ;;
                  *get\\ *-gateway\\ -o\\ name*) echo 'Error from server (NotFound): not found' >&2; exit 1 ;;
                  *go-template=*matchLabels*) printf 'app=platform-agent-gateway,' ;;
                  *jsonpath=*spec.replicas*) printf '{replicas}' ;;
                  *get\\ pods*) printf '{listing}' ;;
                """) + pids + textwrap.dedent("""\
                  *exec\\ pod/*cmdline*) printf 'python3\\0-m\\0hermes_cli.main\\0gateway\\0' ;;
                esac
                """))
            fake.chmod(0o755)
            helper = RUNNER.read_text().split("ready_gateway_pod() {", 1)[1].split("\n}\n", 1)[0]
            script = f"ready_gateway_pod() {{{helper}\n}}\nready_gateway_pod ctx ns {wait}"
            env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", "AGENT_SERVICE_NAME": "platform-agent",
                   "EVAL_GATEWAY_CONTAINER": "platform-agent", "EVAL_PAUSE_AUDITS_POD_POLL_SECONDS": "1",
                   "EVAL_PAUSE_AUDITS_PROBE_TIMEOUT": "5s", "EVAL_PAUSE_AUDITS_POD_WAIT_SECONDS": "300"}
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)

    def test_one_replica_one_ready_pod(self):
        result = self.run_helper(1, ["gw-a"])
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "pod/gw-a"))

    def test_a_rollout_still_settling_is_waited_out(self):
        # Two Ready pods for a one-replica Deployment: the old one has not gone yet.
        result = self.run_helper(1, ["gw-a", "gw-b"])
        self.assertEqual(result.returncode, 1)

    def test_an_ha_gateway_with_all_replicas_ready_is_accepted(self):
        result = self.run_helper(2, ["gw-a", "gw-b"])
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "pod/gw-a"))

    def test_an_ha_pod_ready_but_still_in_its_entrypoint_is_waited_out(self):
        # HA's probe passes with nothing on 8642, so gw-c is Ready mid-merge.
        result = self.run_helper(2, ["gw-a", "gw-c"], starting=("gw-c",))
        self.assertEqual(result.returncode, 1)

    def test_a_surge_pod_not_ready_yet_is_waited_out(self):
        # A rollout's surge pod is not Ready while its entrypoint runs, so the Ready
        # set alone still reads as the two old pods: settled, wrongly.
        result = self.run_helper(2, ["gw-a", "gw-b"], unready=("gw-c",))
        self.assertEqual(result.returncode, 1)

    def test_a_lookup_that_fails_for_a_moment_is_waited_out(self):
        # A blip at the workload read is not "no such workload".
        result = self.run_helper(1, ["gw-a"], wait=30, api_down=2)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "pod/gw-a"))

    def test_an_ha_gateway_on_rwo_storage_is_a_statefulset(self):
        result = self.run_helper(2, ["gw-0", "gw-1"], kind="statefulset")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "pod/gw-0"))

    def test_a_gateway_scaled_to_zero_returns_at_once(self):
        start = time.monotonic()
        result = self.run_helper(0, [], wait=300)
        self.assertEqual(result.returncode, 1)
        self.assertLess(time.monotonic() - start, 30)

    def test_no_gateway_workload_returns_at_once(self):
        result = self.run_helper(1, ["gw-a"], wait=300, kind="daemonset")
        self.assertEqual(result.returncode, 1)


class RunnerTest(unittest.TestCase):
    def test_called_at_lease_and_before_the_fan_out(self):
        text = RUNNER.read_text()
        calls = [m.start() for m in re.finditer(r'^scheduled_audits pause "lease"$', text, re.M)]
        self.assertEqual(len(calls), 1)
        # And again right before the fan-out, which catches a restart in between.
        again = [m.start() for m in re.finditer(r'^scheduled_audits pause "fan-out"$', text, re.M)]
        self.assertEqual(len(again), 1)
        self.assertLess(again[0], text.index('# Two phases on both lanes'))
        lease_reset = text.index('reset_agent_pulls "lease"')
        first_unit = re.search(r'^\s+run_one_unit "\$\{TASKS', text, re.M).start()
        self.assertLess(lease_reset, calls[0])
        self.assertLess(calls[0], first_unit)

    def test_the_function_pipes_this_script(self):
        body = RUNNER.read_text().split("scheduled_audits() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('< "${SCRIPT_DIR}/ci_pause_scheduled_audits.py"', body)
        self.assertIn('"gke_${PROJECT_ID}_"*', body)
        self.assertIn("EVAL_PAUSE_AUDITS_ATTEMPTS", body)
        self.assertIn("return 0", body)
        # A pause that does not take stops the run, loudly; a resume only warns.
        pause_branch = body.split('if [ "${mode}" = "pause" ]; then', 1)[1].split("\n  fi\n", 1)[0]
        self.assertIn("exit 1", pause_branch)
        # It execs into one Ready pod by name, not deployment/<name>.
        self.assertIn('pod="$(ready_gateway_pod "${ctx}" "${ns}" "${wait}")"', body)
        self.assertNotIn('exec -i "deployment/', body)
        # The script's DECIDED exit ends the retries.
        self.assertIn('"${rc}" -eq "${EVAL_PAUSE_AUDITS_DECIDED_EXIT}" ] && break', body)
        decided = re.search(r"^readonly EVAL_PAUSE_AUDITS_DECIDED_EXIT=(\d+)$", RUNNER.read_text(), re.M).group(1)
        self.assertIn(f"OK, RETRY, DECIDED = 0, 1, {decided}", SCRIPT.read_text())

    def test_the_exit_trap_resumes(self):
        trap = RUNNER.read_text().split("profile_and_dump_on_exit() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('declare -F scheduled_audits >/dev/null && scheduled_audits resume "exit"', trap)

    def test_the_resume_waits_briefly_for_the_gateway(self):
        # It runs in the EXIT trap, ahead of the artifact dump.
        text = RUNNER.read_text()
        body = text.split("scheduled_audits() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('[ "${mode}" != "resume" ] || wait="${EVAL_RESUME_AUDITS_POD_WAIT_SECONDS}"', body)
        resume = int(re.search(r"^readonly EVAL_RESUME_AUDITS_POD_WAIT_SECONDS=(\d+)$", text, re.M).group(1))
        self.assertLessEqual(resume, 30)


class ChainWatchTest(unittest.TestCase):
    """ci-eval-pr.sh pauses the stage's audits through the fan-out, outside the job table (#5570)."""

    def setUp(self):
        self.text = RUNNER.read_text()

    def body(self, name):
        return self.text.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]

    def test_chain_mode_passes_the_flag_and_only_warns(self):
        body = self.body("scheduled_audits")
        self.assertIn('[ "${mode}" = "chain" ] && args=(--chain)', body)
        # Only a lease or fan-out pause stops the run; a failed pass is tried again next time.
        self.assertIn('if [ "${mode}" = "pause" ]; then', body)

    def test_the_watch_runs_from_the_fan_out_to_its_end(self):
        start = self.text.index('scheduled_audits pause "fan-out"\nstart_chain_audits_watch\n')
        first_launch = self.text.index('launch_units "${UNIT_QUEUE}"')
        stop = self.text.index("\nstop_chain_audits_watch\n")
        leftovers = self.text.index("\nreport_github_leftovers\n")
        self.assertLess(start, first_launch)
        # After the last phase, the scope-excluded cases.
        self.assertLess(self.text.index("for SCOPE_LINE in"), stop)
        self.assertLess(stop, leftovers)

    def test_the_watch_is_outside_the_job_table(self):
        body = self.body("start_chain_audits_watch")
        # A subshell that exits at once: launch_units counts `jobs -rp`, and the phases `wait`.
        self.assertRegex(body, r"\(\n    \(\n")
        self.assertIn('echo "$!" > "${pidfile}"', body)
        self.assertIn('scheduled_audits chain "fan-out watch"', body)
        self.assertIn('kill -0 "${owner}"', body)

    def test_the_exit_trap_stops_the_watch_before_the_resume(self):
        trap = self.text.split("profile_and_dump_on_exit() {", 1)[1].split("\n}\n", 1)[0]
        stop = trap.index("declare -F stop_chain_audits_watch >/dev/null && stop_chain_audits_watch")
        self.assertLess(stop, trap.index('scheduled_audits resume "exit"'))

    def run_watch(self, interval, stub, steps, stop_wait=20):
        """The two functions as the runner defines them, scheduled_audits stubbed, then <steps>."""
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "calls"
            script = "\n".join([
                "set -euo pipefail",
                f'STATE_DIR="{tmp}"',
                f"readonly EVAL_CHAIN_AUDITS_INTERVAL_SECONDS={interval}",
                'readonly EVAL_CHAIN_AUDITS_PID_FILE="watch.pid"',
                'readonly EVAL_CHAIN_AUDITS_RUNNING_FILE="watch.running"',
                f"readonly EVAL_CHAIN_AUDITS_STOP_WAIT_SECONDS={stop_wait}",
                f'LOG="{log}"',
                f"scheduled_audits() {{ {stub}; }}",
                "start_chain_audits_watch() {" + self.body("start_chain_audits_watch") + "\n}",
                "stop_chain_audits_watch() {" + self.body("stop_chain_audits_watch") + "\n}",
                "start_chain_audits_watch",
                # Not a job of this shell: a bare wait returns at once.
                'test -z "$(jobs -rp)"',
                "wait",
                *[step.replace("{tmp}", tmp) for step in steps],
            ])
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return log.read_text().splitlines() if log.exists() else []

    def test_the_watch_runs_passes_and_a_stop_ends_its_nap_at_once(self):
        # A 4s nap against a 2.5s bound: a stop that lands mid-pass legitimately
        # waits one of the stop's own 1s polls, so the bound has to clear that,
        # and the nap has to be longer than the bound for "waited out the nap"
        # to be told apart. Timed in Python, not with bash's whole-second
        # SECONDS, which rounds a 1.1s stop up to 2 (#5910).
        py = shlex.quote(sys.executable)
        now = f"{py} -c 'import time; print(time.time())'"
        calls = self.run_watch(4, 'echo "$1 $2" >> "$LOG"', [
            "sleep 9",
            f"start=$({now})",
            "stop_chain_audits_watch",
            # The nap ended at once, not at its next wake-up.
            f"{py} -c 'import sys; sys.exit(float(sys.argv[2]) - float(sys.argv[1]) > 2.5)' \"$start\" \"$({now})\""
            ' || { echo "stop waited out the nap" >&2; exit 9; }',
            '[ ! -e "{tmp}/watch.running" ] && [ ! -e "{tmp}/watch.pid" ]',
            'n=$(wc -l < "$LOG")',
            # Longer than the interval: a watch that survived the stop would pass again.
            "sleep 5",
            '[ "$(wc -l < "$LOG")" -eq "$n" ]',
        ])
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(set(calls), {"chain fan-out watch"})

    def test_a_stop_leaves_no_nap_holding_the_log_open(self):
        # An orphaned `sleep` would keep the job's stdout open for the rest of its interval.
        start = time.monotonic()
        self.run_watch(60, 'echo "$1" >> "$LOG"', ["sleep 1", "stop_chain_audits_watch"])
        self.assertLess(time.monotonic() - start, 30)

    def test_a_second_stop_still_waits_for_a_pass_the_first_outwaited(self):
        # The fan-out's stop times out; the EXIT trap's stop, with no pid file left, waits on.
        calls = self.run_watch(1, 'echo started >> "$LOG"; sleep 5; echo landed >> "$LOG"', [
            "sleep 2",
            "stop_chain_audits_watch 2>/dev/null",
            'tail -1 "$LOG" | grep -qx started',
            '[ ! -e "{tmp}/watch.pid" ]',
            "stop_chain_audits_watch",
            'tail -1 "$LOG" | grep -qx landed || { echo "the second stop did not wait" >&2; exit 9; }',
        ], stop_wait=2)
        self.assertEqual(calls, ["started", "landed"])

    def test_a_stop_waits_for_a_pass_under_way(self):
        # The pass's pauses land before the EXIT trap's resume reads the record.
        calls = self.run_watch(1, 'echo started >> "$LOG"; sleep 3; echo landed >> "$LOG"', [
            "sleep 2",
            "stop_chain_audits_watch",
            'tail -1 "$LOG" | grep -qx landed || { echo "stop returned mid-pass" >&2; exit 9; }',
            '[ ! -e "{tmp}/watch.running" ]',
        ])
        self.assertEqual(calls, ["started", "landed"])

if __name__ == "__main__":
    unittest.main()
