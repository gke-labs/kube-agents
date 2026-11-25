"""remediation-kcc-stateful-deletion-policy-abandon checks verdict, deletion-policy, and IP."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "remediation-kcc-stateful-deletion-policy-abandon"
    / "task.yaml"
)
CHECK = "verdict-and-kcc-deletion-and-network-safeguards-are-named"
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


def test_accepts_plain_verdict_and_safeguards():
    report = (
        "VERDICT: MISSING-SAFEGUARDS\n\n"
        "Add `cnrm.cloud.google.com/deletion-policy: abandon` and set "
        "`ipv4Enabled: false` with a private IP."
    )
    assert _passes(report)


def test_rejects_wrong_verdict():
    report = (
        "VERDICT: SAFE-TO-MERGE\n\n"
        "cnrm.cloud.google.com/deletion-policy: abandon and ipv4Enabled: false."
    )
    assert not _passes(report)


def test_rejects_missing_abandon_annotation():
    report = (
        "VERDICT: MISSING-SAFEGUARDS\n\n"
        "Set `ipv4Enabled: false` for private networking."
    )
    assert not _passes(report)


def test_accepts_private_network_ref_and_vpc_prose():
    report = (
        "VERDICT: MISSING-SAFEGUARDS\n\n"
        "Add `cnrm.cloud.google.com/deletion-policy: abandon` and configure "
        "`privateNetworkRef` on a private VPC network while disabling public IPv4."
    )
    assert _passes(report)


def test_rejects_dismissed_private_ip():
    report = (
        "VERDICT: MISSING-SAFEGUARDS\n\n"
        "Add `cnrm.cloud.google.com/deletion-policy: abandon`. "
        "A private IP is not needed here; `ipv4Enabled: true` is fine."
    )
    assert not _passes(report)
