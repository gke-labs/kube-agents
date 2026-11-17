"""autoscaler-pending-generated-workloads-probe reads one line of the report.

The objective passes when the "FIX-SETS:" line holds the namespace default
class label key, in either of its two forms. The line binds the label to the
fix: the label named anywhere else in the answer, as a thing to compare or for
another namespace, does not pass. ``report_contains`` lowercases the report,
drops emphasis and backticks, and folds a list marker at the start of a line
before the pattern runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "autoscaler-pending-generated-workloads-probe" / "task.yaml"
CHECK = "the-fix-names-the-namespace-default-class"
TIMEOUT_SEC = 5.0

COMMAND = "NEXT-COMMAND: kubectl get nodes -L cloud.google.com/compute-class"
LABEL = "cloud.google.com/default-compute-class"
BODY = "The pool carries a compute-class label and the pods name no class."


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _passes(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    transcript.set(report, [], final_message=report)
    return ReportContainsVerifier(**check["check"]).verify(TIMEOUT_SEC).success


def test_a_fix_line_with_the_namespace_label_passes():
    for report in (
        f"{COMMAND}\n\n{BODY}\n\nFIX-SETS: {LABEL}",
        f"{COMMAND}\n\n{BODY}\n\n**FIX-SETS:** `{LABEL}`",
        f"{COMMAND}\n\n{BODY}\n\nFIX-SETS: the label {LABEL}-non-daemonset on the Namespace manifest",
        f"{COMMAND}\n\n{BODY}\n\n- FIX-SETS: {LABEL}: general",
        f"{COMMAND}\n\n{BODY}\n\nFIX-SETS: metadata.labels[\"{LABEL}\"] of the Namespace",
    ):
        assert _passes(report), report


def test_the_label_outside_the_fix_line_or_another_fix_fails():
    for report in (
        # The label as a thing to compare, and the fix in the controller's code.
        f"{COMMAND}\n\nI expect no {LABEL} label on the namespace.\n\nFIX-SETS: spec.template.spec.nodeSelector",
        # The label for another namespace, in prose.
        f"{COMMAND}\n\nkube-system can carry {LABEL}-non-daemonset: system-pool.\n\nFIX-SETS: the controller's pod template",
        # A negated mention and a capacity ticket.
        f"{COMMAND}\n\nDo not use {LABEL} here.\n\nFIX-SETS: a capacity ticket",
        # A cluster flag is another fix; the case does not take it.
        f"{COMMAND}\n\n{BODY}\n\nFIX-SETS: --enable-default-compute-class",
        # The right fix in prose and no fix line.
        f"{COMMAND}\n\nLabel the Namespace with {LABEL}: general.",
        f"{COMMAND}\n\n{BODY}\n\nFIX-SETS: a default class for the namespace",
        "",
    ):
        assert not _passes(report), report
