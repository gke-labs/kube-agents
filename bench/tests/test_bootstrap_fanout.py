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

"""The discovery fan-out read and the ``bootstrap_fanout`` verifier.

``fixtures/bootstrap_fanout/boards.json`` holds the board rows two live
discovery sweeps left on one install -- ``main``, with no Cluster Agent card
after it, and ``branch``, with one per profile -- with
the project id replaced and the Cluster Agent cards keyed by profile name, as
the gate keys them. The in-pod script runs here under the test
interpreter against a data root rebuilt from them; each failure case mutates
one of the two.
"""

from __future__ import annotations

import ast
import copy
import json
import re
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from devops_bench.verification.base import VERIFIERS
from devops_bench.verification.spec import parse_node

from kube_agents_bench import discovery, verifiers
from kube_agents_bench.verifiers import BootstrapFanoutVerifier

BOARDS = json.loads(
    (Path(__file__).parent / "fixtures" / "bootstrap_fanout" / "boards.json").read_text()
)
REPO = Path(__file__).resolve().parents[2]

# The board tables as the install's hermes creates them, cut to the columns
# the read touches plus the NOT NULL ones.
_SCHEMA = (
    "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, assignee TEXT, "
    "status TEXT NOT NULL, created_by TEXT, created_at INTEGER NOT NULL, idempotency_key TEXT)",
    "CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL, "
    "PRIMARY KEY (parent_id, child_id))",
    "CREATE TABLE kanban_worker_children (child_id TEXT PRIMARY KEY, "
    "creator_id TEXT NOT NULL, created_at INTEGER NOT NULL)",
)


def _board(label: str) -> dict[str, Any]:
    return copy.deepcopy(BOARDS[label])


def _write_root(
    root: Path,
    board: dict[str, Any] | None,
    roster: dict[str, dict[str, str] | None] | None = None,
    schema: tuple[str, ...] = _SCHEMA,
) -> Path:
    roster = BOARDS["roster"] if roster is None else roster
    for name, identity in {**roster, "platform": None}.items():
        home = root / "profiles" / name
        home.mkdir(parents=True)
        config: dict[str, Any] = {"agent": {"environment_probe": False}}
        if identity is not None:
            config["cluster_identity"] = identity
        (home / "config.yaml").write_text(yaml.safe_dump(config))
        for ready in discovery.READY_FILES:
            (home / ready).write_text("")
    if board is None:
        return root
    (root / discovery.SCAN_MARKER).write_text(f"task_id={board['sweep']}\nfiled_at=1790608075\n")
    with sqlite3.connect(root / "kanban.db") as conn:
        for ddl in schema:
            conn.execute(ddl)
        conn.executemany(
            "INSERT INTO tasks VALUES (:id, :title, :assignee, :status, :created_by, :created_at, :idempotency_key)",
            board["tasks"],
        )
        conn.executemany("INSERT INTO task_links VALUES (?, ?)", board["task_links"])
        if board["kanban_worker_children"]:
            conn.executemany("INSERT INTO kanban_worker_children VALUES (?, ?, ?)", board["kanban_worker_children"])
    return root


def _run_script(root: Path) -> str:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            discovery._IN_POD_SCRIPT,
            str(root),
            "kanban.db",
            discovery.FANOUT_PRESENT,
            discovery.SCAN_MARKER,
            discovery.CLUSTER_KEY_PREFIX,
            ",".join(discovery.READY_FILES),
            *discovery.RESERVED_PROFILES,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def pod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point the verifier's shell at a data root under ``tmp_path``."""

    def build(
        board: dict[str, Any] | None,
        roster: dict[str, dict[str, str] | None] | None = None,
        schema: tuple[str, ...] = _SCHEMA,
    ) -> Path:
        root = _write_root(tmp_path, board, roster, schema)

        def shell(script: str, timeout: float) -> str:
            assert discovery.FANOUT_PRESENT in script
            return _run_script(root)

        monkeypatch.setattr(verifiers, "_agent_shell", shell)
        return root

    return build


def _verify(require: str):
    return BootstrapFanoutVerifier(type="bootstrap_fanout", require=require).verify(0.0)


