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

"""The hand-off reads and the ``bootstrap_handoff`` verifier.

``fixtures/bootstrap_handoff/children-metadata.json`` is three Cluster Agent
cards' completed-run metadata from a live sweep, keyed by card id, trimmed to
one workload each and with the project id replaced; ``raw-without-block.txt``
is the raw report that sweep wrote for those clusters, which has no findings
block. Both in-pod scripts run here under the test interpreter: the board read
against a sqlite board rebuilt from the metadata, and the raw read with the
repository's own ``inventory_findings.py`` as the parser, as the sandbox runs
its staged copy.
"""

from __future__ import annotations

import ast
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

from kube_agents_bench import discovery, onboarding
from kube_agents_bench.verifiers import BootstrapHandoffVerifier

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "bootstrap_handoff"
METADATA: dict[str, dict[str, Any]] = json.loads((FIXTURES / "children-metadata.json").read_text())
RAW_WITHOUT_BLOCK = (FIXTURES / "raw-without-block.txt").read_text()
PARSER_DIR = REPO / "agents" / "platform" / "scripts"
GATE = REPO / "agents" / "chat" / "scripts" / "bootstrap_scan_gate.py"
STACK = REPO / "bench" / "tf" / "prebuilt" / "bootstrap-discovery"
TASK = REPO / "bench" / "tasks" / "bootstrap-discovery-fanout" / "task.yaml"

SWEEP = "t_sweep"
SWEEP_AT = 1000

_SCHEMA = (
    (
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, assignee TEXT, "
        "status TEXT NOT NULL, created_at INTEGER NOT NULL, idempotency_key TEXT, body TEXT)"
    ),
    (
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
        "status TEXT NOT NULL, started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT, "
        "summary TEXT, metadata TEXT)"
    ),
)


def _local_shell(script: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=False).stdout


def _block_line(meta: dict[str, Any], finding: dict[str, Any]) -> str:
    return json.dumps(
        {
            "check": "probes-readiness",
            "project": meta["project"],
            "cluster": meta["cluster"],
            "namespace": finding["namespace"],
            "object": finding.get("workload") or finding["namespace"],
            "title": finding["issue"],
            "severity_hint": finding["severity"],
        }
    )


def _block(metadata: dict[str, dict[str, Any]], skip: tuple[str, ...] = ()) -> str:
    lines = [
        _block_line(meta, f)
        for meta in metadata.values()
        if meta["cluster"] not in skip
        for f in meta["findings"]
    ]
    return "\n```findings\n" + "\n".join(lines) + "\n```\n"


