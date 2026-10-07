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

"""The sandbox read and the ``bootstrap_findings`` verifier.

The items file under test is the one ``inventory_findings.py extract`` writes
from the raw report the bootstrap-ranking stack plants, so the case's expected
pairs are checked against what the script produces rather than against a
hand-written copy of its output. The read command runs under ``sh`` here with
the items path pointed into a temporary directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from devops_bench.verification.base import VERIFIERS
from devops_bench.verification.spec import parse_node

from kube_agents_bench import onboarding
from kube_agents_bench.verifiers import BootstrapFindingsVerifier

REPO = Path(__file__).resolve().parents[2]
RAW = REPO / "bench" / "tf" / "prebuilt" / "bootstrap-ranking" / "inventory-raw.txt"
EXTRACT = REPO / "agents" / "platform" / "scripts" / "inventory_findings.py"
TASK = REPO / "bench" / "tasks" / "bootstrap-inventory-ranking-delivery" / "task.yaml"


def _objective() -> dict[str, Any]:
    spec = yaml.safe_load(TASK.read_text().split("\n---\n", 1)[1])["verification_spec"]
    return next(e for e in spec if e["name"] == "findings-extracted")["check"]


def _expected() -> list[dict[str, str]]:
    return _objective()["expected_findings"]


def _extract(out: Path) -> dict[str, Any]:
    subprocess.run(
        [sys.executable, str(EXTRACT), "extract", "--raw", str(RAW), "--out", str(out)],
        check=True,
        capture_output=True,
    )
    return json.loads(out.read_text())


def _local_shell(script: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=True).stdout


@pytest.fixture
def items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The items path the read command looks at, with the command run locally."""
    path = tmp_path / "INVENTORY.items.json"
    monkeypatch.setattr(onboarding, "ITEMS_FILE", str(path))
    monkeypatch.setattr(onboarding, "sandbox_shell", _local_shell)
    return path


def _verify(expected: list[dict[str, str]] | None = None, timeout: float = 0.0):
    return BootstrapFindingsVerifier(
        type="bootstrap_findings", expected_findings=_expected() if expected is None else expected
    ).verify(timeout)


# --- the case and its fixture ---------------------------------------------


def test_the_expected_pairs_are_what_extract_writes_from_the_planted_report(tmp_path: Path) -> None:
    written = _extract(tmp_path / "items.json")
    assert sorted((i["check"], i["object"]) for i in written["items"]) == sorted(
        (e["check"], e["object"]) for e in _expected()
    )


def test_the_stack_plants_the_report_the_tests_read() -> None:
    main_tf = (RAW.parent / "main.tf").read_text()
    assert 'file("${path.module}/inventory-raw.txt")' in main_tf


def test_the_stack_and_the_reader_spell_the_items_file_as_the_script_does() -> None:
    script = EXTRACT.read_text()
    assert f'DEFAULT_ITEMS_PATH = "{onboarding.ITEMS_FILE}"' in script
    assert "INVENTORY.items.json" in (RAW.parent / "main.tf").read_text()


def test_the_stack_files_the_card_the_gate_and_the_hand_off_name() -> None:
    main_tf = (RAW.parent / "main.tf").read_text()
    gate = (REPO / "agents" / "chat" / "scripts" / "bootstrap_scan_gate.py").read_text()
    handoff = (REPO / "agents" / "chat" / "scripts" / "bootstrap_handoff.py").read_text()
    assert 'PRIORITIZE_IDEMPOTENCY_KEY = "bootstrap-inventory-prioritize"' in gate
    assert 'SCAN_ASSIGNEE = "platform"' in gate
    assert 'card_key      = "bootstrap-inventory-prioritize"' in main_tf
    assert 'card_assignee = "platform"' in main_tf
    assert 'PRIORITIZE_TITLE = "Prioritize the onboarding inventory report"' in handoff
    assert 'card_title    = "Prioritize the onboarding inventory report"' in main_tf


# --- the read -------------------------------------------------------------


def test_the_read_returns_the_file_the_extract_wrote(items: Path) -> None:
    written = _extract(items)
    state, text, why = onboarding.read_items(onboarding.sandbox_shell, 5.0)
    assert (state, why) == ("present", "")
    assert json.loads(text) == written


def test_no_file_is_absent_not_a_failed_read(items: Path) -> None:
    state, text, why = onboarding.read_items(onboarding.sandbox_shell, 5.0)
    assert state == "absent"
    assert str(items) in why


def test_a_failed_exec_is_a_failed_read() -> None:
    state, _, why = onboarding.read_items(lambda s, t: "", 5.0)
    assert state == "error"
    assert "could not be read" in why


