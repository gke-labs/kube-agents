"""consistency-kcc-kubelet-cfs-quota-drift checks verdict and cpuCfsQuota risk."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "consistency-kcc-kubelet-cfs-quota-drift"
    / "task.yaml"
)
CHECK = "verdict-and-cfs-quota-enforcement-risk-are-named"
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


def test_accepts_plain_verdict_and_cfs_quota_risk():
    report = (
        "VERDICT: CPU-LIMITS-UNENFORCED\n\n"
        "`cpuCfsQuota: false` disables kernel CFS quota enforcement, so CPU "
        "limits are not enforced and noisy-neighbour starvation can occur."
    )
    assert _passes(report)


def test_rejects_wrong_verdict():
    report = (
        "VERDICT: CPU-LIMITS-ENFORCED\n\n"
        "cpuCfsQuota disables nothing."
    )
    assert not _passes(report)


def test_rejects_missing_cfs_quota_mention():
    report = (
        "VERDICT: CPU-LIMITS-UNENFORCED\n\n"
        "Noisy-neighbour starvation is possible."
    )
    assert not _passes(report)