def _cluster_card(tid: str, meta: dict[str, Any], status: str = "done") -> dict[str, Any]:
    profile = f"cluster-{meta['project']}-{meta['cluster']}-{meta['location']}"
    return {
        "id": tid,
        "title": f"Report cluster inventory: `{meta['cluster']}`",
        "assignee": profile,
        "status": status,
        "created_at": SWEEP_AT + 10,
        "idempotency_key": discovery.CLUSTER_KEY_PREFIX + profile,
    }


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An agent data root and a sandbox raw file under ``tmp_path``, read locally."""
    root = tmp_path / "agent"
    root.mkdir()
    raw = tmp_path / "sandbox" / "INVENTORY.raw.md"
    raw.parent.mkdir()
    monkeypatch.setattr(onboarding, "DATA_ROOT", str(root))
    monkeypatch.setattr(onboarding, "HERMES_PYTHON", str(tmp_path / "no-hermes-python"))
    monkeypatch.setattr(onboarding, "FALLBACK_PYTHON", sys.executable)
    monkeypatch.setattr(onboarding, "RAW_FILE", str(raw))
    monkeypatch.setattr(onboarding, "PARSER_DIR", str(PARSER_DIR))
    monkeypatch.setattr(onboarding, "HANDOFF_MODULE_DIR", str(REPO / "agents" / "chat" / "scripts"))
    monkeypatch.setattr(onboarding, "SANDBOX_PYTHON", sys.executable)
    monkeypatch.setattr(onboarding, "agent_shell", _local_shell)
    monkeypatch.setattr(onboarding, "sandbox_shell", _local_shell)

    def build(
        raw_text: str | None,
        cards: list[dict[str, Any]] | None = None,
        runs: list[tuple[str, str, dict[str, Any] | None]] | None = None,
        extra: list[dict[str, Any]] = (),
    ) -> Path:
        if cards is None:
            cards = [_cluster_card(tid, meta) for tid, meta in METADATA.items()]
        if runs is None:
            runs = [(tid, "completed", METADATA[tid]) for tid in METADATA]
        (root / discovery.SCAN_MARKER).write_text(f"task_id={SWEEP}\nfiled_at={SWEEP_AT}\n")
        sweep = {
            "id": SWEEP,
            "title": "Discover the environment",
            "assignee": "platform",
            "status": "done",
            "created_at": SWEEP_AT,
            "idempotency_key": "bootstrap-inventory-scan",
        }
        with sqlite3.connect(root / "kanban.db") as conn:
            for ddl in _SCHEMA:
                conn.execute(ddl)
            conn.executemany(
                "INSERT INTO tasks VALUES (:id, :title, :assignee, :status, :created_at, :idempotency_key, :body)",
                [{"body": None, **card} for card in (sweep, *cards, *extra)],
            )
            conn.executemany(
                "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, metadata) "
                "VALUES (?, 'done', 1, 2, ?, ?)",
                [(tid, outcome, None if meta is None else json.dumps(meta)) for tid, outcome, meta in runs],
            )
        if raw_text is not None:
            raw.write_text(raw_text)
        return raw

    return build


def _verify(require: str, timeout: float = 0.0):
    return BootstrapHandoffVerifier(type="bootstrap_handoff", require=require).verify(timeout)


def _ranking_card(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "t_rank",
        "title": "Prioritize the onboarding inventory report",
        "assignee": "platform",
        "status": "todo",
        "created_at": SWEEP_AT + 600,
        "idempotency_key": onboarding.PRIORITIZE_KEY,
        "body": _handoff_module()._prioritize_body(),
        **overrides,
    }


def _handoff_module():
    sys.path.insert(0, str(GATE.parent))
    try:
        import bootstrap_handoff
    finally:
        sys.path.remove(str(GATE.parent))
    return bootstrap_handoff


# --- raw_report_has_findings_block ----------------------------------------


def test_the_live_raw_file_without_a_block_fails_with_the_parsers_code(install) -> None:
    install(RAW_WITHOUT_BLOCK)
    result = _verify("raw_report_has_findings_block")
    assert result.status == "fail", result.reason
    assert "exit code 10" in result.reason
    assert "no ```findings block in the raw file" in result.reason


def test_a_block_missing_one_cluster_fails_naming_it(install) -> None:
    install(RAW_WITHOUT_BLOCK + _block(METADATA, skip=("seeded-c",)))
    result = _verify("raw_report_has_findings_block")
    assert result.status == "fail", result.reason
    assert "none for cluster(s) ['seeded-c']" in result.reason


def test_a_complete_block_passes(install) -> None:
    install(RAW_WITHOUT_BLOCK + _block(METADATA))
    result = _verify("raw_report_has_findings_block")
    assert result.status == "pass", result.reason
    assert "5 block line(s) covering all 2 cluster(s)" in result.reason


def test_a_cluster_with_no_findings_needs_no_line(install) -> None:
    assert [m["cluster"] for m in METADATA.values() if not m["findings"]] == ["seeded-b"]
    install(RAW_WITHOUT_BLOCK + _block(METADATA, skip=("seeded-b",)))
    assert _verify("raw_report_has_findings_block").status == "pass"


def test_a_card_without_a_project_needs_no_line(install) -> None:
    # The hand-off lists such a card as a gap, not in the block.
    runs = [
        (tid, "completed", {k: v for k, v in meta.items() if not (meta["cluster"] == "seeded-c" and k == "project")})
        for tid, meta in METADATA.items()
    ]
    install(RAW_WITHOUT_BLOCK + _block(METADATA, skip=("seeded-c",)), runs=runs)
    assert _verify("raw_report_has_findings_block").status == "pass"


def test_a_title_carrying_the_sentinel_does_not_break_the_read(install) -> None:
    extra = [{"id": "t_odd", "title": f"Prioritize {onboarding.HANDOFF_READ}", "assignee": "platform",
              "status": "todo", "created_at": SWEEP_AT + 20, "idempotency_key": None}]
    install(RAW_WITHOUT_BLOCK + _block(METADATA), extra=extra)
    assert _verify("ranking_card_filed").status == "fail"


def test_a_malformed_block_line_fails_with_the_parsers_errors(install) -> None:
    install(RAW_WITHOUT_BLOCK + "\n```findings\n{\"check\": \"probes-readiness\"}\nnot json\n```\n")
    result = _verify("raw_report_has_findings_block")
    assert result.status == "fail", result.reason
    assert "exit code 11" in result.reason
    assert "missing project, cluster, object, title" in result.reason
    assert "not valid JSON" in result.reason


def test_no_raw_file_fails(install) -> None:
    install(None)
    result = _verify("raw_report_has_findings_block")
    assert result.status == "fail", result.reason
    assert "there is no" in result.reason


def test_only_done_cards_latest_completed_run_counts(install) -> None:
    tids = list(METADATA)
    seeded_c = next(t for t in tids if METADATA[t]["cluster"] == "seeded-c")
    seeded_a = next(t for t in tids if METADATA[t]["cluster"] == "seeded-a")
    cards = [_cluster_card(t, METADATA[t], "running" if t == seeded_c else "done") for t in tids]
    # seeded-a's latest completed run reports no findings; an earlier one did.
    runs = [(t, "completed", METADATA[t]) for t in tids]
    runs.append((seeded_a, "completed", {**METADATA[seeded_a], "findings": []}))
    runs.append((seeded_a, "crashed", None))
    install(RAW_WITHOUT_BLOCK + "\n```findings\n```\n", cards=cards, runs=runs)
    result = _verify("raw_report_has_findings_block")
    assert result.status == "pass", result.reason
    assert "covering all 0 cluster(s)" in result.reason


def test_a_previous_sweeps_cluster_card_is_not_required(install) -> None:
    tids = list(METADATA)
    cards = [_cluster_card(t, METADATA[t]) for t in tids]
    seeded_c = next(c for c in cards if "seeded-c" in c["assignee"])
    seeded_c["created_at"] = SWEEP_AT - 1
    install(RAW_WITHOUT_BLOCK + _block(METADATA, skip=("seeded-c",)), cards=cards)
    assert _verify("raw_report_has_findings_block").status == "pass"


def test_a_writer_the_agent_pod_cannot_import_is_an_error(install, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(onboarding, "HANDOFF_MODULE_DIR", str(tmp_path / "no-scripts"))
    install(RAW_WITHOUT_BLOCK + _block(METADATA))
    result = _verify("raw_report_has_findings_block")
    assert result.status == "error", result.reason
    assert "bootstrap_handoff from" in result.reason


def test_an_image_without_the_writer_still_grades(install, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # An image from before the hand-off: no module, no raw file, no ranking card.
    monkeypatch.setattr(onboarding, "HANDOFF_MODULE_DIR", str(tmp_path / "no-scripts"))
    install(None)
    raw = _verify("raw_report_has_findings_block")
    assert raw.status == "fail", raw.reason
    assert "there is no" in raw.reason
    ranking = _verify("ranking_card_filed")
    assert ranking.status == "fail", ranking.reason


def test_a_marker_written_by_hand_names_the_sweep(install) -> None:
    install(RAW_WITHOUT_BLOCK, extra=[_ranking_card()])
    (Path(onboarding.DATA_ROOT) / discovery.SCAN_MARKER).write_text(f"  task_id = {SWEEP}\nfiled_at={SWEEP_AT}\n")
    result = _verify("ranking_card_filed")
    assert result.status == "pass", result.reason


def test_a_finding_the_writer_does_not_list_needs_no_line(install) -> None:
    # A finding with no issue or title is a gap line in the raw file, not a block line.
    runs = [
        (tid, "completed", dict(meta, findings=[{"namespace": "ns", "description": "no title"}]) if meta["cluster"] == "seeded-c" else meta)
        for tid, meta in METADATA.items()
    ]
    install(RAW_WITHOUT_BLOCK + _block(METADATA, skip=("seeded-c",)), runs=runs)
    assert _verify("raw_report_has_findings_block").status == "pass"


def test_a_parser_the_sandbox_cannot_import_is_an_error(install, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install(RAW_WITHOUT_BLOCK)
    monkeypatch.setattr(onboarding, "PARSER_DIR", str(tmp_path / "no-scripts"))
    result = _verify("raw_report_has_findings_block")
    assert result.status == "error", result.reason
    assert "cannot import inventory_findings" in result.reason


def test_an_unreadable_sandbox_is_an_error(install, monkeypatch: pytest.MonkeyPatch) -> None:
    install(RAW_WITHOUT_BLOCK + _block(METADATA))
    monkeypatch.setattr(onboarding, "sandbox_shell", lambda s, t: "")
    assert _verify("raw_report_has_findings_block").status == "error"


@pytest.mark.parametrize("require", ["raw_report_has_findings_block", "ranking_card_filed"])
def test_an_unreadable_agent_pod_is_an_error(install, monkeypatch: pytest.MonkeyPatch, require: str) -> None:
    install(RAW_WITHOUT_BLOCK + _block(METADATA), extra=[_ranking_card()])
    monkeypatch.setattr(onboarding, "agent_shell", lambda s, t: "")
    assert _verify(require).status == "error"


def test_no_sweep_marker_is_an_error(install) -> None:
    install(RAW_WITHOUT_BLOCK)
    (Path(onboarding.DATA_ROOT) / discovery.SCAN_MARKER).unlink()
    result = _verify("ranking_card_filed")
    assert result.status == "error"
    assert "no discovery sweep has been filed" in result.reason


def test_a_fail_outranks_a_final_read_that_errors(install, monkeypatch: pytest.MonkeyPatch) -> None:
    install(RAW_WITHOUT_BLOCK)
    reads = {"n": 0}

    def fail_then_unreadable(script: str, timeout: float) -> str:
        reads["n"] += 1
        return _local_shell(script, timeout) if reads["n"] == 1 else ""

    monkeypatch.setattr(onboarding, "sandbox_shell", fail_then_unreadable)
    result = _verify("raw_report_has_findings_block", timeout=2.0)
    assert reads["n"] > 1
    assert result.status == "fail", result.reason
    assert "the last read failed" in result.reason


# --- ranking_card_filed ---------------------------------------------------


def test_a_keyed_ranking_card_passes(install) -> None:
    install(RAW_WITHOUT_BLOCK, extra=[_ranking_card()])
    result = _verify("ranking_card_filed")
    assert result.status == "pass", result.reason
    assert "t_rank (todo) is keyed bootstrap-inventory-prioritize" in result.reason


def test_a_ranking_card_without_the_key_fails_and_says_so(install) -> None:
    install(RAW_WITHOUT_BLOCK, extra=[_ranking_card(idempotency_key=None)])
    result = _verify("ranking_card_filed")
    assert result.status == "fail", result.reason
    assert "no unarchived card keyed bootstrap-inventory-prioritize" in result.reason
    assert "a ranking card was filed without the key" in result.reason
    assert "t_rank" in result.reason


def test_no_ranking_card_fails(install) -> None:
    install(RAW_WITHOUT_BLOCK)
    result = _verify("ranking_card_filed")
    assert result.status == "fail", result.reason
    assert "without the key" not in result.reason


@pytest.mark.parametrize(
    "overrides", [{"status": "archived"}, {"created_at": SWEEP_AT - 1}], ids=["archived", "before-the-sweep"]
)
def test_an_archived_or_earlier_keyed_card_fails(install, overrides: dict[str, Any]) -> None:
    install(RAW_WITHOUT_BLOCK, extra=[_ranking_card(**overrides)])
    assert _verify("ranking_card_filed").status == "fail"


@pytest.mark.parametrize("status", onboarding.RANKING_WONT_RUN)
def test_a_ranking_card_that_will_not_run_fails(install, status: str) -> None:
    # The hand-off's own card, keyed and after the sweep, that ranks nothing in this status.
    install(RAW_WITHOUT_BLOCK, extra=[_ranking_card(status=status)])
    result = _verify("ranking_card_filed")
    assert result.status == "fail", result.reason
    assert f"is {status}, so it will not rank the report" in result.reason


def test_a_ranking_card_the_hand_off_did_not_file_fails(install) -> None:
    # A sweep worker's own card: keyed, after the sweep, still running, and no raw file behind it.
    install(RAW_WITHOUT_BLOCK, extra=[_ranking_card(status="running", body="Rank the inventory now.")])
    result = _verify("ranking_card_filed")
    assert result.status == "fail", result.reason
    assert "was not filed by the hand-off" in result.reason


def test_without_the_hand_off_module_the_ranking_card_is_graded_on_status(
    install, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(onboarding, "HANDOFF_MODULE_DIR", str(tmp_path / "no-scripts"))
    install(None, extra=[_ranking_card(body="Rank the inventory now.")])
    assert _verify("ranking_card_filed").status == "pass"


def test_the_wont_run_statuses_match_the_hand_off() -> None:
    bootstrap_handoff = _handoff_module()
    assert set(onboarding.RANKING_WONT_RUN) == set(bootstrap_handoff.SETTLED) - {bootstrap_handoff.DONE}


# --- the mirrored names and the case --------------------------------------


def _gate_constant(name: str, path: Path = GATE) -> str:
    """A module-level string constant, following a ``module.NAME`` alias to its sibling module."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            value = node.value
            if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                return _gate_constant(value.attr, path.parent / f"{value.value.id}.py")
            return ast.literal_eval(value)
    raise AssertionError(f"{name} is not assigned in {path}")


