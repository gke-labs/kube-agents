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

"""The bootstrap-discovery plant, run against a stub cluster.

`bench/tf/prebuilt/bootstrap-discovery/main.tf` re-arms the onboarding gate and
waits for the sweep it files. Three behaviours pinned here fail quietly on a
real install:

  1. Step 4 must not hand over on a run that ended before the sweep filed its
     cards, or one the worker did not end itself -- a rate-limit block, or a
     run that filed some and was reclaimed. Handing over early grades a
     fan-out that is still being written. With no Cluster Agent card filed,
     only a completed run hands over: a worker that blocked can still file
     them once the block is lifted.
  2. On failure, the exit trap must wait for every gateway pod's gate run to
     exit before it lists the cards to archive. A gate run that read the marker
     as absent files its sweep after the trap puts the marker back, and above
     one replica that run is on the leader, not whichever pod `kubectl exec
     deployment/...` picks.
  3. On failure, the exit trap must close a gate step 2 opened, and no other:
     left open, the gate files a sweep nothing archives; closed where it was
     open, it never files that install's own sweep. So a failed read of what
     closed the gate stops the apply before step 2 changes anything.

The rest pin step 1's refusals, step 2 stopping when it cannot list the open
cards or the sandbox pods, or remove the INVENTORY files from one, step 4's
handling of a board read that fails and of a sweep no worker picked up, the
trap retrying a marker restore or a card listing that fails, the trap and the
destroy carrying on past a failed step and naming it, and every exec into the
agent Deployment bounding its wait for a pod. As in
`test_autoops_incident_plant.py`, the provisioners are rendered the way
Terraform renders them and run against a stub `kubectl`/`gcloud`/`sleep`. The
reads in steps 1 and 2 also run on their own against a data directory, and
step 4's board query against a sqlite board.
"""

import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODULE = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "bootstrap-discovery" / "main.tf"
_HEREDOC_RE = re.compile(r"command\s*=\s*<<-EOT\n(.*?)\n\s*EOT\n", re.S)
_RUN_STATE_RE = re.compile(r"^run_state\(\) \{\n  agent_py [^\n]*<<'PY'\n(.*?)\nPY\n", re.S | re.M)
_STEP_1_RE = re.compile(r"^state=\"\$\(agent_py [^\n]*<<'PY' \|\| true\n(.*?)\nPY\n", re.S | re.M)
_STEP_2_RE = re.compile(r"^old_id=\"\$\(agent_py <<'PY'\n(.*?)\nPY\n", re.S | re.M)
_PLANT_BLOCK = 0
_DESTROY_BLOCK = 1
_SWEEP = "t_sweep"
_CLUSTER_KEY = "bootstrap-inventory-cluster-example-project-cluster-"

_INTERPOLATIONS = {
    "local.home": "/opt/data",
    "local.hermes": "/opt/hermes/.venv/bin/hermes",
    "local.python": "/opt/hermes/.venv/bin/python3",
    "local.key_like": "bootstrap-inventory-%",
    "local.cluster_key_like": "bootstrap-inventory-cluster-%",
    "local.file_wait": "600",
    "local.run_wait": "900",
    "local.poll": "15",
    "local.inventory": "/opt/data/INVENTORY.raw.md /opt/data/INVENTORY.md",
    "local.gate_script": "bootstrap_scan_gate.py",
    "local.gate_wait": "300",
    "local.list_tries": "3",
    "local.list_wait": "5",
    "local.pod_wait": "5",
    "local.scan_job": "bootstrap-inventory-scan",
    "local.rate_limit_block": "provider rate limit: API retries exhausted",
    "var.project_id": "kube-agents-evals",
    "var.host_cluster_name": "platform-agent-host",
    "var.host_cluster_location": "us-central1",
    "var.agent_namespace": "kubeagents-system",
    "var.agent_deployment": "platform-agent-gateway",
    "var.agent_container": "platform-agent",
    "var.sandbox_selector": "app=platform-agent-shell",
    "var.sandbox_container": "shell",
}