def _card(board: dict[str, Any], assignee_prefix: str) -> dict[str, Any]:
    return next(t for t in board["tasks"] if str(t["assignee"]).startswith(assignee_prefix))


# --- the read -------------------------------------------------------------


def test_the_read_returns_the_sweep_its_children_and_the_roster(tmp_path: Path) -> None:
    payload, why = discovery.read_fanout(lambda s, t: _run_script(_write_root(tmp_path, _board("branch"))), 5.0)
    assert why == ""
    assert payload["sweep"] == {"id": "t_60319ae1", "status": "archived"}
    assert len(payload["roster"]) == 4
    assert payload["unidentified"] == []
    keys = sorted(c["key"] for c in payload["children"])
    assert keys[0] == "bootstrap-inventory-cluster-cluster-example-project-agent-harness-dev-cluster--1d9f820d"
    # Found by key and time, so a card under any other key is not a cluster card.
    assert all(k.startswith(discovery.CLUSTER_KEY_PREFIX) for k in keys)


def test_the_read_skips_the_reserved_profiles(tmp_path: Path) -> None:
    payload, _ = discovery.read_fanout(lambda s, t: _run_script(_write_root(tmp_path, _board("branch"))), 5.0)
    assert "platform" not in [r["profile"] for r in payload["roster"]]
    assert "platform" not in payload["unidentified"]


@pytest.mark.parametrize(
    "config",
    [yaml.safe_dump({"cluster_identity": "not-a-mapping"}).encode(), b"\xff\xfe not utf-8"],
    ids=["identity-not-a-mapping", "undecodable"],
)
def test_a_malformed_profile_is_unidentified_not_a_failed_read(tmp_path: Path, config: bytes) -> None:
    root = _write_root(tmp_path, _board("branch"))
    name = "cluster-example-project-support-eval-cluster-us-central1-a"
    (root / "profiles" / name / "config.yaml").write_bytes(config)
    payload, why = discovery.read_fanout(lambda s, t: _run_script(root), 5.0)
    assert why == ""
    assert name in payload["unidentified"]
    assert len(payload["roster"]) == 3


@pytest.mark.parametrize("absent", discovery.READY_FILES)
def test_a_profile_whose_scaffold_did_not_finish_is_not_on_the_roster(tmp_path: Path, absent: str) -> None:
    root = _write_root(tmp_path, _board("branch"))
    name = "cluster-example-project-support-eval-cluster-us-central1-a"
    (root / "profiles" / name / absent).unlink()
    payload, why = discovery.read_fanout(lambda s, t: _run_script(root), 5.0)
    assert why == ""
    assert payload["not_ready"] == [name]
    assert name not in [r["profile"] for r in payload["roster"]]
    assert len(payload["roster"]) == 3


def _module_constant(path: Path, name: str) -> Any:
    tree = ast.parse(path.read_text())
    value = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets)
    )
    if isinstance(value, ast.Call) and getattr(value.func, "id", None) == "frozenset":
        value = value.args[0]
    return ast.literal_eval(value)


def test_the_mirrored_names_match_the_agent_scripts() -> None:
    gate = REPO / "agents" / "chat" / "scripts" / "bootstrap_scan_gate.py"
    scripts = REPO / "agents" / "platform" / "scripts"
    assert _module_constant(gate, "SCAN_FILED_MARKER") == discovery.SCAN_MARKER
    assert _module_constant(gate, "CLUSTER_IDEMPOTENCY_KEY_PREFIX") == discovery.CLUSTER_KEY_PREFIX
    assert set(_module_constant(scripts / "cluster_agent_profile.py", "RESERVED_PROFILES")) == set(
        discovery.RESERVED_PROFILES
    )
    assert discovery.READY_FILES == (
        _module_constant(scripts / "profile_scaffold.py", "PROFILE_MARKER"),
        *_module_constant(scripts / "cluster_agent_reconcile.py", "SCAFFOLD_ARTIFACTS"),
    )


