"""capacity-multipool-ca-scan-delay-capacitybuffer reads the verdict and both mechanisms.

The objective passes when a line is ``VERDICT: USE-NAMESPACE-DEFAULT-AND-CAPACITYBUFFER``,
no line is ``VERDICT: USE-PER-DEPLOYMENT-PR``, and the report names both
``cloud.google.com/default-compute-class`` and ``CapacityBuffer`` (or
``capacity buffer``). ``report_contains`` lowercases the report, drops emphasis
and backticks, and folds leading list or quote decoration and trailing stops
before the patterns run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "capacity-multipool-ca-scan-delay-capacitybuffer"
    / "task.yaml"
)
CHECK = "verdict-and-both-targeting-headroom-mechanisms-are-named"
TIMEOUT_SEC = 5.0


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


def test_accepts_plain_verdict_and_both_mechanisms():
    report = (
        "VERDICT: USE-NAMESPACE-DEFAULT-AND-CAPACITYBUFFER\n\n"
        "Untargeted pods force Cluster Autoscaler to scan all 28 pools. "
        "Set the namespace label `cloud.google.com/default-compute-class` on "
        "`imaging-jobs` and configure a `CapacityBuffer` for warm headroom."
    )
    assert _passes(report)


def test_accepts_decorated_verdict_and_prose_rejection_of_wrong_verdict():
    report = (
        "**VERDICT: USE-NAMESPACE-DEFAULT-AND-CAPACITYBUFFER.**\n\n"
        "VERDICT: USE-PER-DEPLOYMENT-PR does not apply because UUID names churn.\n"
        "- Apply `cloud.google.com/default-compute-class` to the namespace.\n"
        "- Add a capacity-buffer (`capacitybuffers.autoscaling.x-k8s.io/v1beta1`).\n"
    )
    assert _passes(report)


def test_rejects_wrong_compute_class_label_suffix():
    report = (
        "VERDICT: USE-NAMESPACE-DEFAULT-AND-CAPACITYBUFFER\n\n"
        "Use `cloud.google.com/default-compute-class-non-daemonset` and `CapacityBuffer`."
    )
    assert not _passes(report)


def test_rejects_wrong_verdict():
    report = (
        "VERDICT: USE-PER-DEPLOYMENT-PR\n\n"
        "Even with `cloud.google.com/default-compute-class` and `CapacityBuffer`, "
        "open per-Deployment PRs."
    )
    assert not _passes(report)


def test_rejects_both_verdicts():
    report = (
        "VERDICT: USE-NAMESPACE-DEFAULT-AND-CAPACITYBUFFER\n"
        "VERDICT: USE-PER-DEPLOYMENT-PR\n"
        "Use `cloud.google.com/default-compute-class` and `CapacityBuffer`."
    )
    assert not _passes(report)


def test_rejects_hedged_verdict_line():
    report = (
        "VERDICT: USE-NAMESPACE-DEFAULT-AND-CAPACITYBUFFER (probably)\n\n"
        "Use `cloud.google.com/default-compute-class` and `CapacityBuffer`."
    )
    assert not _passes(report)


def test_rejects_missing_default_compute_class_label():
    report = (
        "VERDICT: USE-NAMESPACE-DEFAULT-AND-CAPACITYBUFFER\n\n"
        "Provision a `CapacityBuffer` for warm headroom."
    )
    assert not _passes(report)


def test_rejects_missing_capacity_buffer():
    report = (
        "VERDICT: USE-NAMESPACE-DEFAULT-AND-CAPACITYBUFFER\n\n"
        "Set `cloud.google.com/default-compute-class` on the namespace."
    )
    assert not _passes(report)
