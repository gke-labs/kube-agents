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

"""The bootstrap-ranking plant, run against a stub cluster.

`bench/tf/prebuilt/bootstrap-ranking/main.tf` plants a raw inventory report on
the shell sandbox, files the prioritization card, waits for its worker, and
arms the delivery job. Pinned here: step 1's refusals and the sandbox-pod
count stopping the apply before anything changes; the order clear, plant,
file, wait, arm; a leftover arm being torn down first; the planted bytes
being the fixture's; the card being filed with the gate's key and assignee;
step 5's hand-over rules; the exit trap archiving, clearing and disarming on
a failure or a TERM or INT after step 2, and finishing when a signal arrives
during that cleanup; and the destroy carrying on past a failed step and
naming it. As in `test_bootstrap_discovery_plant.py`, the provisioners are
rendered the way Terraform renders them and run against a stub
`kubectl`/`gcloud`/`sleep`. The in-pod card filing runs on its own against a
stub `hermes`, step 5's board query against a sqlite board, and the arm and
disarm against a stub `cron.jobs` over a temporary data directory.
"""

import base64
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
import time
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_STACK = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "bootstrap-ranking"
_MODULE = _STACK / "main.tf"
_RAW = _STACK / "inventory-raw.txt"
_HEREDOC_RE = re.compile(r"command\s*=\s*<<-EOT\n(.*?)\n\s*EOT\n", re.S)
_BODY_RE = re.compile(r"card_body_b64 = base64encode\(<<-EOB\n(.*?)\n\s*EOB\n", re.S)
_CREATE_RE = re.compile(r"^card=\"\$\(agent_py [^\n]*<<'PY'\n(.*?)\nPY\n", re.S | re.M)
_RUN_STATE_RE = re.compile(r"^run_state\(\) \{\n  agent_py [^\n]*<<'PY'\n(.*?)\nPY\n", re.S | re.M)
_READ_STATE_RE = re.compile(r"^read_state\(\) \{\n  agent_py [^\n]*<<'PY'[^\n]*\n(.*?)\nPY\n", re.S | re.M)
_EOP_RE = r"{}\s*=\s*<<-EOP\n(.*?)\n\s*EOP\n"
_PLANT_BLOCK = 0
_DESTROY_BLOCK = 1
_CARD = "t_card"
_RATE_LIMIT = "provider rate limit: API retries exhausted"


def _card_body() -> str:
    return textwrap.dedent(_BODY_RE.search(_MODULE.read_text()).group(1)) + "\n"


def _in_pod(name: str, interpolations: dict) -> str:
    """The arm or disarm Python, rendered with `interpolations`."""
    body = textwrap.dedent(re.search(_EOP_RE.format(name), _MODULE.read_text(), re.S).group(1)) + "\n"
    return re.sub(r"\$\{([^}]*)\}", lambda m: interpolations[m.group(1).strip()], body)


_IN_POD = {
    "local.home": "/opt/data",
    "local.scan_job": "bootstrap-inventory-scan",
    "local.delivery_job": "bootstrap-inventory-delivery",
    "local.state_file": "/opt/data/.bench-onboarding-jobs.json",
    "local.settle_wait": "120",
    "local.settle_poll": "2",
}


_INTERPOLATIONS = {
    "local.home": "/opt/data",
    "local.hermes": "/opt/hermes/.venv/bin/hermes",
    "local.python": "/opt/hermes/.venv/bin/python3",
    "local.key_like": "bootstrap-inventory-%",
    "local.raw_file": "/opt/data/INVENTORY.raw.md",
    "local.inventory": " ".join(
        f"/opt/data/{name}"
        for name in (
            "INVENTORY.raw.md",
            "INVENTORY.raw.md.tmp",
            "INVENTORY.md",
            "INVENTORY.md.tmp",
            "INVENTORY.items.json",
            "INVENTORY.scores.json",
            "INVENTORY.delivered.md",
        )
    ),
    "local.raw_b64": base64.b64encode(_RAW.read_bytes()).decode(),
    "local.card_key": "bootstrap-inventory-prioritize",
    "local.card_assignee": "platform",
    "local.card_title": "Prioritize the onboarding inventory report",
    "local.card_body_b64": base64.b64encode(_card_body().encode()).decode(),
    "local.run_wait": "900",
    "local.poll": "15",
    "local.rate_limit_block": _RATE_LIMIT,
    "local.list_tries": "3",
    "local.list_wait": "5",
    "local.pod_wait": "5",
    "local.scan_job": _IN_POD["local.scan_job"],
    "local.delivery_job": _IN_POD["local.delivery_job"],
    "local.state_file": _IN_POD["local.state_file"],
    "local.greeted": "/opt/data/.bootstrap_greeted",
    "base64encode(local.arm_py)": base64.b64encode(_in_pod("arm_py", _IN_POD).encode()).decode(),
    "base64encode(local.disarm_py)": base64.b64encode(_in_pod("disarm_py", _IN_POD).encode()).decode(),
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
    "self.triggers.disarm_b64": _INTERPOLATIONS["base64encode(local.disarm_py)"],
}