_DESTROY_INTERPOLATIONS = {
    "self.triggers.host_project": _INTERPOLATIONS["var.project_id"],
    "self.triggers.host_cluster": _INTERPOLATIONS["var.host_cluster_name"],
    "self.triggers.host_location": _INTERPOLATIONS["var.host_cluster_location"],
    "self.triggers.namespace": _INTERPOLATIONS["var.agent_namespace"],
    "self.triggers.deployment": _INTERPOLATIONS["var.agent_deployment"],
    "self.triggers.container": _INTERPOLATIONS["var.agent_container"],
    "self.triggers.sandbox_selector": _INTERPOLATIONS["var.sandbox_selector"],
    "self.triggers.sandbox_container": _INTERPOLATIONS["var.sandbox_container"],
}

# Records every call to $CALLS, tagging the in-pod Python by what it reads, and
# keeps scenario state as files under $STATE. The leader pod is listed second so
# that a check of the first pod alone reads idle. Without the Running filter the
# listing also returns an evicted pod whose exec fails.
_KUBECTL_STUB = r'''#!/usr/bin/env python3
import os, pathlib, sys

argv = sys.argv[1:]
state = pathlib.Path(os.environ["STATE"])
line = " ".join(argv)


def bump(name):
    path = state / name
    n = int(path.read_text()) if path.exists() else 0
    path.write_text(str(n + 1))
    return n


def record(tag=""):
    with open(os.environ["CALLS"], "a") as fh:
        fh.write(("kubectl " + line + (" [" + tag + "]" if tag else "")).replace("\n", " ") + "\n")


if argv[:1] == ["get"]:
    record()
    if argv[1] == "deployment":
        print("app=gw,")
    elif "app=gw" in argv and os.environ.get("NO_PODS") != "1":
        print("pod/gw-follower\npod/gw-leader")
        if "--field-selector=status.phase=Running" not in argv:
            print("pod/gw-evicted")
    elif "app=platform-agent-shell" in argv:
        if os.environ.get("SANDBOX_LIST_FAIL") == "1":
            sys.stderr.write("error: You must be logged in to the server (Unauthorized)\n")
            sys.exit(1)
        print("pod/platform-agent-shell-0")
    sys.exit(0)

cmd = argv[argv.index("--") + 1 :]
target = next(a for a in argv if a.startswith(("deployment/", "pod/")))
script = cmd[2] if cmd[:2] == ["sh", "-c"] else ""
stdin = sys.stdin.read() if cmd[1:2] == ["-"] else ""

if ".user_aligned" in stdin:
    record("state")
    print(os.environ.get("STEP_1_STATE", "clear"))
elif ".bootstrap_scan_filed" in stdin:
    record("old_id")
    old_id = os.environ.get("OLD_ID", "t_old")
    if old_id == "fail":
        sys.stderr.write("error: unable to upgrade connection: container not found\n")
        sys.exit(1)
    print(old_id)
elif "s/^task_id=//p" in script:
    record("sweep_id")
    if os.environ.get("GATE_FILES") == "1" and bump("sweep_reads") >= 1:
        print("t_new")
elif "task_id=$1" in script:
    record("restore")
    if bump("restores") < int(os.environ.get("RESTORE_FAILS", "0")):
        sys.stderr.write("error: unable to upgrade connection: container not found\n")
        sys.exit(1)
elif "rm" in cmd and "/opt/data/.bootstrap_scan_filed" in cmd:
    record("rearm")
elif "rm" in cmd and "/opt/data/INVENTORY.md" in cmd:
    record("clear " + target)
    if os.environ.get("INVENTORY_RM_FAIL") == target:
        sys.stderr.write("error: unable to upgrade connection: container not found\n")
        sys.exit(1)
elif "archive" in cmd:
    record("archive")
    if cmd[-1] in os.environ.get("ARCHIVE_FAIL", "").split():
        sys.stderr.write("error: unable to upgrade connection: container not found\n")
        sys.exit(1)
elif "/proc" in stdin:
    record("gate " + target)
    if target == "pod/gw-evicted":
        sys.exit(1)
    busy = int(os.environ.get("GATE_BUSY_CHECKS", "0"))
    if target == "pod/gw-leader" and bump("leader_checks") < busy:
        print("running")
    else:
        if target == "pod/gw-leader":
            (state / "gate_done").touch()
        print("idle")
elif "task_runs" in stdin:
    record("run_state")
    states = os.environ.get("RUN_STATES", "1 1 1 1").split(";")
    answer = states[min(bump("run_state"), len(states) - 1)]
    if answer == "fail":
        sys.stderr.write("error: unable to upgrade connection: container not found\n")
        sys.exit(1)
    print(answer)
elif "idempotency_key LIKE" in stdin:
    record("open_cards")
    if str(bump("listings") + 1) in os.environ.get("FAILED_LISTINGS", "").split():
        sys.stderr.write("error: unable to upgrade connection: container not found\n")
        sys.exit(1)
    raced = os.environ.get("RACE") == "1" and (state / "gate_done").exists()
    print(os.environ.get("OPEN_CARDS", "t_raced" if raced else ""))
else:
    record()
'''

