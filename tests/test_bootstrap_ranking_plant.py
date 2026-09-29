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
the shell sandbox, files the prioritization card, and waits for its worker.
Pinned here: step 1's refusals and the sandbox-pod count stopping the apply
before anything changes; the order clear, plant, file, wait; the planted bytes
being the fixture's; the card being filed with the gate's key and assignee;
step 5's hand-over rules; the exit trap archiving and clearing on a failure
after step 2; and the destroy carrying on past a failed step and naming it.
As in `test_bootstrap_discovery_plant.py`, the provisioners are rendered the
way Terraform renders them and run against a stub `kubectl`/`gcloud`/`sleep`.
The in-pod card filing runs on its own against a stub `hermes`, and step 5's
board query against a sqlite board.
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
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_STACK = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "bootstrap-ranking"
_MODULE = _STACK / "main.tf"
_RAW = _STACK / "inventory-raw.txt"
_HEREDOC_RE = re.compile(r"command\s*=\s*<<-EOT\n(.*?)\n\s*EOT\n", re.S)
_BODY_RE = re.compile(r"card_body_b64 = base64encode\(<<-EOB\n(.*?)\n\s*EOB\n", re.S)
_CREATE_RE = re.compile(r"^card=\"\$\(agent_py [^\n]*<<'PY'\n(.*?)\nPY\n", re.S | re.M)
_RUN_STATE_RE = re.compile(r"^run_state\(\) \{\n  agent_py [^\n]*<<'PY'\n(.*?)\nPY\n", re.S | re.M)
_PLANT_BLOCK = 0
_DESTROY_BLOCK = 1
_CARD = "t_card"
_RATE_LIMIT = "provider rate limit: API retries exhausted"


def _card_body() -> str:
    return textwrap.dedent(_BODY_RE.search(_MODULE.read_text()).group(1)) + "\n"


_INTERPOLATIONS = {
    "local.home": "/opt/data",
    "local.hermes": "/opt/hermes/.venv/bin/hermes",
    "local.python": "/opt/hermes/.venv/bin/python3",
    "local.key_like": "bootstrap-inventory-%",
    "local.raw_file": "/opt/data/INVENTORY.raw.md",
    "local.inventory": "/opt/data/INVENTORY.raw.md /opt/data/INVENTORY.md /opt/data/INVENTORY.items.json",
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
}

# Records every call to $CALLS, tagging the in-pod Python by what it reads, and
# keeps scenario state as files under $STATE.
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

if ".user_aligned" in stdin:
    record("state")
    print(os.environ.get("STEP_1_STATE", "clear"))
elif '"kanban", "create"' in stdin:
    record("create")
    (state / "create_argv").write_text("\n".join(cmd))
    if os.environ.get("CREATE_FAIL") == "1":
        unreachable()
    print(os.environ.get("CARD_ID", "t_card"))
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
    open_cards = [c for c in os.environ.get("OPEN_CARDS", "").split() if c not in archived]
    print(" ".join(open_cards + os.environ.get("STUCK_CARDS", "").split()))
elif "base64 -d" in script:
    record("plant " + target)
    (state / "planted").write_text(stdin)
    (state / "plant_args").write_text("\n".join(cmd[3:]))
    if os.environ.get("PLANT_FAIL") == "1":
        unreachable()
elif "rm" in cmd and "/opt/data/INVENTORY.md" in cmd:
    record("clear " + target)
    if os.environ.get("INVENTORY_RM_FAIL") == target:
        unreachable()
elif "archive" in cmd:
    record("archive")
    if cmd[-1] in os.environ.get("ARCHIVE_FAIL", "").split():
        unreachable()
    with open(state / "archived", "a") as fh:
        fh.write(cmd[-1] + "\n")
else:
    record()