# Records every call to $CALLS, tagging the in-pod Python by what it reads, and
# keeps scenario state as files under $STATE.
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


def unreachable():
    sys.stderr.write("error: unable to upgrade connection: container not found\n")
    sys.exit(1)


if argv[:1] == ["get"]:
    record("list sandbox")
    if os.environ.get("SANDBOX_LIST_FAIL") == "1":
        unreachable()
    for n in range(int(os.environ.get("SANDBOX_PODS", "1"))):
        print("pod/platform-agent-shell-%d" % n)
    sys.exit(0)

cmd = argv[argv.index("--") + 1 :]
target = next(a for a in argv if a.startswith(("deployment/", "pod/")))
script = cmd[2] if cmd[:2] == ["sh", "-c"] else ""
stdin = sys.stdin.read() if cmd[1:2] == ["-"] or "base64 -d" in script else ""

if "_jobs_lock" in stdin:
    record("disarm")
    if os.environ.get("DISARM_FAIL") == "1":
        unreachable()
elif "os.replace(tmp, state)" in stdin:
    record("arm")
    if os.environ.get("ARM_FAIL") == "1":
        unreachable()
elif ".user_aligned" in stdin:
    record("state")
    states = os.environ.get("STEP_1_STATE", "clear").split(";")
    print(states[min(bump("state"), len(states) - 1)])
elif '"kanban", "create"' in stdin:
    record("create")
    (state / "create_argv").write_text("\n".join(cmd))
    card = os.environ.get("CARD_ID", "t_card")
    if card:
        with open(state / "created", "a") as fh:
            fh.write(card + "\n")
    # A create whose exec failed after the board filed the card.
    if os.environ.get("CREATE_FAIL") == "1":
        unreachable()
    print(card)
elif "task_runs" in stdin:
    record("run_state")
    states = os.environ.get("RUN_STATES", "1 1 1").split(";")
    answer = states[min(bump("run_state"), len(states) - 1)]
    if answer == "fail":
        unreachable()
    print(answer)
elif "idempotency_key LIKE" in stdin:
    record("open_cards")
    if str(bump("listings") + 1) in os.environ.get("FAILED_LISTINGS", "").split():
        unreachable()
    archived = (state / "archived").read_text().split() if (state / "archived").exists() else []
    created = (state / "created").read_text().split() if (state / "created").exists() else []
    open_cards = [c for c in os.environ.get("OPEN_CARDS", "").split() + created if c not in archived]
    print(" ".join(open_cards + os.environ.get("STUCK_CARDS", "").split()))
elif "base64 -d" in script:
    record("plant " + target)
    (state / "planted").write_text(stdin)
    (state / "plant_args").write_text("\n".join(cmd[3:]))
    if os.environ.get("PLANT_FAIL") == "1":
        unreachable()
elif "rm" in cmd and "/opt/data/INVENTORY.md" in cmd:
    record("clear " + target)
    if os.environ.get("INVENTORY_RM_FAIL") == target and bump("rm_" + target.replace("/", "_")) + 1 >= int(
        os.environ.get("INVENTORY_RM_FAIL_FROM", "1")
    ):
        unreachable()
elif "archive" in cmd:
    record("archive")
    if cmd[-1] in os.environ.get("ARCHIVE_FAIL", "").split():
        unreachable()
    with open(state / "archived", "a") as fh:
        fh.write(cmd[-1] + "\n")
    if cmd[-1] == os.environ.get("SIGNAL_ON_ARCHIVE_OF"):
        os.kill(os.getppid(), getattr(signal, "SIG" + os.environ["SIGNAL"]))
else:
    record()
'''

_GCLOUD_STUB = '#!/bin/bash\necho "gcloud $*" >> "$CALLS"\n'
# SIGNAL_ON_SLEEP signals the plant from its first sleep, step 5's poll on a
# happy-path run.
_SLEEP_STUB = r'''#!/bin/bash
echo "sleep $*" >> "$CALLS"
if [ -n "${SIGNAL:-}" ] && [ -n "${SIGNAL_ON_SLEEP:-}" ] && [ ! -e "$STATE/signalled" ]; then
  touch "$STATE/signalled"
  kill -"$SIGNAL" "$PPID"