_GCLOUD_STUB = '#!/bin/bash\necho "gcloud $*" >> "$CALLS"\n'
_SLEEP_STUB = '#!/bin/bash\necho "sleep $*" >> "$CALLS"\n'


def _render_plant(**overrides: str) -> str:
    """The create-time provisioner, with `overrides` on top of _INTERPOLATIONS."""
    return _render(_PLANT_BLOCK, {**_INTERPOLATIONS, **overrides})


def _render_destroy() -> str:
    return _render(_DESTROY_BLOCK, _DESTROY_INTERPOLATIONS)


def _render(block: int, interpolations: dict) -> str:
    """Render a provisioner's command as Terraform would: dedent, protect
    `$${` escapes, substitute interpolations, then restore the escapes."""
    body = textwrap.dedent(_HEREDOC_RE.findall(_MODULE.read_text())[block])
    sentinel = "\x00"
    body = body.replace("$${", sentinel)
    unresolved = []

    def substitute(match: "re.Match[str]") -> str:
        expression = match.group(1).strip()
        if expression not in interpolations:
            unresolved.append(expression)
            return match.group(0)
        return interpolations[expression]

    body = re.sub(r"\$\{([^}]*)\}", substitute, body).replace(sentinel, "${")
    if unresolved:
        raise AssertionError(f"add these to the interpolations: {sorted(set(unresolved))}")
    return body