'''

_GCLOUD_STUB = '#!/bin/bash\necho "gcloud $*" >> "$CALLS"\n'
_SLEEP_STUB = '#!/bin/bash\necho "sleep $*" >> "$CALLS"\n'


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
        return [c for c in calls if re.search(r"\[(archive|clear |plant |create)", c)]

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
        self.assertEqual([len(archive), len(clear), len(clear_agent), len(plant), len(create)], [1, 1, 1, 1, 1], calls)
        self.assertEqual(len(runs), 3)
        self.assertLess(archive[0], clear[0])
        self.assertLess(clear[0], plant[0])
        self.assertLess(clear_agent[0], plant[0])
        self.assertLess(plant[0], create[0])
        self.assertLess(create[0], runs[0])
        self.assertIn("Card t_card's worker ended its run after 30s.", completed.stdout)
        self.assertNotIn("Plant failed", completed.stderr)

    def test_the_planted_bytes_are_the_fixture_and_land_at_the_raw_path(self):
        completed, _ = self._run()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(base64.b64decode((self._state / "planted").read_text()), _RAW.read_bytes())
        self.assertEqual((self._state / "plant_args").read_text().split("\n"), ["sh", "/opt/data/INVENTORY.raw.md", "/opt/data"])

    def test_the_in_sandbox_write_decodes_and_takes_the_volume_owner(self):
        probe = subprocess.run(["stat", "-c", "%u:%g", "."], capture_output=True, text=True)
        if probe.returncode != 0:
            self.skipTest("no GNU stat, which the sandbox image has")
        completed, _ = self._run()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        script = next(line for line in self._plant.splitlines() if "base64 -d" in line)
        in_pod = re.search(r"sh -c '([^']*)'", script).group(1)
        home = self._root / "data"
        home.mkdir()
        raw = home / "INVENTORY.raw.md"
        written = subprocess.run(
            ["sh", "-c", in_pod, "sh", str(raw), str(home)],
            input=(self._state / "planted").read_text(), capture_output=True, text=True,
        )
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(raw.read_bytes(), _RAW.read_bytes())
        self.assertFalse((home / "INVENTORY.raw.md.tmp").exists())

    def test_step_1_refuses_every_state_but_clear_before_changing_anything(self):
        # "" is a step-1 read that failed: only the catch-all arm stops it.
        refusals = {
            "aligned": ".user_aligned exists",
            "completed": "onboarding already delivered",
            "unfiled": "has not filed its discovery sweep",
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

    def test_a_failed_plant_fails_and_the_trap_cleans_up(self):
        completed, calls = self._run(PLANT_FAIL=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Plant failed", completed.stderr)
        self.assertEqual(self._indices(calls, "[create]"), [])
        self.assertEqual(len(self._indices(calls, "[clear deployment/platform-agent-gateway]")), 2)

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
        self.assertEqual(self._indices(calls, "[archive]"), [])

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
        completed, _ = self._run(
            RUN_STATES="0 0 0", FAILED_LISTINGS="3 4 5", INVENTORY_RM_FAIL="pod/platform-agent-shell-0"
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "Cleanup incomplete: could not list the open cards, remove the INVENTORY files.", completed.stderr
        )

    def test_every_exec_into_the_agent_bounds_its_wait_for_a_pod(self):
        completed, calls = self._run(OPEN_CARDS="", RUN_STATES="0 0 0")
        self.assertNotEqual(completed.returncode, 0)
        destroy, destroy_calls = self._run(self._destroy_script, OPEN_CARDS="t_a")
        for call in calls + destroy_calls:
            if "deployment/platform-agent-gateway" in call:
                self.assertIn("--pod-running-timeout=5s", call)

    def test_destroy_archives_and_clears_both_pods(self):
        completed, calls = self._run(self._destroy_script, OPEN_CARDS="t_a t_b")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self._indices(calls, "[archive]")), 2)
        self.assertEqual(len(self._indices(calls, "[clear deployment/platform-agent-gateway]")), 1)
        self.assertEqual(len(self._indices(calls, "[clear pod/platform-agent-shell-0]")), 1)

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


if __name__ == "__main__":
    unittest.main()