fi
'''


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
class BootstrapRankingPlantTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._plant = _render(_PLANT_BLOCK, _INTERPOLATIONS)
        cls._destroy = _render(_DESTROY_BLOCK, _DESTROY_INTERPOLATIONS)

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = pathlib.Path(self._dir.name)
        self._root = root
        self._script = root / "plant.sh"
        self._script.write_text(self._plant)
        self._destroy_script = root / "destroy.sh"
        self._destroy_script.write_text(self._destroy)
        self._stub_dir = root / "bin"
        self._stub_dir.mkdir()
        for name, source in (("kubectl", _KUBECTL_STUB), ("gcloud", _GCLOUD_STUB), ("sleep", _SLEEP_STUB)):
            path = self._stub_dir / name
            path.write_text(source)
            path.chmod(0o755)
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

    def _changes(self, calls):
        return [c for c in calls if re.search(r"\[(archive|clear |plant |create|arm|disarm)", c)]

    def test_bash_syntax_is_valid(self):
        for script in (self._script, self._destroy_script):
            completed = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_the_happy_path_clears_plants_files_and_waits_in_that_order(self):
        completed, calls = self._run(OPEN_CARDS="t_old", RUN_STATES="0 0 0;1 0 0;1 1 1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        archive = self._indices(calls, "archive t_old [archive]")
        clear = self._indices(calls, "[clear pod/platform-agent-shell-0]")
        clear_agent = self._indices(calls, "[clear deployment/platform-agent-gateway]")
        plant = self._indices(calls, "[plant pod/platform-agent-shell-0]")
        create = self._indices(calls, "[create]")
        runs = self._indices(calls, "[run_state]")
        arm = self._indices(calls, "[arm]")
        self.assertEqual(
            [len(archive), len(clear), len(clear_agent), len(plant), len(create), len(arm)], [1, 1, 1, 1, 1, 1], calls
        )
        self.assertEqual(len(runs), 3)
        self.assertLess(archive[0], clear[0])
        self.assertLess(clear[0], plant[0])
        self.assertLess(clear_agent[0], plant[0])
        self.assertLess(plant[0], create[0])
        self.assertLess(create[0], runs[0])
        self.assertLess(runs[-1], arm[0])
        self.assertEqual(self._indices(calls, "[disarm]"), [])
        self.assertIn("Card t_card's worker ended its run after 30s.", completed.stdout)
        self.assertNotIn("Plant failed", completed.stderr)

    def test_the_planted_bytes_are_the_fixture_and_land_at_the_raw_path(self):
        completed, _ = self._run()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(base64.b64decode((self._state / "planted").read_text()), _RAW.read_bytes())
        self.assertEqual((self._state / "plant_args").read_text().split("\n"), ["sh", "/opt/data/INVENTORY.raw.md", "/opt/data"])

    def test_the_in_sandbox_write_decodes_and_runs_under_gnu_stat(self):
        probe = subprocess.run(["stat", "-c", "%u:%g", "."], capture_output=True, text=True)
        if probe.returncode != 0:
            self.skipTest("no GNU stat, which the sandbox image has")
        script = next(line for line in self._plant.splitlines() if "sh -c 'base64 -d" in line)
        in_pod = re.search(r"sh -c '([^']*)'", script).group(1)
        home = self._root / "data"
        home.mkdir()
        raw = home / "INVENTORY.raw.md"
        written = subprocess.run(
            ["sh", "-c", in_pod, "sh", str(raw), str(home)],
            input=_INTERPOLATIONS["local.raw_b64"], capture_output=True, text=True,
        )
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(raw.read_bytes(), _RAW.read_bytes())
        self.assertFalse((home / "INVENTORY.raw.md.tmp").exists())

    def test_step_1_refuses_every_state_but_clear_before_changing_anything(self):
        # "" is a step-1 read that failed: only the catch-all arm stops it.
        refusals = {
            "aligned": ".user_aligned exists",
            "greeted": ".bootstrap_greeted exists",
            "completed": "onboarding already delivered",
            "unfiled": "has not filed its discovery sweep",
            "nojobs": "is not in the cron store",
            "paused": "is paused",
            "bound": "delivers to a chat, not local",
            "": "could not read the onboarding markers",
        }
        for state, message in refusals.items():
            with self.subTest(state=state):
                self._clear()
                completed, calls = self._run(STEP_1_STATE=state)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(message, completed.stderr)
                self.assertEqual(self._changes(calls), [])
                self.assertNotIn("Plant failed", completed.stderr)

    def test_a_leftover_arm_is_torn_down_before_the_checks(self):
        completed, calls = self._run(STEP_1_STATE="armed;clear")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("An earlier run left delivery armed", completed.stderr)
        states = self._indices(calls, "[state]")
        disarm = self._indices(calls, "[disarm]")
        clear = self._indices(calls, "[clear pod/platform-agent-shell-0]")
        self.assertEqual((len(states), len(disarm)), (2, 1))
        self.assertLess(clear[0], disarm[0])
        self.assertLess(disarm[0], states[1])
        self.assertLess(states[1], self._indices(calls, "[plant pod/platform-agent-shell-0]")[0])

    def test_a_leftover_arm_that_will_not_tear_down_stops_the_apply(self):
        completed, calls = self._run(STEP_1_STATE="armed;clear", DISARM_FAIL=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("could not finish the teardown an earlier run left", completed.stderr)
        self.assertEqual(self._indices(calls, "[plant pod/platform-agent-shell-0]"), [])
        self.assertNotIn("Plant failed", completed.stderr)

    def test_anything_but_one_sandbox_pod_stops_the_apply_before_changing_anything(self):
        for pods in (0, 2):
            with self.subTest(pods=pods):
                self._clear()
                completed, calls = self._run(SANDBOX_PODS=pods)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(f"found {pods}", completed.stderr)
                self.assertEqual(self._changes(calls), [])

    def test_a_card_left_open_after_archiving_stops_the_apply_before_the_plant(self):
        completed, calls = self._run(STUCK_CARDS="t_stuck")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("still open after archiving: t_stuck", completed.stderr)
        self.assertEqual(self._indices(calls, "[plant pod/platform-agent-shell-0]"), [])
        self.assertIn("Plant failed", completed.stderr)

    def test_the_card_is_filed_with_the_gates_key_and_assignee(self):
        gate = (_REPO_ROOT / "agents" / "chat" / "scripts" / "bootstrap_scan_gate.py").read_text()
        module = _MODULE.read_text()
        for gate_name, local in (("PRIORITIZE_IDEMPOTENCY_KEY", "card_key"), ("SCAN_ASSIGNEE", "card_assignee")):
            with self.subTest(local=local):
                want = re.search(rf'^{gate_name} = "([^"]+)"', gate, re.M).group(1)
                self.assertEqual(re.search(rf'^\s*{local}\s*=\s*"([^"]+)"', module, re.M).group(1), want)
                self.assertEqual(_INTERPOLATIONS[f"local.{local}"], want)
        completed, _ = self._run()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        argv = (self._state / "create_argv").read_text().split("\n")
        self.assertEqual(argv[:2], ["/opt/hermes/.venv/bin/python3", "-"])
        self.assertEqual(base64.b64decode(argv[2]).decode(), _card_body())

    def test_the_in_pod_create_passes_the_card_and_parses_the_id(self):
        hermes = self._root / "hermes"
        record = self._root / "hermes_argv"
        hermes.write_text(
            "#!/usr/bin/env python3\nimport json, sys\n"
            f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            "print(json.dumps({'id': 't_new', 'status': 'ready', 'meta': {'k': 1}}, indent=2))\n"
        )
        hermes.chmod(0o755)
        rendered = _render(_PLANT_BLOCK, {**_INTERPOLATIONS, "local.hermes": str(hermes)})
        code = _CREATE_RE.search(rendered).group(1)
        out = subprocess.run(
            [sys.executable, "-c", code, _INTERPOLATIONS["local.card_body_b64"]],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(out.stdout.strip(), "t_new")
        self.assertEqual(
            json.loads(record.read_text()),
            ["kanban", "create", "--json", "--assignee", "platform", "--idempotency-key",
             "bootstrap-inventory-prioritize", "--body", _card_body(), "Prioritize the onboarding inventory report"],
        )

    def test_the_card_body_names_both_sop_paths_the_gate_names(self):
        gate = (_REPO_ROOT / "agents" / "chat" / "scripts" / "bootstrap_scan_gate.py").read_text()
        paths = re.search(r"PRIORITIZE_INSTRUCTIONS_PATHS = \((.*?)\)", gate, re.S).group(1)
        for path in re.findall(r'"([^"]+)"', paths):
            self.assertIn(path, _card_body())
        self.assertIn("/opt/data/INVENTORY.raw.md", _card_body())
        self.assertIn("/opt/data/INVENTORY.md.", _card_body())

    def test_a_missing_card_id_fails_and_the_trap_cleans_up(self):
        completed, calls = self._run(CARD_ID="")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("returned no card id", completed.stderr)
        self.assertIn("Plant failed", completed.stderr)
        self.assertEqual(self._indices(calls, "[run_state]"), [])
        self.assertEqual(len(self._indices(calls, "[clear pod/platform-agent-shell-0]")), 2)

    def test_a_failed_create_fails_and_the_trap_archives_what_it_finds(self):
        completed, calls = self._run(CREATE_FAIL=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Plant failed", completed.stderr)
        self.assertEqual(len(self._indices(calls, "[open_cards]")), 3)
        archived = self._indices(calls, "archive t_card [archive]")
        self.assertEqual(len(archived), 1, calls)
        self.assertLess(self._indices(calls, "[create]")[0], archived[0])

    def test_a_failed_plant_fails_and_the_trap_cleans_up(self):
        completed, calls = self._run(PLANT_FAIL=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Plant failed", completed.stderr)
        self.assertEqual(self._indices(calls, "[create]"), [])
        clear_agent = self._indices(calls, "[clear deployment/platform-agent-gateway]")
        self.assertEqual(len(clear_agent), 2)
        self.assertLess(clear_agent[-1], self._indices(calls, "[disarm]")[0])

    def test_a_failed_arm_fails_and_the_trap_disarms(self):
        completed, calls = self._run(ARM_FAIL=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Plant failed", completed.stderr)
        self.assertLess(self._indices(calls, "[arm]")[0], self._indices(calls, "[disarm]")[0])
        archived = self._indices(calls, "archive t_card [archive]")
        self.assertEqual(len(archived), 1, calls)
        self.assertLess(self._indices(calls, "[create]")[0], archived[0])

    def test_step_5_reports_a_card_no_worker_picked_up(self):
        completed, calls = self._run(RUN_STATES="0 0 0")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("no worker picked up card t_card within 900s", completed.stderr)
        self.assertEqual(len(self._indices(calls, "[run_state]")), 900 // 15 + 1)
        self.assertIn("Plant failed", completed.stderr)

    def test_step_5_hands_over_a_card_whose_runs_did_not_end_on_their_own(self):
        # One run the guardrail blocked, then a retry still going at run_wait.
        completed, calls = self._run(RUN_STATES="1 0 0;1 1 0;2 1 0")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Card t_card has 1 ended run(s), none ended by its worker, after 900s", completed.stdout)
        self.assertNotIn("worker ended its run", completed.stdout)
        self.assertEqual(self._indices(calls, "[archive]"), [])
        self.assertEqual(len(self._indices(calls, "[arm]")), 1)

    def test_step_5_hands_over_on_the_last_good_read_when_later_reads_fail(self):
        completed, _ = self._run(RUN_STATES="1 0 0;fail")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Board reads after 0s failed", completed.stderr)

    def test_step_5_reports_a_board_it_never_read_as_unread(self):
        completed, calls = self._run(RUN_STATES="fail")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("no read of card t_card's runs from the board succeeded", completed.stderr)
        self.assertNotIn("no worker picked up", completed.stderr)

    def test_the_trap_retries_a_failed_listing(self):
        # Listings 1 and 2 are step 2's; the trap's first try is 3.
        completed, calls = self._run(RUN_STATES="0 0 0", FAILED_LISTINGS="3 4")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(len(self._indices(calls, "[open_cards]")), 5)
        self.assertNotIn("Cleanup incomplete", completed.stderr)

    def test_the_trap_names_what_it_could_not_clean(self):
        # Step 2's removal succeeds, so the failure is step 5's.
        completed, _ = self._run(
            RUN_STATES="0 0 0",
            FAILED_LISTINGS="3 4 5",
            INVENTORY_RM_FAIL="pod/platform-agent-shell-0",
            INVENTORY_RM_FAIL_FROM=2,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("no worker picked up card t_card", completed.stderr)
        self.assertIn(
            "Cleanup incomplete: could not list the open cards, remove the INVENTORY files.", completed.stderr
        )
        self._clear()
        completed, _ = self._run(RUN_STATES="0 0 0", DISARM_FAIL=1)
        self.assertIn("Cleanup incomplete: could not disarm the delivery job.", completed.stderr)

    def test_a_signal_during_step_5_still_cleans_up(self):
        # A Prow deadline arrives as SIGTERM, and bash skips the EXIT trap
        # when an untrapped signal ends it.
        for name in ("TERM", "INT"):
            with self.subTest(signal=name):
                self._clear()
                completed, calls = self._run(RUN_STATES="0 0 0", SIGNAL=name, SIGNAL_ON_SLEEP=1)
                self.assertEqual(completed.returncode, 143, completed.stderr)
                self.assertIn("Plant failed (exit 143)", completed.stderr)
                self.assertEqual(len(self._indices(calls, "[run_state]")), 1)
                archived = self._indices(calls, "archive t_card [archive]")
                self.assertEqual(len(archived), 1, calls)
                clear = self._indices(calls, "[clear pod/platform-agent-shell-0]")
                self.assertEqual(len(clear), 2)
                disarm = self._indices(calls, "[disarm]")
                self.assertEqual(len(disarm), 1)
                self.assertLess(archived[0], clear[-1])
                self.assertLess(clear[-1], disarm[0])

    def test_a_signal_during_the_trap_does_not_cut_its_cleanup_short(self):
        for name in ("TERM", "INT"):
            with self.subTest(signal=name):
                self._clear()
                completed, calls = self._run(RUN_STATES="0 0 0", SIGNAL=name, SIGNAL_ON_ARCHIVE_OF="t_card")
                self.assertNotEqual(completed.returncode, 0)
                archived = self._indices(calls, "archive t_card [archive]")
                self.assertEqual(len(archived), 1, calls)
                clear = self._indices(calls, "[clear pod/platform-agent-shell-0]")
                disarm = self._indices(calls, "[disarm]")
                self.assertEqual([len(clear), len(disarm)], [2, 1], calls)
                self.assertLess(archived[0], clear[-1])
                self.assertLess(clear[-1], disarm[0])
                self.assertNotIn("Cleanup incomplete", completed.stderr)

    def test_every_exec_into_the_agent_bounds_its_wait_for_a_pod(self):
        completed, calls = self._run(OPEN_CARDS="", RUN_STATES="0 0 0")
        self.assertNotEqual(completed.returncode, 0)
        self._clear()
        destroy, destroy_calls = self._run(self._destroy_script, OPEN_CARDS="t_a")
        self.assertEqual(destroy.returncode, 0, destroy.stderr)
        for call in calls + destroy_calls:
            if "deployment/platform-agent-gateway" in call:
                self.assertIn("--pod-running-timeout=5s", call)

    def test_destroy_archives_and_clears_both_pods(self):
        completed, calls = self._run(self._destroy_script, OPEN_CARDS="t_a t_b")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self._indices(calls, "[archive]")), 2)
        self.assertEqual(len(self._indices(calls, "[clear deployment/platform-agent-gateway]")), 1)
        clear = self._indices(calls, "[clear pod/platform-agent-shell-0]")
        self.assertEqual(len(clear), 1)
        disarm = self._indices(calls, "[disarm]")
        self.assertEqual(len(disarm), 1)
        self.assertLess(clear[0], disarm[0])

    def test_destroy_carries_on_past_a_failed_step_and_names_it(self):
        completed, calls = self._run(
            self._destroy_script,
            OPEN_CARDS="t_a t_b",
            ARCHIVE_FAIL="t_a",
            INVENTORY_RM_FAIL="deployment/platform-agent-gateway",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(len(self._indices(calls, "[archive]")), 2)
        self.assertEqual(len(self._indices(calls, "[clear pod/platform-agent-shell-0]")), 1)
        self.assertIn("could not archive t_a, remove the agent's INVENTORY files.", completed.stderr)
        self._clear()
        completed, calls = self._run(self._destroy_script, DISARM_FAIL=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("could not disarm the delivery job.", completed.stderr)

    def test_destroy_names_a_failed_sandbox_listing(self):
        completed, _ = self._run(self._destroy_script, SANDBOX_LIST_FAIL=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("could not list the sandbox pods.", completed.stderr)


class RunStateQueryTest(unittest.TestCase):
    """Step 5's board query against a sqlite board."""

    def _board(self, runs):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = pathlib.Path(directory.name)
        conn = sqlite3.connect(home / "kanban.db")
        conn.execute(
            "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, ended_at INTEGER, outcome TEXT, summary TEXT)"
        )
        conn.executemany("INSERT INTO task_runs (task_id, ended_at, outcome, summary) VALUES (?, ?, ?, ?)", runs)
        conn.commit()
        conn.close()
        rendered = _render(_PLANT_BLOCK, {**_INTERPOLATIONS, "local.home": str(home)})
        return _RUN_STATE_RE.search(rendered).group(1)

    def _counts(self, runs):
        code = self._board(runs)
        out = subprocess.run(
            [sys.executable, "-c", code, _CARD, _RATE_LIMIT], capture_output=True, text=True, check=True
        )
        return out.stdout.split()

    def test_a_completed_run_and_a_worker_block_count_and_a_rate_limit_block_does_not(self):
        runs = [
            (_CARD, 10, "blocked", _RATE_LIMIT + " after 3 tries"),
            (_CARD, 20, "timed_out", None),
            (_CARD, None, None, None),
            ("t_other", 30, "completed", "done"),
        ]
        self.assertEqual(self._counts(runs), ["3", "2", "0"])
        self.assertEqual(self._counts(runs + [(_CARD, 40, "blocked", "raw report has no findings block")]), ["4", "3", "1"])
        self.assertEqual(self._counts([(_CARD, 50, "completed", None)]), ["1", "1", "1"])


# Only what the arm and disarm call, over a jobs.json in the data directory.
_CRON_JOBS_STUB = """import contextlib, json, os
_FILE = os.path.join(os.environ["DATA"], "cron", "jobs.json")


def load_jobs():
    return json.load(open(_FILE))["jobs"]


def save_jobs(jobs):
    json.dump({"jobs": jobs}, open(_FILE, "w"))


# `is_job_runnable` in the pinned Hermes: `pause_job` sets all three, and a pod
# start re-enables the job but leaves the other two.
def is_job_runnable(job):
    return bool(job.get("enabled", True)) and not (job.get("state") == "paused" or job.get("paused_at"))


def compute_next_run(schedule, last_run_at=None):
    return "next:" + schedule["expr"]


@contextlib.contextmanager
def _jobs_lock():
    yield
"""

_SCAN = {"id": "bootstrap-inventory-scan", "schedule": {"expr": "* * * * *"}, "deliver": "local", "next_run_at": "t0"}
_DELIVERY = {
    "id": "bootstrap-inventory-delivery",
    "schedule": {"expr": "* * * * *"},
    "deliver": "local",
    "next_run_at": "t0",
    "fire_claim": {"by": "gone"},
    "pending_slot": "t0",
}


class ArmDisarmTest(unittest.TestCase):
    """The in-pod arm and disarm against a stub `cron.jobs`."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        self._home = root / "data"
        (self._home / "cron").mkdir(parents=True)
        (root / "cron").mkdir()
        (root / "cron" / "__init__.py").write_text("")
        (root / "cron" / "jobs.py").write_text(_CRON_JOBS_STUB)
        self._env = {**os.environ, "PYTHONPATH": str(root), "DATA": str(self._home)}
        values = {
            **_IN_POD,
            "local.home": str(self._home),
            "local.state_file": str(self._home / ".bench-onboarding-jobs.json"),
            "local.settle_wait": "1",
            "local.settle_poll": "0.1",
        }
        self._arm = _in_pod("arm_py", values)
        self._disarm = _in_pod("disarm_py", values)
        self._greeted = self._home / ".bootstrap_greeted"
        self._jobs([_SCAN, _DELIVERY])

    def _jobs(self, jobs=None):
        path = self._home / "cron" / "jobs.json"
        if jobs is not None:
            path.write_text(json.dumps({"jobs": jobs}))
        return json.loads(path.read_text())["jobs"]

    def _py(self, code):
        return subprocess.run(
            [sys.executable, "-"], input=code, env=self._env, capture_output=True, text=True, timeout=60
        )

    def test_arm_refuses_a_missing_paused_or_bound_job_and_changes_nothing(self):
        for jobs, message in (
            ([_SCAN], "bootstrap-inventory-delivery are not in the cron store"),
            ([_SCAN, {**_DELIVERY, "enabled": False}], "bootstrap-inventory-delivery is paused"),
            ([_SCAN, {**_DELIVERY, "state": "paused"}], "bootstrap-inventory-delivery is paused"),
            ([_SCAN, {**_DELIVERY, "paused_at": "t0"}], "bootstrap-inventory-delivery is paused"),
            ([_SCAN, {**_DELIVERY, "deliver": "origin"}], "delivers to 'origin', not local"),
        ):
            with self.subTest(message=message):
                self._jobs(jobs)
                out = self._py(self._arm)
                self.assertNotEqual(out.returncode, 0)
                self.assertIn(message, out.stderr)
                self.assertEqual(sorted(p.name for p in self._home.iterdir()), ["cron"])

    def _read_state(self):
        rendered = _render(
            _PLANT_BLOCK,
            {
                **_INTERPOLATIONS,
                "local.home": str(self._home),
                "local.state_file": str(self._home / ".bench-onboarding-jobs.json"),
                "local.greeted": str(self._greeted),
            },
        )
        return _READ_STATE_RE.search(rendered).group(1) + "\n"

    def test_step_1_reads_a_paused_delivery_job_as_paused(self):
        # A paused scan job does not matter: with `.bootstrap_scan_filed`
        # present, its every run skips.
        (self._home / ".bootstrap_scan_filed").touch()
        read_state = self._read_state()
        for jobs, answer in (
            ([_SCAN, _DELIVERY], "clear"),
            ([{**_SCAN, "enabled": False}, _DELIVERY], "clear"),
            ([_SCAN, {**_DELIVERY, "enabled": False}], "paused"),
            ([_SCAN, {**_DELIVERY, "state": "paused"}], "paused"),
            ([_SCAN, {**_DELIVERY, "paused_at": "t0"}], "paused"),
        ):
            with self.subTest(answer=answer, jobs=jobs):
                self._jobs(jobs)
                out = self._py(read_state)
                self.assertEqual((out.returncode, out.stdout.strip()), (0, answer), out.stderr)

    def test_step_1_reads_a_greeted_marker_as_greeted(self):
        (self._home / ".bootstrap_scan_filed").touch()
        self._greeted.touch()
        out = self._py(self._read_state())
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "greeted"), out.stderr)

    def test_arm_records_the_jobs_then_touches_the_marker_once(self):
        out = self._py(self._arm)
        self.assertEqual(out.returncode, 0, out.stderr)
        state = self._home / ".bench-onboarding-jobs.json"
        self.assertEqual(json.loads(state.read_text())["jobs"], [_SCAN, _DELIVERY])
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self._home / ".user_aligned").exists())
        self.assertNotEqual(self._py(self._arm).returncode, 0)

    def test_arm_refuses_a_marker_it_did_not_create_and_leaves_its_record_for_the_trap(self):
        # A person's first chat between step 1 and the arm. The state file is
        # already in place, so the exit trap's disarm handles this as an arm;
        # main.tf's header says what that person then gets.
        marker = self._home / ".user_aligned"
        marker.write_text("person")
        out = self._py(self._arm)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("FileExistsError", out.stderr)
        self.assertEqual(marker.read_text(), "person")
        self.assertEqual(
            sorted(p.name for p in self._home.iterdir()), [".bench-onboarding-jobs.json", ".user_aligned", "cron"]
        )

    def test_a_failed_write_leaves_no_state_file_to_wedge_the_next_run(self):
        # Every reader takes the state file as proof of an arm and hands it to
        # the disarm, which cannot parse a half-written one.
        full_disk = (
            "import json\n"
            "def dump(obj, fh):\n"
            "    fh.write('{\"jo')\n"
            "    raise OSError(28, 'No space left on device')\n"
            "json.dump = dump\n"
        )
        out = self._py(full_disk + self._arm)
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(sorted(p.name for p in self._home.iterdir()), ["cron"])
        self.assertIn("was not armed", self._py(self._disarm).stdout)
        self.assertEqual(self._py(self._arm).returncode, 0)

    def test_disarm_puts_back_the_removed_jobs_and_clears_the_markers(self):
        self.assertEqual(self._py(self._arm).returncode, 0)
        self._jobs([_SCAN])
        (self._home / ".bootstrap_completed").write_text("")
        (self._home / ".bootstrap_sandbox_unanswered").write_text("")
        out = self._py(self._disarm)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("put back: bootstrap-inventory-delivery", out.stdout)
        scan, delivery = self._jobs()
        self.assertEqual(scan, _SCAN)
        self.assertEqual(
            delivery,
            {"id": "bootstrap-inventory-delivery", "schedule": {"expr": "* * * * *"}, "deliver": "local",
             "next_run_at": "next:* * * * *"},
        )
        self.assertEqual(sorted(p.name for p in self._home.iterdir()), ["cron"])

    def test_disarm_without_an_arm_leaves_a_real_marker_alone(self):
        (self._home / ".user_aligned").write_text("")
        out = self._py(self._disarm)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("was not armed", out.stdout)
        self.assertTrue((self._home / ".user_aligned").exists())

    def test_disarm_waits_a_bounded_time_for_a_run_under_way(self):
        self.assertEqual(self._py(self._arm).returncode, 0)
        conn = sqlite3.connect(self._home / "cron" / "executions.db")
        conn.execute("CREATE TABLE executions (id INTEGER PRIMARY KEY, job_id TEXT, status TEXT)")
        conn.execute("INSERT INTO executions (job_id, status) VALUES ('bootstrap-inventory-delivery', 'running')")
        conn.commit()
        conn.close()
        started = time.monotonic()
        out = self._py(self._disarm)
        self.assertEqual(out.returncode, 0, out.stderr)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 2.0)
        self.assertLess(elapsed, 10.0)
        self.assertFalse((self._home / ".bench-onboarding-jobs.json").exists())


if __name__ == "__main__":
    unittest.main()
