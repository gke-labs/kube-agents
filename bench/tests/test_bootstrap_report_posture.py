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

"""The ``bootstrap_report_posture`` verifier and the case that grades with it.

The report is read with the same sandbox read ``bootstrap_report_criticals``
uses, run under ``sh`` here with its paths pointed into a temporary directory.
The expected posture line is held to the one ``inventory_findings.py select``
prints, so the case and the script cannot drift apart.
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
from kube_agents_bench.verifiers import BootstrapReportPostureVerifier

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "agents" / "platform" / "scripts"
STACK = REPO / "bench" / "tf" / "prebuilt" / "bootstrap-ranking"
TASK = REPO / "bench" / "tasks" / "bootstrap-inventory-posture-scope" / "task.yaml"

# What the 10-20 install's report opened with (#3090).
REPORTED = (
    "Scanned 5 GKE clusters (`platform-agent-host`, `seeded-a`, `seeded-b`, `seeded-c`, `seeded-d`) across 221 "
    "workloads. No critical vulnerabilities or active outages were detected; the fleet exhibits a healthy baseline "
    "with standard hygiene opportunities across startup probe configurations, container filesystem hardening, and "
    "namespace resource boundaries."
)
ROLLUP = "Also found: 4 more items, none of them critical, tracked in the findings queue."
CLOSING = "The full inventory is available on request."


def _task() -> dict[str, Any]:
    return yaml.safe_load(TASK.read_text().split("\n---\n", 1)[1])


def _objective() -> dict[str, Any]:
    return next(e for e in _task()["verification_spec"] if e["name"] == "report-posture-scoped")["check"]


def _scope_line() -> str:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import inventory_findings
    finally:
        sys.path.remove(str(SCRIPTS))
    return inventory_findings.SCAN_SCOPE_LINE


def _local_shell(script: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=True).stdout


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The sandbox's data volume, read locally; the tests write the report into it."""
    for name, attr in (
        ("INVENTORY.items.json", "ITEMS_FILE"),
        ("INVENTORY.scores.json", "SCORES_FILE"),
        ("INVENTORY.md", "REPORT_FILE"),
        ("INVENTORY.delivered.md", "DELIVERED_FILE"),
    ):
        monkeypatch.setattr(onboarding, attr, str(tmp_path / name))
    monkeypatch.setattr(onboarding, "PARSER_DIR", str(SCRIPTS))
    monkeypatch.setattr(onboarding, "SANDBOX_PYTHON", sys.executable)
    monkeypatch.setattr(onboarding, "sandbox_shell", _local_shell)
    return tmp_path


def _report(*posture: str, middle: str = "") -> str:
    """A report as SOP Step 6 lays it out with no list: heading, posture, roll-up, closing."""
    return "\n\n".join(["# GKE Environment Scan", *posture, *([middle] if middle else []), ROLLUP, CLOSING]) + "\n"


def _verify(timeout: float = 0.0, **overrides: Any):
    return BootstrapReportPostureVerifier(**{**_objective(), **overrides}).verify(timeout)


# --- the case and its fixture ---------------------------------------------


def test_the_case_expects_the_line_select_prints() -> None:
    assert " ".join(_objective()["posture"][1].split()) == _scope_line()


def test_the_case_grades_the_opening_only() -> None:
    # A word list over prose cannot tell a count ("no critical findings") from a verdict.
    assert not _objective().get("forbidden_patterns")


def test_the_case_expects_the_planted_reports_counts() -> None:
    raw = STACK / _task()["infrastructure"]["variables"]["raw_report"]
    state = json.loads(raw.with_suffix(".cards.json").read_text())
    clusters = [c["metadata"] for c in state["clusters"]]
    workloads = sum(len(m["workloads"]) for m in clusters)
    assert len(clusters) == 1
    assert _objective()["posture"][0] == f"Scanned 1 cluster and {workloads} workloads."


def test_the_planted_report_lists_nothing_critical_by_its_hints() -> None:
    raw = STACK / _task()["infrastructure"]["variables"]["raw_report"]
    sys.path.insert(0, str(SCRIPTS))
    try:
        import inventory_findings
    finally:
        sys.path.remove(str(SCRIPTS))
    hints = {item.get("severity_hint") for item in inventory_findings.parse_block(raw.read_text())}
    assert hints == {"low"}


# --- the verdict ----------------------------------------------------------

#: The verifier's optional word list, exercised here; the case itself grades the opening only.
VERDICTS = [r"\bhealthy\b", r"\bclean\b", r"\ball clear\b", r"\bgood shape\b", r"\b(?:zero|no) (?:active )?outages?\b",
            r"\bno critical vulnerabilit"]


def test_a_report_that_opens_with_the_fixed_posture_passes(sandbox: Path) -> None:
    (sandbox / "INVENTORY.delivered.md").write_text(_report(*_objective()["posture"]))
    result = _verify()
    assert result.status == "pass", result.reason