def test_the_stack_waits_out_the_longest_gate_run() -> None:
    gate = REPO / "agents" / "chat" / "scripts" / "bootstrap_scan_gate.py"
    stack = (REPO / "bench" / "tf" / "prebuilt" / "bootstrap-discovery" / "main.tf").read_text()
    gate_wait = int(re.search(r"^\s*gate_wait\s*=\s*(\d+)\s*$", stack, re.M).group(1))
    assert gate_wait == _module_constant(gate, "RECONCILE_TIMEOUT_SECONDS") + 60  # one cron tick


def test_the_stack_spells_the_mirrored_names_as_their_sources_do() -> None:
    gate = REPO / "agents" / "chat" / "scripts" / "bootstrap_scan_gate.py"
    delivery = REPO / "agents" / "chat" / "scripts" / "bootstrap_delivery.py"
    jobs = json.loads((REPO / "agents" / "chat" / "defaults" / "cron" / "jobs.json").read_text())["jobs"]
    stack = (REPO / "bench" / "tf" / "prebuilt" / "bootstrap-discovery" / "main.tf").read_text()

    def local(name: str) -> str:
        return re.search(rf'^\s*{name}\s*=\s*"([^"]*)"\s*$', stack, re.M).group(1)

    # The scan is a stage of oobe: the gate runs inside oobe.py, so the stack looks for either script.
    scan_jobs = [job for job in jobs if job["id"] == local("scan_job")]
    assert len(scan_jobs) == 1
    assert local("scan_job") == _module_constant(delivery, "OOBE_JOB_ID")
    assert local("gate_script").split() == [scan_jobs[0]["script"], gate.name]
    for key in ("SCAN_IDEMPOTENCY_KEY", "CLUSTER_IDEMPOTENCY_KEY_PREFIX", "PRIORITIZE_IDEMPOTENCY_KEY"):
        assert _module_constant(gate, key).startswith(local("key_like").removesuffix("%")), key
    assert local("cluster_key_like") == _module_constant(gate, "CLUSTER_IDEMPOTENCY_KEY_PREFIX") + "%"


def test_a_failed_exec_is_a_failed_read() -> None:
    payload, why = discovery.read_fanout(lambda s, t: "", 5.0)
    assert payload is None
    assert "could not be read" in why


def test_the_command_prefers_hermes_interpreter() -> None:
    cmd = discovery.command()
    assert cmd.startswith("PY=/opt/hermes/.venv/bin/python3")
    assert discovery.FANOUT_PRESENT in cmd


# --- the objective --------------------------------------------------------


def test_the_branch_sweep_filed_one_card_per_cluster_agent(pod) -> None:
    pod(_board("branch"))
    result = _verify("one_card_per_cluster_agent")
    assert result.status == "pass", result.reason
    assert "one card for each of 4 Cluster Agent(s)" in result.reason


def test_the_main_sweep_filed_none_and_fails(pod) -> None:
    pod(_board("main"))
    result = _verify("one_card_per_cluster_agent")
    assert result.status == "fail"
    assert "filed 0 cluster card(s) for 4 Cluster Agent(s)" in result.reason


def _without(board: dict, *ids: str) -> dict:
    board["tasks"] = [t for t in board["tasks"] if t["id"] not in ids]
    board["kanban_worker_children"] = [w for w in board["kanban_worker_children"] if w[0] not in ids]
    return board


def test_a_board_with_no_cluster_card_yet_fails_rather_than_errors(pod) -> None:
    board = _board("branch")
    cluster_ids = [t["id"] for t in board["tasks"] if str(t.get("idempotency_key") or "").startswith(discovery.CLUSTER_KEY_PREFIX)]
    pod(_without(board, *cluster_ids))
    result = _verify("one_card_per_cluster_agent")
    assert result.status == "fail", result.reason
    assert "filed 0 cluster card(s) for 4 Cluster Agent(s)" in result.reason


def test_a_missing_card_is_named(pod) -> None:
    board = _board("branch")
    dropped = _card(board, "cluster-example-project-platform-agent-host")
    _without(board, dropped["id"])
    pod(board)
    result = _verify("one_card_per_cluster_agent")
    assert result.status == "fail"
    assert "no card for ['cluster-example-project-platform-agent-host-us-east4']" in result.reason


