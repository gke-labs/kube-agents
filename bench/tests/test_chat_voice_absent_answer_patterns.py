"""chat-voice-absent-answer-quotes-no-command grades the worker's finding, not the gate's evidence.

The case's no-command check forbids a ``kubectl``, ``gcloud`` or ``$ `` line
in the text before the first "Evidence for verified claims" heading, the
section ``kanban_complete`` renders from ``verified_claims`` below the
worker's text (``deploy/docker/patches/kanban_verified_claims.py``). These
fixtures run the whole ``report_contains`` check as the verifier grades it
(``conftest.check_passes``), on a finding followed by the section the gate
really renders, so a change to the gate's heading or layout reds this test
rather than the nightly. The last test pins the heading literal the
lookaheads stop at: ``pattern_surface`` lowercases the report and drops the
``**``, and the patterns spell the result.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml
from conftest import check_passes

from kube_agents_bench.verifiers import pattern_surface

CASE = "chat-voice-absent-answer-quotes-no-command"
CHECK = "the-answer-quotes-no-command"
HEADING_SURFACE = "evidence for verified claims"
PATCH = Path(__file__).resolve().parents[2] / "deploy" / "docker" / "patches" / "kanban_verified_claims.py"

FINDING = (
    "**No namespace `ledger-archive` exists on `seeded-b`.** The cluster has 11 Active namespaces "
    "(`default`, `seeded-upgrade`, and 9 system namespaces), and `ledger-archive` is not among them."
)
QUOTING_FINDING = (
    "**The `ledger-archive` namespace is not on seeded-b.** `kubectl get namespaces` returns `11` active "
    "namespaces, and `ledger-archive` is not among them."
)
CLAIM = {
    "claim": "Namespace ledger-archive does not exist on seeded-b, which has 11 Active namespaces.",
    "command": "kubectl get namespaces",
    "excerpt": "NAME STATUS AGE\ndefault Active 80d\nkube-system Active 80d\nseeded-upgrade Active 35d",
    "exit_code": 0,
    "ran_at": "2026-11-10T16:08:24Z",
}


def _gate():
    spec = importlib.util.spec_from_file_location("kanban_verified_claims", PATCH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("kanban_verified_claims", module)
    spec.loader.exec_module(module)
    return module


def _with_evidence(finding: str) -> str:
    gate = _gate()
    return gate.append_section(finding, gate.render_evidence([CLAIM]))


def _delivered(result: str) -> str:
    return f"checking seeded-b.\n\nResult of delegated task t_1:\n{result}"


def test_a_clean_finding_passes_with_the_gates_section_below_it():
    report = _delivered(_with_evidence(FINDING))
    assert "$ kubectl get namespaces" in report
    assert check_passes(CASE, CHECK, report)


def test_a_clean_finding_passes_without_a_section():
    assert check_passes(CASE, CHECK, _delivered(FINDING))


def test_a_command_quoted_inside_the_finding_fails():
    assert not check_passes(CASE, CHECK, _delivered(_with_evidence(QUOTING_FINDING)))
    assert not check_passes(CASE, CHECK, _delivered(QUOTING_FINDING))


def test_a_command_fence_above_the_heading_fails():
    fenced = f"{FINDING}\n```\n$ kubectl get ns ledger-archive\n```"
    assert not check_passes(CASE, CHECK, _delivered(_with_evidence(fenced)))
    gcloud = f"{FINDING}\nChecked with gcloud container clusters describe seeded-b."
    assert not check_passes(CASE, CHECK, _delivered(_with_evidence(gcloud)))


def test_the_heading_literal_is_the_one_the_lookaheads_stop_at():
    assert pattern_surface(_gate().EVIDENCE_HEADING) == HEADING_SURFACE
    task = Path(__file__).resolve().parents[1] / "tasks" / CASE / "task.yaml"
    (check,) = [c for c in yaml.safe_load(task.read_text())["verification_spec"] if c["name"] == CHECK]
    for pattern in check["check"]["forbidden_patterns"]:
        assert HEADING_SURFACE in pattern, pattern
