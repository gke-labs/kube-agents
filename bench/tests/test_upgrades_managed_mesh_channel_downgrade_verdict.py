"""upgrades-managed-mesh-channel-downgrade-guard checks verdict and downgrade/skew risks."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "upgrades-managed-mesh-channel-downgrade-guard"
    / "task.yaml"
)
CHECK = "verdict-and-mesh-downgrade-risks-are-named"
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


def test_accepts_plain_verdict_and_risks():
    report = (
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\n\n"
        "Switching from rapid (1.24) to stable (1.22) risks a control-plane "
        "downgrade, sidecar skew, and Envoy xDS NACK loops."
    )
    assert _passes(report)


def test_rejects_wrong_verdict():
    report = (
        "VERDICT: SAFE-TO-SWITCH\n\n"
        "No downgrade or sidecar NACK issues."
    )
    assert not _passes(report)


def test_rejects_verdict_slug_echo_without_risks():
    report = (
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\n\n"
        "Choosing BLOCK-CHANNEL-DOWNGRADE because it is risky."
    )
    assert not _passes(report)