def test_a_second_card_for_one_cluster_agent_fails(pod) -> None:
    board = _board("branch")
    twin = dict(_card(board, "cluster-example-project-support-eval"), id="t_00000001")
    board["tasks"].append(twin)
    board["kanban_worker_children"].append([twin["id"], board["sweep"], twin["created_at"] + 1])
    pod(board)
    result = _verify("one_card_per_cluster_agent")
    assert result.status == "fail"
    assert "more than one card" in result.reason


def test_a_card_assigned_to_the_wrong_profile_fails(pod) -> None:
    board = _board("branch")
    _card(board, "cluster-example-project-cc-triage")["assignee"] = "platform"
    pod(board)
    result = _verify("one_card_per_cluster_agent")
    assert result.status == "fail"
    assert "matching no Cluster Agent" in result.reason


def test_a_card_keyed_by_the_cluster_identity_fails(pod) -> None:
    # The hyphen-joined identity gives `proj-a`/`b` and `proj`/`a-b` one key.
    board = _board("branch")
    card = _card(board, "cluster-example-project-platform-agent-host")
    card["idempotency_key"] = discovery.CLUSTER_KEY_PREFIX + "example-project-platform-agent-host-us-east4"
    pod(board)
    result = _verify("one_card_per_cluster_agent")
    assert result.status == "fail"
    assert "no card for ['cluster-example-project-platform-agent-host-us-east4']" in result.reason


def test_a_profile_without_an_identity_is_not_on_the_roster(pod) -> None:
    board = _board("branch")
    dropped = _card(board, "cluster-example-project-platform-agent-host")
    _without(board, dropped["id"])
    roster = dict(BOARDS["roster"])
    roster["cluster-example-project-platform-agent-host-us-east4"] = None
    pod(board, roster)
    assert _verify("one_card_per_cluster_agent").status == "pass"


def test_no_cluster_agent_is_an_error(pod) -> None:
    pod(_board("main"), roster={})
    result = _verify("one_card_per_cluster_agent")
    assert result.status == "error"
    assert "no ready Cluster Agent profile" in result.reason


def test_no_sweep_marker_is_an_error(pod) -> None:
    pod(None)
    result = _verify("one_card_per_cluster_agent")
    assert result.status == "error"
    assert "no discovery sweep has been filed" in result.reason


def test_a_marker_naming_an_unknown_card_is_an_error(pod) -> None:
    board = _board("branch")
    board["tasks"] = [t for t in board["tasks"] if t["id"] != board["sweep"]]
    pod(board)
    result = _verify("one_card_per_cluster_agent")
    assert result.status == "error"
    assert "is not on the board" in result.reason


def test_a_fail_outranks_a_final_read_that_errors(pod, monkeypatch: pytest.MonkeyPatch) -> None:
    pod(_board("main"))
    read = verifiers._agent_shell
    reads = {"n": 0}

    def fail_then_unreadable(script: str, timeout: float) -> str:
        reads["n"] += 1
        return read(script, timeout) if reads["n"] == 1 else ""

    monkeypatch.setattr(verifiers, "_agent_shell", fail_then_unreadable)
    result = BootstrapFanoutVerifier(type="bootstrap_fanout", require="one_card_per_cluster_agent").verify(2.0)
    assert reads["n"] > 1
    assert result.status == "fail", result.reason
    assert "filed 0 cluster card(s) for 4 Cluster Agent(s)" in result.reason
    assert "the last read failed" in result.reason


def test_an_unreadable_pod_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verifiers, "_agent_shell", lambda s, t: "")
    assert _verify("one_card_per_cluster_agent").status == "error"


# --- registration ---------------------------------------------------------


def test_the_verifier_is_published_as_an_entry_point() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["bootstrap_fanout"] == "kube_agents_bench.verifiers:BootstrapFanoutVerifier"


def test_parse_node_builds_it_like_a_task_yaml_would() -> None:
    node = parse_node({"type": "bootstrap_fanout", "require": "one_card_per_cluster_agent"})
    assert isinstance(node, BootstrapFanoutVerifier)
    assert VERIFIERS.get("bootstrap_fanout") is BootstrapFanoutVerifier


def test_an_unknown_requirement_is_rejected_at_load() -> None:
    with pytest.raises(Exception):
        parse_node({"type": "bootstrap_fanout", "require": "every_cluster"})
