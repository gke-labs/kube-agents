"""capacity-gpu-zonal-skew-fallback-chain checks verdict, ComputeClass, and zonal spread."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "capacity-gpu-zonal-skew-fallback-chain"
    / "task.yaml"
)
CHECK = "verdict-and-computeclass-zonal-mechanisms-are-named"
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


def test_accepts_plain_verdict_and_mechanisms():
    report = (
        "VERDICT: USE-COMPUTECLASS-FALLBACK\n\n"
        "Use a declarative `ComputeClass` priority fallback chain with "
        "`topologySpreadConstraints` to address zonal capacity skew."
    )
    assert _passes(report)


def test_rejects_wrong_verdict():
    report = (
        "VERDICT: BUILD-POLLING-SCRIPT\n\n"
        "ComputeClass and topology spread are optional."
    )
    assert not _passes(report)


def test_rejects_verdict_slug_echo_without_computeclass():
    report = (
        "VERDICT: USE-COMPUTECLASS-FALLBACK\n\n"
        "Choosing USE-COMPUTECLASS-FALLBACK fixes zonal skew."
    )
    assert not _passes(report)


def test_accepts_compute_class_label_and_k8s_zone_spellings():
    report = (
        "VERDICT: USE-COMPUTECLASS-FALLBACK\n\n"
        "Use `cloud.google.com/compute-class` with `topology.kubernetes.io/zone` "
        "and `podTopologySpread` to resolve zonal GPU skew."
    )
    assert _passes(report)
