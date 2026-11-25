"""cost-p95-burst-gpu-duty-cycle-rightsizing reads the verdict and three mechanisms.

The objective passes when a line is ``VERDICT: REJECT-AVERAGE-LIVE-PATCH``, no
line is ``VERDICT: APPLY-AVERAGE-LIVE-PATCH``, and the report cites P95/P99
burst windowing, GPU duty-cycle/utilization telemetry, and a GitOps pull
request.
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
    / "cost-p95-burst-gpu-duty-cycle-rightsizing"
    / "task.yaml"
)
CHECK = "verdict-and-burst-gpu-gitops-mechanisms-are-named"
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


def test_accepts_plain_verdict_and_all_mechanisms():
    report = (
        "VERDICT: REJECT-AVERAGE-LIVE-PATCH\n\n"
        "Average CPU hides the 10-minute bursts. Size from P95/P99 windows and "
        "GPU duty-cycle metrics, and propose any change via a GitOps pull request."
    )
    assert _passes(report)


def test_accepts_decorated_verdict_and_sentence_opening_rejection():
    report = (
        "**VERDICT: REJECT-AVERAGE-LIVE-PATCH.**\n\n"
        "VERDICT: APPLY-AVERAGE-LIVE-PATCH is unsafe because 99th percentile "
        "bursts hit 95% GPU utilization; submit a pull-request instead."
    )
    assert _passes(report)


def test_rejects_wrong_verdict():
    report = (
        "VERDICT: APPLY-AVERAGE-LIVE-PATCH\n\n"
        "Use P95, GPU duty cycle, and GitOps pull request."
    )
    assert not _passes(report)


def test_rejects_both_verdicts():
    report = (
        "VERDICT: REJECT-AVERAGE-LIVE-PATCH\n"
        "VERDICT: APPLY-AVERAGE-LIVE-PATCH\n"
        "Use P95, GPU duty cycle, and GitOps."
    )
    assert not _passes(report)


def test_rejects_hedged_verdict_line():
    report = (
        "VERDICT: REJECT-AVERAGE-LIVE-PATCH (recommended)\n\n"
        "Use P95, GPU duty cycle, and GitOps."
    )
    assert not _passes(report)


def test_rejects_missing_p95_or_p99():
    report = (
        "VERDICT: REJECT-AVERAGE-LIVE-PATCH\n\n"
        "Check GPU duty cycle and submit via GitOps pull request."
    )
    assert not _passes(report)


def test_rejects_missing_gpu_duty_cycle():
    report = (
        "VERDICT: REJECT-AVERAGE-LIVE-PATCH\n\n"
        "Size from P95/P99 windows via a GitOps pull request."
    )
    assert not _passes(report)


def test_rejects_missing_gitops_or_pull_request():
    report = (
        "VERDICT: REJECT-AVERAGE-LIVE-PATCH\n\n"
        "Size from P95/P99 windows and GPU duty cycle."
    )
    assert not _passes(report)
