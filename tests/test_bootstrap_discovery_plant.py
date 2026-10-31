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

  1. Step 4 must not hand over before the hand-off has: it waits for the
     hand-off's own ranking card, or for the sweep and its Cluster Agent cards to
     stay settled for the hold, and a failed board read keeps the last state
     rather than ending the wait.
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
destroy carrying on past a failed step and naming it, the trap ignoring a
signal that arrives during its cleanup, and every exec into the agent
Deployment bounding its wait for a pod. As in
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
_HANDOFF_STATE_RE = re.compile(r"^handoff_state\(\) \{\n  agent_py [^\n]*<<'PY'\n(.*?)\nPY\n", re.DOTALL | re.MULTILINE)
_STEP_1_RE = re.compile(r"^state=\"\$\(agent_py [^\n]*<<'PY' \|\| true\n(.*?)\nPY\n", re.S | re.M)
_STEP_2_RE = re.compile(r"^old_id=\"\$\(agent_py <<'PY'\n(.*?)\nPY\n", re.S | re.M)
_PLANT_BLOCK = 0
_DESTROY_BLOCK = 1
_SWEEP = "t_sweep"
_CLUSTER_KEY = "bootstrap-inventory-cluster-cluster-example-project-"

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
    "local.gate_script": "oobe.py bootstrap_scan_gate.py",
    "local.gate_wait": "300",
    "local.list_tries": "3",
    "local.list_wait": "5",
    "local.pod_wait": "5",
    "local.scan_job": "oobe",
    "local.prioritize_key": "bootstrap-inventory-prioritize",
    "local.handoff_wait": "1800",
    "local.settle_hold": "120",
    "local.cap_file": "/opt/data/.first_run_cluster_cards",
    "local.cap_mark": "/opt/data/.first_run_cluster_cards.bench",
    # first_run_cluster_cards null, the stack's default; both bench cases set it.
    "local.cards_cap": "",
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
    "self.triggers.pod_wait": _INTERPOLATIONS["local.pod_wait"],
    "self.triggers.home": _INTERPOLATIONS["local.home"],
    "self.triggers.hermes": _INTERPOLATIONS["local.hermes"],
    "self.triggers.python": _INTERPOLATIONS["local.python"],
    "self.triggers.key_like": _INTERPOLATIONS["local.key_like"],
    "self.triggers.inventory": _INTERPOLATIONS["local.inventory"],
    "self.triggers.cap_file": _INTERPOLATIONS["local.cap_file"],
    "self.triggers.cards_cap": _INTERPOLATIONS["local.cards_cap"],
    "self.triggers.cap_mark": _INTERPOLATIONS["local.cap_mark"],
}

