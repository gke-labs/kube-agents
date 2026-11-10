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

The second half runs the case's first check, the one that reads the finding
for the namespace, the cluster and a negation, with a runner-shaped fleet
record for slot b so ``{cluster:b}`` and ``fixture_roles`` resolve as they
do in a run. It pins the shapes the automated review found could pass it
(a failure to check dressed in the prompt's words, a question back in other
words than "which cluster") against the three green findings and a correct
sentence that uses "which" without asking.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest import mock

import yaml
from conftest import check_passes

from kube_agents_bench import fleet
from kube_agents_bench.verifiers import pattern_surface

CASE = "chat-voice-absent-answer-quotes-no-command"
CHECK = "the-answer-quotes-no-command"
CHECK_FINDING = "the-answer-names-the-missing-namespace-and-the-cluster"
SLOT_ROLE = "version-laggard"
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


# The three green findings on the branch's image (2026-11-10, gkedemos), as
# delivered, and a correct sentence that uses "which" without asking.
GREEN_FINDINGS = (
    "**No namespace `ledger-archive` exists on `seeded-b`.** The cluster has 11 Active namespaces "
    "(`default`, `seeded-upgrade`, and 9 system namespaces), and `ledger-archive` is not among them.",
    "**No namespace `ledger-archive` exists on `seeded-b`.** The cluster has `11` Active namespaces "
    "(`default`, `seeded-upgrade`, and `9` system namespaces), and `ledger-archive` is not among them.",
    "**No namespace `ledger-archive` exists on `seeded-b`.** The cluster has 11 `Active` namespaces "
    "(`default`, `seeded-upgrade`, and 9 system namespaces).",
    "**No namespace `ledger-archive` on seeded-b.** seeded-b has 11 namespaces, none of which exist on the cluster "
    "under that name, and ledger-archive is not among them.",
)
# What the automated review found would pass: a failure to check that repeats
# the prompt's terms, and a question back in other words than "which cluster".
NOT_FINDINGS = (
    "I couldn't check whether the ledger-archive namespace is on seeded-b.",
    "I wasn't able to verify ledger-archive on seeded-b.",
    "Which seeded-b cluster did you mean? ledger-archive is not on any I can see.",
    "Quick question: which GKE cluster did you mean for ledger-archive, there is no seeded-b in this project?",
    "Quick question: there are 41 clusters named seeded-b across your projects - which GCP project's "
    "seeded-b cluster did you mean? ledger-archive is not checked yet.",
    # Round two: ability negations that carry the subject and a "not".
    "I can't tell which seeded-b cluster has ledger-archive without the project ID.",
    "I was not able to verify ledger-archive on seeded-b.",
    "ledger-archive on seeded-b could not be checked.",
    "I couldn't confirm whether ledger-archive exists on seeded-b.",
    "I couldn't query seeded-b for ledger-archive.",
    "I did not manage to read the namespaces on seeded-b, so ledger-archive is not confirmed.",
)


def _fleet_dir(root: Path) -> None:
    """A runner-shaped record with slot b reached, so ``{cluster:b}`` expands
    to seeded-b and the slot role resolves to the slot's credential."""
    (root / "clusters").mkdir(exist_ok=True)
    (root / "clusters" / "b.kubeconfig").write_text("apiVersion: v1\n", encoding="utf-8")
    (root / ".fleet-context").write_text(
        f"project=kube-agents-evals\nslot.{SLOT_ROLE}=b\ncluster.b=seeded-b\nlocation.b=us-central1-a\n",
        encoding="utf-8",
    )


def _finding_check_passes(report: str, tmp_path: Path) -> bool:
    _fleet_dir(tmp_path)
    with mock.patch.dict(os.environ, {fleet.FLEET_KUBECONFIG_DIR_ENV: str(tmp_path)}):
        return check_passes(CASE, CHECK_FINDING, report)


def test_the_green_findings_pass_the_finding_check(tmp_path):
    for finding in GREEN_FINDINGS:
        assert _finding_check_passes(_delivered(_with_evidence(finding)), tmp_path), finding


def test_a_failure_to_check_or_a_question_back_fails_the_finding_check(tmp_path):
    for text in NOT_FINDINGS:
        assert not _finding_check_passes(_delivered(text), tmp_path), text


def test_the_ack_line_is_not_read_as_a_question(tmp_path):
    report = f"checking ledger-archive on seeded-b.\n\nResult of delegated task t_1:\n{GREEN_FINDINGS[0]}"
    assert _finding_check_passes(report, tmp_path)