def test_the_reader_spells_the_names_as_their_sources_do() -> None:
    parser = (PARSER_DIR / "inventory_findings.py").read_text()
    assert f'DEFAULT_RAW_PATH = "{onboarding.RAW_FILE}"' in parser
    assert (PARSER_DIR / f"{onboarding.PARSER_MODULE}.py").is_file()
    assert onboarding.PRIORITIZE_KEY == _gate_constant("PRIORITIZE_IDEMPOTENCY_KEY")
    assert onboarding.PRIORITIZE_ASSIGNEE == _gate_constant("SCAN_ASSIGNEE")
    # The ranking card's title is the sweep SOP's, or the hand-off script's once
    # the hand-off moves out of the model's hands.
    title = f"{onboarding.PRIORITIZE_TITLE_WORD} the onboarding inventory report"
    assert title in (REPO / "agents" / "chat" / "scripts" / "bootstrap_handoff.py").read_text()


def test_the_stack_waits_for_the_key_the_reader_checks() -> None:
    main_tf = (STACK / "main.tf").read_text()
    assert re.search(rf'^\s*prioritize_key\s*=\s*"{onboarding.PRIORITIZE_KEY}"\s*$', main_tf, re.MULTILINE)


def test_the_case_runs_the_hand_off_stack() -> None:
    case = yaml.safe_load(TASK.read_text().split("\n---\n", 1)[1])
    infra = case["infrastructure"]
    assert infra["stack"] == "prebuilt/bootstrap-discovery"
    assert "variables" not in infra
    checks = {e["name"]: e["check"] for e in case["verification_spec"]}
    assert checks["raw-report-has-findings-block"] == {"type": "bootstrap_handoff", "require": "raw_report_has_findings_block"}
    assert checks["ranking-card-keyed"] == {"type": "bootstrap_handoff", "require": "ranking_card_filed"}


def test_the_verifier_is_published_as_an_entry_point() -> None:
    with (REPO / "bench" / "pyproject.toml").open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["bootstrap_handoff"] == "kube_agents_bench.verifiers:BootstrapHandoffVerifier"


def test_parse_node_builds_it_like_a_task_yaml_would() -> None:
    node = parse_node({"type": "bootstrap_handoff", "require": "ranking_card_filed"})
    assert isinstance(node, BootstrapHandoffVerifier)
    assert VERIFIERS.get("bootstrap_handoff") is BootstrapHandoffVerifier


def test_an_unknown_requirement_is_rejected_at_load() -> None:
    with pytest.raises(ValueError):
        parse_node({"type": "bootstrap_handoff", "require": "everything"})
