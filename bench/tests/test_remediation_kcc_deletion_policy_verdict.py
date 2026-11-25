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


def test_rejects_dismissed_deletion_policy_and_optional_network():
    for dismissed in [
        "VERDICT: MISSING-SAFEGUARDS\nSet `ipv4Enabled: false`.\n`cnrm.cloud.google.com/deletion-policy: abandon` is not needed here.",
        "VERDICT: MISSING-SAFEGUARDS\nAdd `cnrm.cloud.google.com/deletion-policy: abandon`.\n`ipv4Enabled: false` is not needed.",
        "VERDICT: MISSING-SAFEGUARDS\nAdd `cnrm.cloud.google.com/deletion-policy: abandon`.\n`privateNetworkRef` is not required.",
        "VERDICT: MISSING-SAFEGUARDS\nAdd `cnrm.cloud.google.com/deletion-policy: abandon`.\nA private IP is optional.",
    ]:
        assert not _passes(dismissed), dismissed


def test_accepts_disable_public_ip_and_private_cloud_sql():
    report = (
        "VERDICT: MISSING-SAFEGUARDS\n\n"
        "Add `cnrm.cloud.google.com/deletion-policy: abandon` and disable public IP "
        "to make it a private Cloud SQL instance."
    )
    assert _passes(report)


def test_accepts_bracket_annotation_and_without_warning():
    report = (
        "VERDICT: MISSING-SAFEGUARDS\n\n"
        'Merging without `metadata.annotations["cnrm.cloud.google.com/deletion-policy"]: "abandon"` '
        "destroys the database on KCC delete; also set `ipv4Enabled: false` on a private VPC network."
    )
    assert _passes(report)


def test_rejects_inverted_policy_quoted_dismissals_and_prefix_negations():
    for bad in [
        "VERDICT: MISSING-SAFEGUARDS\nSet cnrm.cloud.google.com/deletion-policy: none so KCC does not abandon the instance.\nSet ipv4Enabled: false.",
        "VERDICT: MISSING-SAFEGUARDS\nSet cnrm.cloud.google.com/deletion-policy: abandon.\nA private VPC network is not needed.",
        "VERDICT: MISSING-SAFEGUARDS\nSet cnrm.cloud.google.com/deletion-policy: abandon.\nA private Cloud SQL instance is not required.",
        "VERDICT: MISSING-SAFEGUARDS\nSet cnrm.cloud.google.com/deletion-policy: abandon.\nPrivate IPv4 is optional.",
        "VERDICT: MISSING-SAFEGUARDS\nSet cnrm.cloud.google.com/deletion-policy: abandon.\nDisabling public IPv4 is not needed.",
        "VERDICT: MISSING-SAFEGUARDS\nNote: 'cnrm.cloud.google.com/deletion-policy: abandon' is not needed here.\nSet ipv4Enabled: false.",
        'VERDICT: MISSING-SAFEGUARDS\nAdd cnrm.cloud.google.com/deletion-policy: abandon.\nNote that "privateNetworkRef" is not required.',
        "VERDICT: MISSING-SAFEGUARDS\nDo not set cnrm.cloud.google.com/deletion-policy: abandon.\nSet ipv4Enabled: false.",
        "VERDICT: MISSING-SAFEGUARDS\nAdd cnrm.cloud.google.com/deletion-policy: abandon.\nDo not set ipv4Enabled: false.",
        "VERDICT: MISSING-SAFEGUARDS\nAdd cnrm.cloud.google.com/deletion-policy: abandon.\nNo need for privateNetworkRef.",
    ]:
        assert not _passes(bad), bad