@unittest.skipUnless(shutil.which("bash"), "no bash on PATH")
class BootstrapDiscoveryPlantTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._plant = _render_plant()
        cls._destroy = _render_destroy()

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = pathlib.Path(self._dir.name)
        self._script = root / "plant.sh"
        self._script.write_text(self._plant)
        self._destroy_script = root / "destroy.sh"
        self._destroy_script.write_text(self._destroy)
        self._stub_dir = root / "bin"
        self._stub_dir.mkdir()
        for name, source in (("kubectl", _KUBECTL_STUB), ("gcloud", _GCLOUD_STUB), ("sleep", _SLEEP_STUB)):
            stub = self._stub_dir / name
            stub.write_text(source)
            stub.chmod(0o755)
        self._state = root / "state"
        self._state.mkdir()
        self._calls = root / "calls"

    def _run(self, script=None, **scenario):
        env = dict(os.environ)
        env["PATH"] = f"{self._stub_dir}{os.pathsep}{env['PATH']}"
        env["CALLS"] = str(self._calls)
        env["STATE"] = str(self._state)
        env.update({k: str(v) for k, v in scenario.items()})
        completed = subprocess.run(
            ["bash", str(script or self._script)], env=env, capture_output=True, text=True, timeout=120
        )
        calls = self._calls.read_text().splitlines() if self._calls.exists() else []
        return completed, calls

    @staticmethod
    def _indices(calls, needle):
        return [i for i, call in enumerate(calls) if call.endswith(needle)]

    def test_bash_syntax_is_valid(self):
        completed = subprocess.run(["bash", "-n", str(self._script)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_step_4_waits_past_a_run_that_ended_before_the_fan_out(self):
        # started, ended, filed, ended after the newest card: a run that ended
        # with nothing filed, then a retry partway through filing, then done.
        completed, calls = self._run(GATE_FILES=1, RUN_STATES="1 1 0 0;2 1 1 0;2 2 3 1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self._indices(calls, "[run_state]")), 3)
        self.assertIn("Sweep card t_new filed 3 Cluster Agent card(s)", completed.stdout)
        self.assertEqual(self._indices(calls, "[archive]"), [])

    def test_step_4_hands_over_on_the_last_good_read_when_later_reads_fail(self):
        # A worker is running and has filed two cards; then every read fails.
        completed, calls = self._run(GATE_FILES=1, RUN_STATES="1 0 2 0;fail")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self._indices(calls, "[run_state]")), 900 // 15 + 1)
        self.assertIn("Board reads after 0s failed", completed.stderr)
        self.assertIn("Sweep card t_new has 0 ended run(s) and 2 Cluster Agent card(s)", completed.stdout)
        self.assertEqual(self._indices(calls, "[archive]"), [])

    def test_step_4_reports_a_board_it_never_read_as_unread(self):
        completed, calls = self._run(GATE_FILES=1, RUN_STATES="fail")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("no read of sweep card t_new's runs from the board succeeded", completed.stderr)
        self.assertNotIn("no worker picked up", completed.stderr)
        self.assertEqual(len(self._indices(calls, "[open_cards]")), 3)

    def test_step_4_reports_a_sweep_no_worker_picked_up(self):
        completed, calls = self._run(GATE_FILES=1, RUN_STATES="0 0 0 0")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("no worker picked up sweep card t_new within 900s", completed.stderr)
        self.assertNotIn("Board reads after", completed.stderr)

    def test_step_1_refuses_a_paused_scan_job_before_re_arming(self):
        completed, calls = self._run(STEP_1_STATE="paused")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("cron job is paused", completed.stderr)
        self.assertEqual([c for c in calls if c.endswith(("[rearm]", "[archive]", "[restore]"))], [])

    def test_the_trap_archives_a_sweep_the_leaders_gate_files_after_the_restore(self):
        # The gate never files within the plant's wait, so step 3 fails. The
        # leader's gate run is still going when the trap starts and files its
        # sweep as it exits.
        completed, calls = self._run(GATE_FILES=0, GATE_BUSY_CHECKS=2, RACE=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("filed no sweep card", completed.stderr)
        restore = self._indices(calls, "[restore]")
        leader = self._indices(calls, "[gate pod/gw-leader]")
        archive = [i for i, call in enumerate(calls) if call.endswith("archive t_raced [archive]")]
        self.assertEqual(len(restore), 1)
        self.assertTrue(calls[restore[0]].endswith(" sh t_old [restore]"), calls[restore[0]])
        self.assertEqual(len(leader), 3, calls)
        self.assertEqual(len(archive), 1, calls)
        self.assertLess(restore[0], leader[0])
        self.assertLess(leader[-1], archive[0])

    def test_the_trap_waits_out_the_ceiling_while_the_leader_stays_busy(self):
        # The leader never answers idle, so the trap waits the full gate_wait,
        # then still archives what it finds.
        completed, calls = self._run(GATE_FILES=0, GATE_BUSY_CHECKS=1000, RACE=0)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(len(self._indices(calls, "[gate pod/gw-leader]")), 300 // 5 + 1)
        self.assertEqual(len(self._indices(calls, "[open_cards]")), 3)

    def test_the_trap_writes_no_marker_on_an_install_whose_gate_was_open(self):
        # Nothing had closed the gate on this install, so a marker would stop
        # it filing its own sweep. The sweep it raced in is still archived.
        completed, calls = self._run(GATE_FILES=0, OLD_ID="", RACE=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("previous sweep card: none", completed.stdout)
        self.assertEqual(self._indices(calls, "[restore]"), [])
        self.assertEqual(len([c for c in calls if c.endswith("archive t_raced [archive]")]), 1, calls)

    def test_the_trap_closes_a_gate_only_an_inventory_file_had_closed(self):
        completed, calls = self._run(GATE_FILES=0, OLD_ID="none", RACE=0)
        self.assertNotEqual(completed.returncode, 0)
        restore = self._indices(calls, "[restore]")
        self.assertEqual(len(restore), 1, calls)
        self.assertTrue(calls[restore[0]].endswith(" sh none [restore]"), calls[restore[0]])

    def test_a_failed_read_of_the_gate_stops_the_apply_before_re_arming(self):
        completed, calls = self._run(OLD_ID="fail")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(len(self._indices(calls, "[old_id]")), 1)
        self.assertNotIn("Plant failed", completed.stderr)
        self.assertEqual([c for c in calls if c.endswith(("[rearm]", "[archive]", "[restore]"))], [])

    def test_a_failed_sandbox_listing_stops_the_apply_before_the_marker_goes(self):
        completed, calls = self._run(SANDBOX_LIST_FAIL="1")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Plant failed", completed.stderr)
        self.assertEqual(self._indices(calls, "[rearm]"), [])
        self.assertEqual(len(self._indices(calls, "[clear deployment/platform-agent-gateway]")), 2)
        self.assertIn("Cleanup incomplete: could not remove the INVENTORY files.", completed.stderr)

    def test_a_failed_sandbox_rm_stops_the_apply_before_the_marker_goes(self):
        completed, calls = self._run(INVENTORY_RM_FAIL="pod/platform-agent-shell-0")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._indices(calls, "[rearm]"), [])
        self.assertEqual(len(self._indices(calls, "[clear deployment/platform-agent-gateway]")), 2)
        self.assertIn("Cleanup incomplete: could not remove the INVENTORY files.", completed.stderr)

    def test_a_failed_card_listing_stops_the_apply_before_the_marker_goes(self):
        completed, calls = self._run(GATE_FILES=0, OPEN_CARDS="t_prev", FAILED_LISTINGS="1")
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("still open after archiving", completed.stderr)
        listings = self._indices(calls, "[open_cards]")
        restore = self._indices(calls, "[restore]")
        self.assertLess(listings[0], restore[0])
        self.assertLess(restore[0], listings[1])
        self.assertEqual(self._indices(calls, "[rearm]"), [])

    def test_every_exec_into_the_deployment_bounds_its_wait_for_a_pod(self):
        # With no pod, kubectl waits 60s by default for one to be created, so
        # every retry in the trap and steps 3 and 4 would take a minute.
        completed, calls = self._run(GATE_FILES=0, RESTORE_FAILS=1)
        self.assertNotEqual(completed.returncode, 0)
        execs = [call for call in calls if call.startswith("kubectl exec") and "deployment/" in call]
        self.assertTrue(any(call.endswith("[restore]") for call in execs), calls)
        self.assertTrue(any(call.endswith("[old_id]") for call in execs), calls)
        for call in execs:
            self.assertIn("--pod-running-timeout=5s", call)

    def test_the_trap_retries_the_marker_restore_while_the_agent_is_down(self):
        completed, calls = self._run(GATE_FILES=0, RESTORE_FAILS=2)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(len(self._indices(calls, "[restore]")), 3)
        self.assertEqual(calls.count("sleep 5"), 2)
        self.assertNotIn("Cleanup incomplete", completed.stderr)

    def test_the_trap_waits_a_full_gate_run_after_a_late_restore(self):
        # The restore fails for 100 s while the leader's gate run, which read
        # the marker absent, runs for another 250 s and files as it exits.
        completed, calls = self._run(GATE_FILES=0, RESTORE_FAILS=20, GATE_BUSY_CHECKS=50, RACE=1)
        self.assertNotEqual(completed.returncode, 0)
        archive = [i for i, call in enumerate(calls) if call.endswith("archive t_raced [archive]")]
        self.assertEqual(len(self._indices(calls, "[restore]")), 21)
        self.assertEqual(len(self._indices(calls, "[gate pod/gw-leader]")), 51)
        self.assertEqual(len(archive), 1, calls)
        self.assertNotIn("Cleanup incomplete", completed.stderr)

    def test_the_trap_names_a_marker_restore_it_never_managed(self):
        completed, calls = self._run(GATE_FILES=0, RESTORE_FAILS=1000, NO_PODS=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(calls.count("sleep 5"), 60)
        self.assertEqual(len(self._indices(calls, "[open_cards]")), 3)
        self.assertIn("Cleanup incomplete: could not put back the sweep marker.", completed.stderr)

    def test_the_trap_names_a_card_it_could_not_archive(self):
        completed, _ = self._run(GATE_FILES=0, RACE=1, ARCHIVE_FAIL="t_raced")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Cleanup incomplete: could not archive t_raced.", completed.stderr)

    # Step 2 lists the cards twice, so the trap's listings are the third on.
    def test_the_trap_retries_a_card_listing_that_fails(self):
        completed, calls = self._run(GATE_FILES=0, RACE=1, FAILED_LISTINGS="3")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(len(self._indices(calls, "[open_cards]")), 4)
        self.assertEqual(len([c for c in calls if c.endswith("archive t_raced [archive]")]), 1, calls)
        self.assertNotIn("Cleanup incomplete", completed.stderr)

    def test_the_trap_names_a_card_listing_it_never_got(self):
        completed, calls = self._run(GATE_FILES=0, RACE=1, FAILED_LISTINGS="3 4 5")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(len(self._indices(calls, "[open_cards]")), 5)
        self.assertEqual(self._indices(calls, "[archive]"), [])
        self.assertIn("Cleanup incomplete: could not list the open cards.", completed.stderr)
        self.assertEqual(len(self._indices(calls, "[clear pod/platform-agent-shell-0]")), 2)

    def test_the_destroy_clears_the_inventory_files_when_it_cannot_list_the_cards(self):
        completed, calls = self._run(script=self._destroy_script, FAILED_LISTINGS="1")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("Cleanup incomplete: could not list the open cards.", completed.stderr)
        self.assertEqual(len(self._indices(calls, "[clear deployment/platform-agent-gateway]")), 1)
        self.assertEqual(len(self._indices(calls, "[clear pod/platform-agent-shell-0]")), 1)

    def test_the_destroy_clears_the_sandbox_when_the_agents_rm_fails(self):
        completed, calls = self._run(
            script=self._destroy_script, INVENTORY_RM_FAIL="deployment/platform-agent-gateway"
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("Cleanup incomplete: could not remove the agent's INVENTORY files.", completed.stderr)
        self.assertEqual(len(self._indices(calls, "[clear pod/platform-agent-shell-0]")), 1)

    def test_the_destroy_archives_the_rest_when_one_archive_fails(self):
        completed, calls = self._run(script=self._destroy_script, OPEN_CARDS="t_a t_b", ARCHIVE_FAIL="t_a")
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(len([c for c in calls if c.endswith("archive t_b [archive]")]), 1, calls)
        self.assertIn("Cleanup incomplete: could not archive t_a.", completed.stderr)

    def test_the_destroy_names_a_sandbox_pod_it_could_not_clear(self):
        completed, _ = self._run(script=self._destroy_script, INVENTORY_RM_FAIL="pod/platform-agent-shell-0")
        self.assertEqual(completed.returncode, 1)
        self.assertIn(
            "Cleanup incomplete: could not remove the INVENTORY files from pod/platform-agent-shell-0.",
            completed.stderr,
        )

    def test_the_destroy_names_a_sandbox_listing_it_never_got(self):
        completed, calls = self._run(script=self._destroy_script, SANDBOX_LIST_FAIL="1")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("Cleanup incomplete: could not list the sandbox pods.", completed.stderr)
        self.assertEqual(len(self._indices(calls, "[clear deployment/platform-agent-gateway]")), 1)

    def test_the_trap_waits_out_the_ceiling_when_no_gateway_pod_is_listed(self):
        completed, calls = self._run(GATE_FILES=0, NO_PODS=1, RACE=0)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual([c for c in calls if "[gate " in c], [])
        self.assertEqual(calls.count("sleep 5"), 300 // 5)
        self.assertEqual(len(self._indices(calls, "[open_cards]")), 3)


class Step1StateQueryTest(unittest.TestCase):
    """Step 1's read, run under the test interpreter against a data dir."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._home = pathlib.Path(self._dir.name)
        (self._home / "cron").mkdir()
        self._script = _STEP_1_RE.search(_render_plant(**{"local.home": self._dir.name})).group(1)

    def _state(self, jobs=None, markers=()):
        if jobs is not None:
            (self._home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))
        for marker in markers:
            (self._home / marker).touch()
        completed = subprocess.run(
            [sys.executable, "-", _INTERPOLATIONS["local.scan_job"]],
            input=self._script,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    def test_an_enabled_scan_job_reads_clear(self):
        self.assertEqual(self._state([{"id": "other", "enabled": False}, {"id": "bootstrap-inventory-scan", "enabled": True}]), "clear")

    def test_a_paused_scan_job_reads_paused(self):
        self.assertEqual(self._state([{"id": "bootstrap-inventory-scan", "enabled": False}]), "paused")

    def test_a_missing_scan_job_reads_nojob(self):
        self.assertEqual(self._state([{"id": "other", "enabled": True}]), "nojob")
        (self._home / "cron" / "jobs.json").unlink()
        self.assertEqual(self._state(), "nojob")

    def test_the_markers_are_read_before_the_job(self):
        self.assertEqual(self._state([], markers=[".bootstrap_completed"]), "completed")
        self.assertEqual(self._state(markers=[".user_aligned"]), "aligned")


class Step2GateQueryTest(unittest.TestCase):
    """Step 2's read of what closed the gate, run under the test interpreter
    against a data dir."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._home = pathlib.Path(self._dir.name)
        home = self._dir.name
        overrides = {"local.home": home, "local.inventory": f"{home}/INVENTORY.raw.md {home}/INVENTORY.md"}
        self._script = _STEP_2_RE.search(_render_plant(**overrides)).group(1)

    def _old_id(self, files=None):
        for name, text in (files or {}).items():
            (self._home / name).write_text(text)
        completed = subprocess.run(
            [sys.executable, "-"], input=self._script, capture_output=True, text=True, check=True
        )
        return completed.stdout.strip()

    def test_a_marker_reads_as_its_task_id(self):
        self.assertEqual(self._old_id({".bootstrap_scan_filed": "task_id=t_1\nfiled_at=1\n"}), "t_1")

    def test_a_marker_without_a_task_id_reads_none(self):
        self.assertEqual(self._old_id({".bootstrap_scan_filed": ""}), "none")

    def test_an_inventory_file_alone_reads_none(self):
        for name in ("INVENTORY.raw.md", "INVENTORY.md"):
            with self.subTest(name=name):
                self.assertEqual(self._old_id({name: "report"}), "none")
                (self._home / name).unlink()

    def test_an_open_gate_reads_empty(self):
        self.assertEqual(self._old_id(), "")

    def test_a_marker_it_cannot_read_fails_the_read(self):
        (self._home / ".bootstrap_scan_filed").mkdir()
        with self.assertRaises(subprocess.CalledProcessError):
            self._old_id()


class RunStateQueryTest(unittest.TestCase):
    """Step 4's board query, run under the test interpreter against a board
    shaped the way the install's hermes writes it."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls._home = cls._dir.name
        cls._script = _RUN_STATE_RE.search(_render_plant(**{"local.home": cls._home})).group(1)

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def _query(self, runs, children, prioritize_at=None, other_runs=()):
        board = pathlib.Path(self._home) / "kanban.db"
        board.unlink(missing_ok=True)
        cards = [(f"t_child{i}", _CLUSTER_KEY + str(i), at) for i, at in enumerate(children or [])]
        if prioritize_at is not None:
            cards.append(("t_prioritize", "bootstrap-inventory-prioritize", prioritize_at))
        with sqlite3.connect(board) as conn:
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, idempotency_key TEXT)")
            conn.executemany("INSERT INTO tasks VALUES (?, ?)", [(tid, key) for tid, key, _ in cards])
            conn.execute(
                "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, "
                "started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT, summary TEXT)"
            )
            conn.executemany(
                "INSERT INTO task_runs (task_id, started_at, ended_at, outcome, summary) VALUES (?, ?, ?, ?, ?)",
                [(_SWEEP, *run) for run in runs] + list(other_runs),
            )
            if children is not None:
                conn.execute(
                    "CREATE TABLE kanban_worker_children (child_id TEXT PRIMARY KEY, "
                    "creator_id TEXT NOT NULL, created_at INTEGER NOT NULL)"
                )
                conn.executemany(
                    "INSERT INTO kanban_worker_children VALUES (?, ?, ?)",
                    [(tid, _SWEEP, at) for tid, _, at in cards],
                )
        completed = subprocess.run(
            [
                sys.executable,
                "-",
                _SWEEP,
                _INTERPOLATIONS["local.cluster_key_like"],
                _INTERPOLATIONS["local.rate_limit_block"],
            ],
            input=self._script,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    def test_a_board_with_no_children_table_counts_nothing_filed(self):
        self.assertEqual(self._query([(100, 110, "blocked", "no roster")], None), "1 1 0 0")

    def test_a_run_still_going_counts_no_end(self):
        self.assertEqual(self._query([(100, None, None, None)], [150, 200]), "1 0 2 0")

    def test_a_run_that_ended_before_the_newest_card_does_not_count(self):
        self.assertEqual(self._query([(100, 150, "completed", "done")], [120, 200]), "1 1 2 0")

    def test_a_completed_run_ending_with_the_newest_card_counts(self):
        self.assertEqual(self._query([(100, 200, "completed", "done")], [120, 200]), "1 1 2 1")

    def test_a_prioritize_card_filed_after_the_run_completed_does_not_hold_it(self):
        # The shape a live sweep left: four cluster cards, the run completed,
        # then the prioritize card two seconds later.
        runs = [(295, 458, "completed", "done")]
        self.assertEqual(self._query(runs, [314, 314, 315, 315], prioritize_at=460), "1 1 4 1")

    def test_a_block_the_worker_chose_counts(self):
        self.assertEqual(self._query([(100, 210, "blocked", "Waiting for 4 child tasks")], [120, 200]), "1 1 2 1")

    def test_a_run_the_worker_did_not_end_itself_does_not_count(self):
        prefix = _INTERPOLATIONS["local.rate_limit_block"]
        for outcome, summary in (
            ("blocked", prefix + " (failure_reason=rate_limit): 429"),
            ("reclaimed", "worker process gone"),
            ("crashed", None),
            ("timed_out", None),
        ):
            with self.subTest(outcome=outcome):
                self.assertEqual(self._query([(100, 210, outcome, summary)], [120, 200]), "1 1 2 0")

    def test_the_retry_that_completes_counts_after_a_partial_run_was_reclaimed(self):
        runs = [(100, 210, "reclaimed", None), (220, 400, "completed", "done")]
        self.assertEqual(self._query(runs, [120, 200, 300, 350]), "2 2 4 1")

    def test_a_run_that_completed_without_filing_a_cluster_card_counts(self):
        # A build whose worker files only the prioritize card, or nothing, then completes.
        runs = [(100, 160, "completed", "done")]
        self.assertEqual(self._query(runs, [], prioritize_at=150), "1 1 0 1")
        self.assertEqual(self._query(runs, None), "1 1 0 1")

    def test_with_no_cluster_card_only_a_completed_run_counts(self):
        prefix = _INTERPOLATIONS["local.rate_limit_block"]
        for outcome, summary in (
            ("blocked", "Waiting for the roster"),
            ("blocked", prefix + " (failure_reason=rate_limit): 429"),
            ("reclaimed", "worker process gone"),
            ("crashed", None),
            ("timed_out", None),
        ):
            with self.subTest(outcome=outcome, summary=summary):
                self.assertEqual(self._query([(100, 210, outcome, summary)], [], prioritize_at=150), "1 1 0 0")

    def test_a_run_on_another_card_does_not_count(self):
        # Re-arming archives the previous sweep card, which keeps its completed run.
        old = [("t_old_sweep", 50, 900, "completed", "done")]
        self.assertEqual(self._query([(100, 210, "blocked", "Waiting for the roster")], [], other_runs=old), "1 1 0 0")
        self.assertEqual(self._query([(100, 150, "completed", "done")], [120, 200], other_runs=old), "1 1 2 0")


if __name__ == "__main__":
    unittest.main()
