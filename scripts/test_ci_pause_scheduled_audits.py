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
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
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

    def run_script(self, mode="ok", stage=STAGE, resume=False):
        env = {
            **os.environ,
            "HERMES_BIN": str(self.hermes),
            "FAKE_ROSTER": str(self.roster),
            "FAKE_HERMES_MODE": mode,
            "OOBE_STAGE_SOURCE": str(stage),
            "PLATFORM_ROSTER_SOURCE": str(self.shipped),
        }
        result = subprocess.run(
            [sys.executable, "-", str(self.home), *(["--resume"] if resume else [])], input=SCRIPT.read_text(),
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
        self.assertLess(again[0], text.index('# Two phases on the inject lane'))
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
        self.assertIn('[ "${mode}" = "pause" ] || wait="${EVAL_RESUME_AUDITS_POD_WAIT_SECONDS}"', body)
        resume = int(re.search(r"^readonly EVAL_RESUME_AUDITS_POD_WAIT_SECONDS=(\d+)$", text, re.M).group(1))
        self.assertLessEqual(resume, 30)


if __name__ == "__main__":
    unittest.main()
