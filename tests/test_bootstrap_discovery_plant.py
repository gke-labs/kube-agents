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

"""The bootstrap-discovery plant's two waits, run against a stub cluster.

`bench/tf/prebuilt/bootstrap-discovery/main.tf` re-arms the onboarding gate and
waits for the sweep it files. Both behaviours pinned here fail quietly on a
real install:

  1. Step 4 must not hand over on a run that ended before the sweep filed its
     cards -- a rate-limit block, or a run that filed some and was reclaimed.
     Handing over early grades a fan-out that is still being written.
  2. On failure, the exit trap must wait for every gateway pod's gate run to
     exit before it lists the cards to archive. A gate run that read the marker
     as absent files its sweep after the trap puts the marker back, and above
     one replica that run is on the leader, not whichever pod `kubectl exec
     deployment/...` picks.

As in `test_autoops_incident_plant.py`, the provisioner is rendered the way
Terraform renders it and run against a stub `kubectl`/`gcloud`/`sleep`.
"""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODULE = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "bootstrap-discovery" / "main.tf"
_HEREDOC_RE = re.compile(r"command\s*=\s*<<-EOT\n(.*?)\n\s*EOT\n", re.S)
_PLANT_BLOCK = 0

_INTERPOLATIONS = {
    "local.home": "/opt/data",
    "local.hermes": "/opt/hermes/.venv/bin/hermes",
    "local.python": "/opt/hermes/.venv/bin/python3",
    "local.key_like": "bootstrap-inventory-%",
    "local.file_wait": "600",
    "local.run_wait": "900",
    "local.poll": "15",
    "local.inventory": "/opt/data/INVENTORY.raw.md /opt/data/INVENTORY.md",
    "local.gate_script": "bootstrap_scan_gate.py",
    "local.gate_wait": "300",
    "var.project_id": "kube-agents-evals",
    "var.host_cluster_name": "platform-agent-host",
    "var.host_cluster_location": "us-central1",
    "var.agent_namespace": "kubeagents-system",
    "var.agent_deployment": "platform-agent-gateway",
    "var.agent_container": "platform-agent",
    "var.sandbox_selector": "app=platform-agent-shell",
    "var.sandbox_container": "shell",
}

# Records every call to $CALLS, tagging the in-pod Python by what it reads, and
# keeps scenario state as files under $STATE. The leader pod is listed second so
# that a check of the first pod alone reads idle.
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
    elif "app=gw" in argv:
        print("pod/gw-follower\npod/gw-leader")
    elif "app=platform-agent-shell" in argv:
        print("pod/platform-agent-shell-0")
    sys.exit(0)

cmd = argv[argv.index("--") + 1 :]
target = next(a for a in argv if a.startswith(("deployment/", "pod/")))
script = cmd[2] if cmd[:2] == ["sh", "-c"] else ""
stdin = sys.stdin.read() if cmd[1:2] == ["-"] else ""

if ".user_aligned" in script:
    record("state")
    print("clear")
elif "s/^task_id=//p" in script:
    record("sweep_id")
    if not (state / "rearmed").exists():
        print("t_old")
    elif os.environ.get("GATE_FILES") == "1" and bump("sweep_reads") >= 1:
        print("t_new")
elif "task_id=$1" in script:
    record("restore")
elif "rm" in cmd and "/opt/data/.bootstrap_scan_filed" in cmd:
    record("rearm")
    (state / "rearmed").touch()
elif "archive" in cmd:
    record("archive")
elif "/proc" in stdin:
    record("gate " + target)
    busy = int(os.environ.get("GATE_BUSY_CHECKS", "0"))
    if target == "pod/gw-leader" and bump("leader_checks") < busy:
        print("running")
    else:
        if target == "pod/gw-leader":
            (state / "gate_done").touch()
        print("idle")
elif "idempotency_key LIKE" in stdin:
    record("open_cards")
    raced = os.environ.get("RACE") == "1" and (state / "gate_done").exists()
    print("t_raced" if raced else "")
elif "task_runs" in stdin:
    record("run_state")
    states = os.environ.get("RUN_STATES", "1 1 1 1").split(";")
    print(states[min(bump("run_state"), len(states) - 1)])
else:
    record()
'''

_GCLOUD_STUB = '#!/bin/bash\necho "gcloud $*" >> "$CALLS"\n'
_SLEEP_STUB = "#!/bin/bash\nexit 0\n"


def _render_plant() -> str:
    """Render the create-time provisioner as Terraform would: dedent, protect
    `$${` escapes, substitute interpolations, then restore the escapes."""
    body = textwrap.dedent(_HEREDOC_RE.findall(_MODULE.read_text())[_PLANT_BLOCK])
    sentinel = "\x00"
    body = body.replace("$${", sentinel)
    unresolved = []

    def substitute(match: "re.Match[str]") -> str:
        expression = match.group(1).strip()
        if expression not in _INTERPOLATIONS:
            unresolved.append(expression)
            return match.group(0)
        return _INTERPOLATIONS[expression]

    body = re.sub(r"\$\{([^}]*)\}", substitute, body).replace(sentinel, "${")
    if unresolved:
        raise AssertionError(f"add these to _INTERPOLATIONS: {sorted(set(unresolved))}")
    return body


@unittest.skipUnless(shutil.which("bash"), "no bash on PATH")
class BootstrapDiscoveryPlantTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._plant = _render_plant()

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = pathlib.Path(self._dir.name)
        self._script = root / "plant.sh"
        self._script.write_text(self._plant)
        self._stub_dir = root / "bin"
        self._stub_dir.mkdir()
        for name, source in (("kubectl", _KUBECTL_STUB), ("gcloud", _GCLOUD_STUB), ("sleep", _SLEEP_STUB)):
            stub = self._stub_dir / name
            stub.write_text(source)
            stub.chmod(0o755)
        self._state = root / "state"
        self._state.mkdir()
        self._calls = root / "calls"

    def _run(self, **scenario):
        env = dict(os.environ)
        env["PATH"] = f"{self._stub_dir}{os.pathsep}{env['PATH']}"
        env["CALLS"] = str(self._calls)
        env["STATE"] = str(self._state)
        env.update({k: str(v) for k, v in scenario.items()})
        completed = subprocess.run(
            ["bash", str(self._script)], env=env, capture_output=True, text=True, timeout=120
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
        self.assertIn("Sweep card t_new filed 3 card(s)", completed.stdout)
        self.assertEqual(self._indices(calls, "[archive]"), [])

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
        self.assertEqual(len(leader), 3, calls)
        self.assertEqual(len(archive), 1, calls)
        self.assertLess(restore[0], leader[0])
        self.assertLess(leader[-1], archive[0])

    def test_the_trap_waits_out_the_ceiling_when_no_gateway_pod_can_be_read(self):
        # No leader answers idle, so the trap waits the full gate_wait, then
        # still archives what it finds.
        completed, calls = self._run(GATE_FILES=0, GATE_BUSY_CHECKS=1000, RACE=0)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(len(self._indices(calls, "[gate pod/gw-leader]")), 300 // 5 + 1)
        self.assertEqual(len(self._indices(calls, "[open_cards]")), 3)


if __name__ == "__main__":
    unittest.main()