def test_the_posture_in_one_paragraph_or_wrapped_still_passes(sandbox: Path) -> None:
    counts, line = _objective()["posture"]
    wrapped = line.replace("; ", ";\n")
    assert wrapped != line
    for posture in (f"{counts} {line}", f"{counts}\n\n{wrapped}"):
        (sandbox / "INVENTORY.md").write_text(_report(posture))
        result = _verify()
        assert result.status == "pass", result.reason


def test_the_reported_posture_fails(sandbox: Path) -> None:
    (sandbox / "INVENTORY.delivered.md").write_text(_report(REPORTED))
    result = _verify()
    assert result.status == "fail"
    assert "does not open with the posture the SOP fixes" in result.reason


def test_a_sentence_between_the_counts_and_the_line_fails(sandbox: Path) -> None:
    counts, line = _objective()["posture"]
    (sandbox / "INVENTORY.delivered.md").write_text(_report(counts, "Nothing critical turned up.", line))
    assert _verify().status == "fail"


@pytest.mark.parametrize(
    "verdict",
    [
        "Overall the fleet looks healthy.",
        "No critical vulnerabilities were detected.",
        "There are no active outages.",
        "All clear across the fleet.",
        "Your clusters are in good shape.",
        "The scan came back clean.",
    ],
)
def test_a_verdict_after_the_posture_fails(sandbox: Path, verdict: str) -> None:
    (sandbox / "INVENTORY.delivered.md").write_text(_report(*_objective()["posture"], middle=verdict))
    result = _verify(forbidden_patterns=VERDICTS)
    assert result.status == "fail"
    assert "judges the fleet" in result.reason


def test_a_verdict_in_a_heading_fails(sandbox: Path) -> None:
    # Headings are skipped for the opening, not for the verdict scan: Google Chat posts them as written.
    report = _report(*_objective()["posture"]).replace(
        "# GKE Environment Scan", "# GKE Environment Scan\n\n## Fleet status: healthy"
    )
    (sandbox / "INVENTORY.delivered.md").write_text(report)
    result = _verify(forbidden_patterns=VERDICTS)
    assert result.status == "fail"
    # The second heading is report text, so the opening check fails first.
    assert "does not open with the posture the SOP fixes" in result.reason


def test_a_bold_title_counts_as_the_heading(sandbox: Path) -> None:
    report = _report(*_objective()["posture"]).replace("# GKE Environment Scan", "**GKE Environment Scan**")
    (sandbox / "INVENTORY.delivered.md").write_text(report)
    result = _verify()
    assert result.status == "pass", result.reason


def test_a_roll_up_that_counts_no_critical_findings_judges_nothing(sandbox: Path) -> None:
    report = _report(*_objective()["posture"]).replace(
        ROLLUP, "Also found: 4 more items, no critical findings among them, tracked in the findings queue."
    )
    (sandbox / "INVENTORY.delivered.md").write_text(report)
    result = _verify()
    assert result.status == "pass", result.reason


@pytest.mark.parametrize(
    "line",
    ["## Fleet status: healthy, no active outages", "**No critical vulnerabilities or active outages were detected.**"],
)
def test_a_verdict_heading_or_bold_sentence_before_the_counts_fails_the_opening(sandbox: Path, line: str) -> None:
    # Only the first line can be the title; a second heading or a bold sentence is report text.
    report = _report(*_objective()["posture"]).replace("# GKE Environment Scan", f"# GKE Environment Scan\n\n{line}")
    (sandbox / "INVENTORY.delivered.md").write_text(report)
    result = _verify()
    assert result.status == "fail"
    assert "does not open with the posture the SOP fixes" in result.reason


def test_a_word_that_contains_a_verdict_is_not_one(sandbox: Path) -> None:
    middle = "1. **Critical: `log-shipper` is unhealthy on every node**\n   No logs ship; fix the image tag."
    (sandbox / "INVENTORY.delivered.md").write_text(_report(*_objective()["posture"], middle=middle))
    result = _verify(forbidden_patterns=VERDICTS)
    assert result.status == "pass", result.reason


def test_no_report_fails(sandbox: Path) -> None:
    result = _verify()
    assert result.status == "fail"
    assert "the worker wrote no report" in result.reason


def test_an_unreadable_sandbox_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "sandbox_shell", lambda script, timeout: "")
    assert _verify().status == "error"


# --- registration ---------------------------------------------------------


def test_the_verifier_is_published_as_an_entry_point() -> None:
    with (REPO / "bench" / "pyproject.toml").open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["bootstrap_report_posture"] == "kube_agents_bench.verifiers:BootstrapReportPostureVerifier"


def test_parse_node_builds_the_case_objective() -> None:
    node = parse_node(_objective())
    assert isinstance(node, BootstrapReportPostureVerifier)
    assert VERIFIERS.get("bootstrap_report_posture") is BootstrapReportPostureVerifier


@pytest.mark.parametrize(
    "spec",
    [{}, {"posture": []}, {"posture": ["x"], "forbidden_patterns": ["("]}, {"posture": ["x"], "extra": 1}],
)
def test_a_missing_or_malformed_spec_is_rejected_at_load(spec: dict[str, Any]) -> None:
    with pytest.raises(Exception):
        parse_node({"type": "bootstrap_report_posture", **spec})