def test_sandbox_shell_execs_into_the_sandbox_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="out", stderr="")

    monkeypatch.setattr(onboarding.subprocess, "run", run)
    monkeypatch.setenv("AGENT_SERVICE_NAME", "agent-x")
    monkeypatch.setenv("AGENT_NAMESPACE", "ns-x")
    monkeypatch.setenv("AGENT_CLUSTER_CONTEXT", "ctx-x")
    monkeypatch.delenv("EVAL_SANDBOX_POD", raising=False)
    assert onboarding.sandbox_shell("echo hi", 5.0) == "out"
    assert seen[-1] == [
        "kubectl", "exec", "pod/agent-x-shell-0", "-n", "ns-x", "--context", "ctx-x",
        "-c", "shell", "--", "sh", "-c", "echo hi",
    ]
    monkeypatch.setenv("EVAL_SANDBOX_POD", "other-pod")
    monkeypatch.delenv("AGENT_CLUSTER_CONTEXT")
    onboarding.sandbox_shell("echo hi", 5.0)
    assert seen[-1][2:5] == ["pod/other-pod", "-n", "ns-x"]
    assert "--context" not in seen[-1]


def test_sandbox_shell_returns_nothing_on_a_failed_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        onboarding.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="partial", stderr="x")
    )
    assert onboarding.sandbox_shell("true", 5.0) == ""


# --- the objective --------------------------------------------------------


def test_the_extracted_findings_pass(items: Path) -> None:
    _extract(items)
    result = _verify()
    assert result.status == "pass", result.reason
    assert "holds the 6 expected finding(s)" in result.reason


def test_no_items_file_fails(items: Path) -> None:
    result = _verify()
    assert result.status == "fail"
    assert "extract did not run where the card's worker has its terminal" in result.reason


def test_a_missing_finding_is_named(items: Path) -> None:
    written = _extract(items)
    written["items"] = [i for i in written["items"] if i["object"] != "cart"]
    items.write_text(json.dumps(written))
    result = _verify()
    assert result.status == "fail"
    assert "missing [('probes-readiness', 'cart')]" in result.reason


def test_a_finding_the_report_does_not_carry_is_named(items: Path) -> None:
    written = _extract(items)
    written["items"].append(dict(written["items"][0], check="invented", object="nothing"))
    items.write_text(json.dumps(written))
    result = _verify()
    assert result.status == "fail"
    assert "('invented', 'nothing')" in result.reason


def test_a_duplicated_finding_fails(items: Path) -> None:
    written = _extract(items)
    written["items"].append(written["items"][0])
    items.write_text(json.dumps(written))
    result = _verify()
    assert result.status == "fail"
    assert "('sa-key-in-secret', 'checkout')" in result.reason


def test_a_file_that_is_not_json_fails(items: Path) -> None:
    items.write_text("extracted 6 findings\n")
    result = _verify()
    assert result.status == "fail"
    assert "is not JSON" in result.reason


def test_json_without_an_items_list_fails(items: Path) -> None:
    items.write_text(json.dumps([{"check": "sa-key-in-secret", "object": "checkout"}]))
    result = _verify()
    assert result.status == "fail"
    assert "no list of items" in result.reason


def test_a_file_past_the_cap_fails(items: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _extract(items)
    monkeypatch.setattr(onboarding, "MAX_ITEMS_BYTES", 64)
    result = _verify()
    assert result.status == "fail"
    assert "larger than 64 bytes" in result.reason


def test_an_unreadable_sandbox_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "sandbox_shell", lambda s, t: "")
    assert _verify().status == "error"


def test_a_fail_outranks_a_final_read_that_errors(items: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reads = {"n": 0}

    def absent_then_unreadable(script: str, timeout: float) -> str:
        reads["n"] += 1
        return _local_shell(script, timeout) if reads["n"] == 1 else ""

    monkeypatch.setattr(onboarding, "sandbox_shell", absent_then_unreadable)
    result = _verify(timeout=2.0)
    assert reads["n"] > 1
    assert result.status == "fail", result.reason
    assert "the last read failed" in result.reason


# --- registration ---------------------------------------------------------


def test_the_verifier_is_published_as_an_entry_point() -> None:
    with (REPO / "bench" / "pyproject.toml").open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["bootstrap_findings"] == "kube_agents_bench.verifiers:BootstrapFindingsVerifier"


def test_parse_node_builds_the_case_objective() -> None:
    node = parse_node(_objective())
    assert isinstance(node, BootstrapFindingsVerifier)
    assert VERIFIERS.get("bootstrap_findings") is BootstrapFindingsVerifier


@pytest.mark.parametrize(
    "expected",
    [[], [{"check": "", "object": "x"}], [{"check": "c", "object": "x", "namespace": "n"}]],
)
def test_an_empty_or_malformed_expectation_is_rejected_at_load(expected: list[dict[str, str]]) -> None:
    with pytest.raises(Exception):
        parse_node({"type": "bootstrap_findings", "expected_findings": expected})