# Records every call to $CALLS, tagging the in-pod Python by what it reads, and
# keeps scenario state as files under $STATE. The leader pod is listed second so
# that a check of the first pod alone reads idle. Without the Running filter the
# listing also returns an evicted pod whose exec fails.
_KUBECTL_STUB = r'''#!/usr/bin/env python3
import os, pathlib, signal, sys

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
elif "task_id[[:space:]]*=" in script:
    record("sweep_id")
    if os.environ.get("GATE_FILES") == "1" and bump("sweep_reads") >= 1:
        print("t_new")
elif "task_id=$1" in script:
    record("restore")
    if bump("restores") < int(os.environ.get("RESTORE_FAILS", "0")):
        sys.stderr.write("error: unable to upgrade connection: container not found\n")
        sys.exit(1)
    if os.environ.get("SIGNAL_ON_RESTORE"):
        os.kill(os.getppid(), getattr(signal, "SIG" + os.environ["SIGNAL_ON_RESTORE"]))
elif script == 'rm -f "$1" && rm -f "$2"':
    # The trap's, the destroy's and step 2's removal of a bench cap: one exec,
    # the cap and then the sentinel, so the sentinel outlives a failed cap rm.
    record("uncap+unmark")
    if os.environ.get("UNCAP_FAIL") == "1":
        sys.stderr.write("error: unable to upgrade connection: container not found\n")
        sys.exit(1)
elif "/opt/data/.first_run_cluster_cards.bench" in cmd:
    if "echo bench" in script:
        record("cap_state")
        answer = os.environ.get("CAP_STATE", "none")
        if answer == "fail":
            sys.stderr.write("error: unable to upgrade connection: container not found\n")
            sys.exit(1)
        print(answer)
    else:
        record("unmark" if "rm" in cmd else "mark")
elif "/opt/data/.first_run_cluster_cards" in cmd:
    record("uncap" if "rm" in cmd else "cap")
    if os.environ.get("UNCAP_FAIL" if "rm" in cmd else "CAP_FAIL") == "1":
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
elif "bootstrap-inventory-prioritize" in cmd:
    record("handoff_state")
    states = os.environ.get("HANDOFF_STATES", "1 1 0").split(";")
    answer = states[min(bump("handoff_state"), len(states) - 1)]
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


def _render_destroy(**overrides: str) -> str:
    return _render(_DESTROY_BLOCK, {**_DESTROY_INTERPOLATIONS, **overrides})


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

    def _clear(self):
        """Forget the calls and state of an earlier _run in the same test."""
        self._calls.unlink(missing_ok=True)
        shutil.rmtree(self._state)
        self._state.mkdir()

    @staticmethod
    def _indices(calls, needle):
        return [i for i, call in enumerate(calls) if call.endswith(needle)]

    def test_bash_syntax_is_valid(self):
        completed = subprocess.run(["bash", "-n", str(self._script)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def _run_handoff(self, **scenario):
        return self._run(GATE_FILES=1, **scenario)

    def test_handoff_returns_once_the_ranking_card_is_keyed(self):
        completed, calls = self._run_handoff(HANDOFF_STATES="1 0 0;1 0 0;1 1 0")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self._indices(calls, "[handoff_state]")), 3)
        self.assertIn("a card keyed bootstrap-inventory-prioritize is on the board after 30s", completed.stdout)
        self.assertEqual(self._indices(calls, "[archive]"), [])

    def test_handoff_returns_after_the_cards_stay_settled_for_the_hold(self):
        completed, calls = self._run_handoff(HANDOFF_STATES="1 0 0;1 0 1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # Settled from the read at 15s; 120s later is the read at 135s.
        self.assertEqual(len(self._indices(calls, "[handoff_state]")), 135 // 15 + 1)
        self.assertIn("settled for 120s with no card keyed bootstrap-inventory-prioritize", completed.stdout)

    def test_handoff_restarts_the_hold_when_a_card_unsettles(self):
        completed, calls = self._run_handoff(HANDOFF_STATES="1 0 1;1 0 1;1 0 0;1 0 1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # Settled again from the read at 45s, so the hold ends at 165s.
        self.assertEqual(len(self._indices(calls, "[handoff_state]")), 165 // 15 + 1)

    def test_handoff_keeps_the_hold_across_a_failed_read(self):
        completed, calls = self._run_handoff(HANDOFF_STATES="1 0 1;fail;1 0 1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self._indices(calls, "[handoff_state]")), 120 // 15 + 1)

    def test_handoff_hands_over_at_its_ceiling(self):
        completed, calls = self._run_handoff(HANDOFF_STATES="1 0 0")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self._indices(calls, "[handoff_state]")), 1800 // 15 + 1)
        self.assertIn("No card keyed bootstrap-inventory-prioritize", completed.stdout)
        self.assertEqual(self._indices(calls, "[archive]"), [])

    def test_handoff_reports_a_sweep_no_worker_picked_up(self):
        completed, calls = self._run_handoff(HANDOFF_STATES="0 0 0")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("no worker picked up sweep card t_new within 900s", completed.stderr)
        self.assertEqual(len(self._indices(calls, "[handoff_state]")), 900 // 15 + 1)

    def test_handoff_reports_a_board_it_never_read_as_unread(self):
        completed, calls = self._run_handoff(HANDOFF_STATES="fail")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("no read of sweep card t_new's hand-off from the board succeeded", completed.stderr)
        self.assertEqual(len(self._indices(calls, "[open_cards]")), 3)

    def test_step_1_refuses_every_state_but_clear_before_re_arming(self):
        # "" is a step-1 read that failed: only the catch-all arm stops it.
        refusals = {
            "aligned": ".user_aligned exists",
            "completed": "onboarding already delivered",
            "nojob": "is not in",
            "paused": "cron job is paused",
            "": "could not read the onboarding markers",
        }
        for state, message in refusals.items():
            with self.subTest(state=state):
                self._clear()
                completed, calls = self._run(STEP_1_STATE=state)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(message, completed.stderr)
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
        # With no pod, each of these would wait kubectl's default 60s to fail.
        completed, plant_calls = self._run(GATE_FILES=0, RESTORE_FAILS=1)
        self.assertNotEqual(completed.returncode, 0)
        destroyed, calls = self._run(self._destroy_script, OPEN_CARDS="t_prev")
        self.assertEqual(destroyed.returncode, 0, destroyed.stderr)
        destroy_calls = calls[len(plant_calls) :]
        for tag in ("[restore]", "[old_id]"):
            self.assertTrue(any(call.endswith(tag) for call in plant_calls), tag)
        for tag in ("[open_cards]", "archive t_prev [archive]", "[clear deployment/platform-agent-gateway]"):
            self.assertTrue(any(call.endswith(tag) for call in destroy_calls), tag)
        execs = [call for call in calls if call.startswith("kubectl exec") and "deployment/" in call]
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

    def test_a_signal_during_the_trap_does_not_cut_its_cleanup_short(self):
        for name in ("TERM", "INT"):
            with self.subTest(signal=name):
                self._clear()
                completed, calls = self._run(GATE_FILES=0, RACE=1, SIGNAL_ON_RESTORE=name)
                self.assertNotEqual(completed.returncode, 0)
                restore = self._indices(calls, "[restore]")
                archive = self._indices(calls, "archive t_raced [archive]")
                self.assertEqual(len(restore), 1)
                self.assertEqual(len(archive), 1, calls)
                self.assertGreater(archive[0], restore[0])

    def test_the_trap_names_a_marker_restore_it_never_managed(self):
        completed, calls = self._run(GATE_FILES=0, RESTORE_FAILS=1000, NO_PODS=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(calls.count("sleep 5"), 2 * 300 // 5)
        self.assertEqual(len(self._indices(calls, "[open_cards]")), 3)
        self.assertIn("Cleanup incomplete: could not put back the sweep marker.", completed.stderr)

    def test_the_trap_waits_for_the_gate_after_a_restore_it_never_managed(self):
        # The restore fails for the whole gate_wait while the leader's gate run
        # is still going; that run files as it exits, and the trap archives it.
        completed, calls = self._run(GATE_FILES=0, RESTORE_FAILS=1000, GATE_BUSY_CHECKS=10, RACE=1)
        self.assertNotEqual(completed.returncode, 0)
        archive = [i for i, call in enumerate(calls) if call.endswith("archive t_raced [archive]")]
        self.assertEqual(len(self._indices(calls, "[gate pod/gw-leader]")), 11)
        self.assertEqual(len(archive), 1, calls)
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

    # --- first_run_cluster_cards (#3702) ------------------------------------

    def _capped(self):
        """Swap in the provisioners as rendered with first_run_cluster_cards = 2."""
        self._script.write_text(_render_plant(**{"local.cards_cap": "2"}))
        self._destroy_script.write_text(_render_destroy(**{"self.triggers.cards_cap": "2"}))

    def test_an_unset_cap_writes_and_removes_no_file(self):
        completed, calls = self._run_handoff()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        failed, _ = self._run(GATE_FILES=0)
        self.assertNotEqual(failed.returncode, 0)
        destroyed, calls = self._run(self._destroy_script)
        self.assertEqual(destroyed.returncode, 0, destroyed.stderr)
        self.assertEqual([c for c in calls if c.endswith(("[cap]", "[uncap]", "[mark]", "[unmark]", "[uncap+unmark]"))], [])

    def test_a_set_cap_is_written_before_the_re_arm(self):
        self._capped()
        completed, calls = self._run_handoff()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        cap = self._indices(calls, "[cap]")
        self.assertEqual(len(cap), 1, calls)
        # The sentinel goes in first, so a cap on the install is always marked
        # as this stack's, and only after the read that found no cap there.
        mark = self._indices(calls, "[mark]")
        self.assertEqual(len(mark), 1, calls)
        self.assertLess(self._indices(calls, "[cap_state]")[0], mark[0])
        self.assertLess(mark[0], cap[0])
        # The sentinel holds the cap's value, so step 2 can tell it from an
        # operator's cap written beside a sentinel orphaned later.
        self.assertIn(" sh 2 /opt/data/.first_run_cluster_cards.bench [mark]", calls[mark[0]])
        self.assertIn(" sh 2 /opt/data/.first_run_cluster_cards [cap]", calls[cap[0]])
        self.assertIn('printf "%s\\n" "$1" > "$2"', calls[mark[0]])
        self.assertIn('printf "%s\\n" "$1" > "$2"', calls[cap[0]])
        # Before the gate can act: ahead of the card archive and the marker removal.
        self.assertLess(self._indices(calls, "[old_id]")[0], cap[0])
        self.assertLess(cap[0], self._indices(calls, "[open_cards]")[0])
        self.assertLess(cap[0], self._indices(calls, "[rearm]")[0])
        # A plant that succeeds leaves it for the verifier; the destroy removes it.
        self.assertEqual(self._indices(calls, "[uncap+unmark]"), [])
        self.assertIn("First-run cluster card cap set to 2", completed.stdout)

    def _assert_cap_then_sentinel_removed(self, calls):
        # One exec, cap first: the sentinel's rm runs only after the cap's
        # succeeded, in the pod, with no second exec to fail between them.
        removed = self._indices(calls, "[uncap+unmark]")
        self.assertEqual(len(removed), 1, calls)
        self.assertEqual(self._indices(calls, "[uncap]") + self._indices(calls, "[unmark]"), [], calls)
        self.assertIn(
            ' rm -f "$1" && rm -f "$2" sh /opt/data/.first_run_cluster_cards /opt/data/.first_run_cluster_cards.bench [uncap+unmark]',
            calls[removed[0]],
        )

    def test_the_destroy_removes_a_cap_it_set_then_its_sentinel(self):
        self._capped()
        completed, calls = self._run(self._destroy_script)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self._assert_cap_then_sentinel_removed(calls)

    def test_a_destroy_that_cannot_remove_the_cap_keeps_the_sentinel(self):
        # Marked, the cap is removed by the next run's step 2, which the message says.
        self._capped()
        completed, calls = self._run(self._destroy_script, UNCAP_FAIL=1)
        self.assertNotEqual(completed.returncode, 0)
        self._assert_cap_then_sentinel_removed(calls)
        self.assertIn("remove /opt/data/.first_run_cluster_cards and /opt/data/.first_run_cluster_cards.bench", completed.stderr)
        self.assertIn("The next run's step 2, capped or not", completed.stderr)

    def test_the_trap_removes_the_cap_then_its_sentinel_after_a_failed_plant(self):
        self._capped()
        completed, calls = self._run(GATE_FILES=0)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("filed no sweep card", completed.stderr)
        self._assert_cap_then_sentinel_removed(calls)

    def test_a_trap_that_cannot_remove_the_cap_keeps_the_sentinel(self):
        self._capped()
        completed, calls = self._run(GATE_FILES=0, UNCAP_FAIL=1)
        self.assertNotEqual(completed.returncode, 0)
        self._assert_cap_then_sentinel_removed(calls)
        self.assertIn("Cleanup incomplete", completed.stderr)
        self.assertIn("The next run's step 2, capped or not", completed.stderr)

    def _nothing_changed(self, calls):
        for tag in ("[cap]", "[mark]", "[uncap]", "[unmark]", "[uncap+unmark]", "[archive]", "[rearm]", "[open_cards]"):
            self.assertEqual(self._indices(calls, tag), [], tag)
        self.assertEqual([c for c in calls if "[clear " in c], [])

    def test_an_uncapped_run_removes_a_cap_an_earlier_run_left(self):
        # A run with no cap of its own; a capped run's leftover would
        # otherwise bound its sweep.
        completed, calls = self._run_handoff(CAP_STATE="bench")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self._assert_cap_then_sentinel_removed(calls)
        self.assertLess(self._indices(calls, "[uncap+unmark]")[0], self._indices(calls, "[open_cards]")[0])
        self.assertEqual(self._indices(calls, "[cap]") + self._indices(calls, "[mark]"), [])
        self.assertIn("Removed the first-run cluster card cap an earlier run of this stack left", completed.stdout)

    def test_a_capped_run_replaces_a_cap_an_earlier_run_left(self):
        self._capped()
        completed, calls = self._run_handoff(CAP_STATE="bench")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        order = [self._indices(calls, tag)[0] for tag in ("[uncap+unmark]", "[mark]", "[cap]")]
        self.assertEqual(order, sorted(order), calls)

    def test_a_capped_run_refuses_an_operators_cap_file(self):
        self._capped()
        completed, calls = self._run_handoff(CAP_STATE="operator")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("ERROR: /opt/data/.first_run_cluster_cards exists", completed.stderr)
        self.assertIn("operator's first-run cluster card cap", completed.stderr)
        self._nothing_changed(calls)

    def test_an_uncapped_run_leaves_an_operators_cap_file_alone(self):
        completed, calls = self._run_handoff(CAP_STATE="operator")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual([c for c in calls if c.endswith(("[cap]", "[uncap]", "[mark]", "[unmark]", "[uncap+unmark]"))], [])
        # The run's sweep is bounded by that cap, so the log says so.
        self.assertIn(
            "Note: /opt/data/.first_run_cluster_cards exists on platform-agent-host and this stack did not write it; "
            "the sweep this run files is bounded by that cap.",
            completed.stdout,
        )

    def test_an_orphaned_sentinel_with_no_cap_is_removed_and_nothing_else(self):
        completed, calls = self._run_handoff(CAP_STATE="orphan none")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual([c for c in calls if c.endswith(("[cap]", "[uncap]", "[mark]", "[unmark]", "[uncap+unmark]"))],
                         [c for c in calls if c.endswith("[unmark]")])
        self.assertEqual(len(self._indices(calls, "[unmark]")), 1, calls)
        self.assertIn("left without its cap.", completed.stdout)
        self.assertNotIn("Note:", completed.stdout)

    def test_an_orphaned_sentinel_beside_another_cap_leaves_that_cap_as_an_operators(self):
        # The sentinel goes; the cap file it does not match is an operator's,
        # so a capped run refuses and an uncapped one names it.
        for capped in (True, False):
            with self.subTest(capped=capped):
                self._clear()
                if capped:
                    self._capped()
                else:
                    self._script.write_text(self._plant)
                completed, calls = self._run_handoff(CAP_STATE="orphan operator")
                self.assertEqual(len(self._indices(calls, "[unmark]")), 1, calls)
                self.assertEqual(self._indices(calls, "[uncap]") + self._indices(calls, "[uncap+unmark]"), [])
                self.assertIn("holds another value, so it is an operator's", completed.stdout)
                if capped:
                    self.assertEqual(completed.returncode, 1, completed.stderr)
                    self.assertIn("operator's first-run cluster card cap", completed.stderr)
                    for tag in ("[cap]", "[mark]", "[archive]", "[rearm]", "[open_cards]"):
                        self.assertEqual(self._indices(calls, tag), [], tag)
                else:
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertIn("Note: /opt/data/.first_run_cluster_cards exists", completed.stdout)

    def test_an_unknown_cap_read_stops_the_apply_before_anything_changes(self):
        completed, calls = self._run_handoff(CAP_STATE="bench operator")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("could not read /opt/data/.first_run_cluster_cards", completed.stderr)
        self._nothing_changed(calls)

    def test_a_failed_read_of_the_cap_file_stops_the_apply_before_anything_changes(self):
        for capped in (False, True):
            with self.subTest(capped=capped):
                self._clear()
                if capped:
                    self._capped()
                completed, calls = self._run_handoff(CAP_STATE="fail")
                self.assertEqual(completed.returncode, 1, completed.stderr)
                # The read fails into the case's ERROR arm, as step 1's does.
                self.assertIn("ERROR: could not read /opt/data/.first_run_cluster_cards or "
                              "/opt/data/.first_run_cluster_cards.bench on platform-agent-host (got '')", completed.stderr)
                self._nothing_changed(calls)

    def test_a_cap_write_that_fails_stops_the_apply_and_is_removed(self):
        self._capped()
        completed, calls = self._run(CAP_FAIL=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._indices(calls, "[rearm]"), [])
        self.assertEqual(len(self._indices(calls, "[uncap+unmark]")), 1, calls)


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
        self.assertEqual(self._state([{"id": "other", "enabled": False}, {"id": "oobe", "enabled": True}]), "clear")

    def test_a_paused_scan_job_reads_paused(self):
        self.assertEqual(self._state([{"id": "oobe", "enabled": False}]), "paused")

    def test_a_missing_scan_job_reads_nojob(self):
        self.assertEqual(self._state([{"id": "other", "enabled": True}]), "nojob")
        (self._home / "cron" / "jobs.json").unlink()
        self.assertEqual(self._state(), "nojob")

    def test_the_markers_are_read_before_the_job(self):
        self.assertEqual(self._state([], markers=[".bootstrap_completed"]), "completed")
        self.assertEqual(self._state(markers=[".user_aligned"]), "aligned")

    def test_a_conversation_marker_reads_as_aligned(self):
        self.assertEqual(self._state(markers=[".user_aligned_conversation"]), "aligned")

    def test_a_conversation_binding_reads_as_aligned(self):
        self.assertEqual(self._state(markers=["onboarding_conversation.json"]), "aligned")


class SweepIdReadTest(unittest.TestCase):
    """Step 3's sed over the marker, run by sh against a data dir."""

    def _sweep_id(self, text):
        with tempfile.TemporaryDirectory() as home:
            (pathlib.Path(home) / ".bootstrap_scan_filed").write_text(text)
            plant = _render_plant(**{"local.home": home})
            command = re.search(r"^  agent sh -c '(sed -n -E [^']*)'", plant, re.M).group(1)
            return subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=True).stdout.strip()

    def test_the_gate_marker_reads_as_its_task_id(self):
        self.assertEqual(self._sweep_id("task_id=t_1\nfiled_at=1\n"), "t_1")

    def test_a_marker_written_by_hand_reads_as_its_task_id(self):
        self.assertEqual(self._sweep_id("filed_at=1\n  task_id = t_2\n"), "t_2")


class CapStateReadTest(unittest.TestCase):
    """Step 2's classification of the cap file and its sentinel, run by sh against a data dir."""

    def _cap_read(self, mark=None, cap=None):
        with tempfile.TemporaryDirectory() as home:
            paths = {"local.cap_mark": f"{home}/.first_run_cluster_cards.bench",
                     "local.cap_file": f"{home}/.first_run_cluster_cards"}
            for key, text in (("local.cap_mark", mark), ("local.cap_file", cap)):
                if text is not None:
                    pathlib.Path(paths[key]).write_text(text)
            plant = _render_plant(**paths)
            command = re.search(r"^cap_read=\"\$\(agent sh -c '(.*?)' \\\n", plant, re.S | re.M).group(1)
            return subprocess.run(
                ["sh", "-c", command, "sh", paths["local.cap_mark"], paths["local.cap_file"]],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

    def test_a_sentinel_holding_the_caps_value_reads_as_the_benchs(self):
        self.assertEqual(self._cap_read(mark="2\n", cap="2\n"), "bench")
        self.assertEqual(self._cap_read(mark="2\n", cap="2"), "bench")

    def test_a_sentinel_without_its_cap_reads_as_an_orphan(self):
        self.assertEqual(self._cap_read(mark="2\n"), "orphan none")

    def test_a_sentinel_beside_a_cap_of_another_value_reads_as_an_orphan_and_an_operators_cap(self):
        self.assertEqual(self._cap_read(mark="2\n", cap="10\n"), "orphan operator")
        # An empty sentinel, as an older build of this stack wrote, proves nothing.
        self.assertEqual(self._cap_read(mark="", cap=""), "orphan operator")

    def test_a_cap_without_a_sentinel_is_an_operators(self):
        self.assertEqual(self._cap_read(cap="4\n"), "operator")

    def test_neither_file_reads_as_none(self):
        self.assertEqual(self._cap_read(), "none")


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

    def test_a_marker_written_by_hand_reads_as_its_task_id(self):
        self.assertEqual(self._old_id({".bootstrap_scan_filed": "  task_id = t_1\n"}), "t_1")

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


class HandoffStateQueryTest(unittest.TestCase):
    """Step 4's hand-off query, run under the test interpreter against a board."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls._home = cls._dir.name
        cls._script = _HANDOFF_STATE_RE.search(_render_plant(**{"local.home": cls._home})).group(1)

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def tearDown(self):
        (pathlib.Path(self._home) / "scripts" / "bootstrap_handoff.py").unlink(missing_ok=True)

    def _hand_off_module(self, body):
        scripts = pathlib.Path(self._home) / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "bootstrap_handoff.py").write_text(f"def _prioritize_body():\n    return {body!r}\n")

    def _query(self, sweep_status, cards=(), runs=1, sweep_at=100):
        board = pathlib.Path(self._home) / "kanban.db"
        board.unlink(missing_ok=True)
        with sqlite3.connect(board) as conn:
            conn.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                "created_at INTEGER NOT NULL, idempotency_key TEXT, body TEXT)"
            )
            conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, 'bootstrap-inventory-scan', NULL)", (_SWEEP, sweep_status, sweep_at)
            )
            conn.executemany(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
                [(f"t_{i}", c[1], c[2], c[0], c[3] if len(c) > 3 else None) for i, c in enumerate(cards)],
            )
            conn.executemany("INSERT INTO task_runs (task_id) VALUES (?)", [(_SWEEP,)] * runs)
        completed = subprocess.run(
            [
                sys.executable,
                "-",
                _SWEEP,
                _INTERPOLATIONS["local.cluster_key_like"],
                _INTERPOLATIONS["local.prioritize_key"],
            ],
            input=self._script,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    def test_a_keyed_ranking_card_after_the_sweep_counts(self):
        cards = [(_INTERPOLATIONS["local.prioritize_key"], "todo", 200)]
        self.assertEqual(self._query("running", cards), "1 1 0")

    def test_an_archived_or_older_ranking_card_does_not_count(self):
        key = _INTERPOLATIONS["local.prioritize_key"]
        self.assertEqual(self._query("running", [(key, "archived", 200)]), "1 0 0")
        self.assertEqual(self._query("running", [(key, "todo", 50)]), "1 0 0")

    def test_only_the_hand_offs_own_ranking_card_counts_where_its_module_is(self):
        key = _INTERPOLATIONS["local.prioritize_key"]
        self._hand_off_module("Rank it.")
        self.assertEqual(self._query("running", [(key, "running", 200, "Rank the inventory now.")]), "1 0 0")
        self.assertEqual(self._query("running", [(key, "todo", 200, "Rank it.")]), "1 1 0")
        self.assertEqual(self._query("running", [(key, "todo", 50)]), "1 0 0")

    def test_settled_needs_the_sweep_and_every_cluster_card_ended(self):
        self.assertEqual(self._query("done", [(_CLUSTER_KEY + "a", "done", 150), (_CLUSTER_KEY + "b", "blocked", 150)]), "1 0 1")
        self.assertEqual(self._query("done", [(_CLUSTER_KEY + "a", "done", 150), (_CLUSTER_KEY + "b", "running", 150)]), "1 0 0")
        self.assertEqual(self._query("running", [(_CLUSTER_KEY + "a", "done", 150)]), "1 0 0")
        self.assertEqual(self._query("blocked", [(_CLUSTER_KEY + "a", "archived", 150)]), "1 0 1")
        self.assertEqual(self._query("done", [(_CLUSTER_KEY + "a", "triage", 150)]), "1 0 1")
        self.assertEqual(self._query("done", [(_CLUSTER_KEY + "a", "cancelled", 150), (_CLUSTER_KEY + "b", "failed", 150)]), "1 0 1")

    def test_a_blocked_sweep_with_no_cluster_card_is_not_settled(self):
        self.assertEqual(self._query("blocked"), "1 0 0")
        self.assertEqual(self._query("done"), "1 0 1")

    def test_a_previous_sweeps_cluster_card_is_ignored(self):
        self.assertEqual(self._query("done", [(_CLUSTER_KEY + "old", "running", 50)]), "1 0 1")

    def test_no_run_reads_as_not_started(self):
        self.assertEqual(self._query("todo", runs=0), "0 0 0")


if __name__ == "__main__":
    unittest.main()
