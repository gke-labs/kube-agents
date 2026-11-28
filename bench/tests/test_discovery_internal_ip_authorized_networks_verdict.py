"""discovery-internal-ip-authorized-networks reads standalone verdict lines, not prose."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "discovery-internal-ip-authorized-networks" / "task.yaml"
CHECK = "authorized-networks-timeout-and-internal-ip-fix-stated"
TIMEOUT_SEC = 5.0


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _passes(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    assert check["check"]["type"] == "all"
    transcript.set(report, [], final_message=report)
    return all(
        ReportContainsVerifier(**part).verify(TIMEOUT_SEC).success
        for part in check["check"]["checks"]
    )


def test_structured_verdict_lines_pass():
    for report in (
        "CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS\nREMEDIATION: USE-INTERNAL-IP-ENDPOINT\n\nShort explanation follows.",
        "**CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS**\n**REMEDIATION: USE-INTERNAL-IP-ENDPOINT**\n",
        "`CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS`\n`REMEDIATION: USE-INTERNAL-IP-ENDPOINT`\n",
        "1. CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS\n2. REMEDIATION: USE-INTERNAL-IP-ENDPOINT\n",
        "> CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS\n> REMEDIATION: USE-INTERNAL-IP-ENDPOINT\n",
        "CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS.\nREMEDIATION: USE-INTERNAL-IP-ENDPOINT.\n",
        "CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS\nREMEDIATION: USE-INTERNAL-IP-ENDPOINT\n\nCAUSE: CLUSTER-CONTROL-PLANE-DOWN is ruled out by the cluster evidence.\n",
    ):
        assert _passes(report), report


def test_wrong_missing_hedged_or_prose_only_fails():
    for report in (
        "CAUSE: CLUSTER-CONTROL-PLANE-DOWN\nREMEDIATION: USE-INTERNAL-IP-ENDPOINT\n",
        "CAUSE: RBAC-FORBIDDEN\nREMEDIATION: USE-INTERNAL-IP-ENDPOINT\n",
        "CAUSE: EXPIRED-CLIENT-CERTIFICATE\nREMEDIATION: USE-INTERNAL-IP-ENDPOINT\n",
        "CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS\nREMEDIATION: ALLOW-WILDCARD-CIDR\n",
        "CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS\nREMEDIATION: DISABLE-AUTHORIZED-NETWORKS\n",
        "REMEDIATION: USE-INTERNAL-IP-ENDPOINT\n",
        "CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS\n",
        "CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS\nREMEDIATION: USE-INTERNAL-IP-ENDPOINT\nCAUSE: CLUSTER-CONTROL-PLANE-DOWN\n",
        "CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS\nREMEDIATION: USE-INTERNAL-IP-ENDPOINT\n1. **CAUSE: CLUSTER-CONTROL-PLANE-DOWN**\n",
        "CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS\nREMEDIATION: USE-INTERNAL-IP-ENDPOINT\nREMEDIATION: ALLOW-WILDCARD-CIDR\n",
        "CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS\nREMEDIATION: USE-INTERNAL-IP-ENDPOINT\n2. **REMEDIATION: ALLOW-WILDCARD-CIDR**\n",
        "CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS?\nREMEDIATION: USE-INTERNAL-IP-ENDPOINT\n",
        "CAUSE: PUBLIC-ENDPOINT-BLOCKED-BY-AUTHORIZED-NETWORKS\nREMEDIATION: USE-INTERNAL-IP-ENDPOINT (maybe not)\n",
        "Use --internal-ip so kubectl talks to 192.0.2.10 inside 192.0.2.0/24 instead of hitting the public endpoint through Cloud NAT.",
    ):
        assert not _passes(report), report


def test_verbatim_prompt_echo_fails():
    doc = yaml.safe_load(TASK.read_text())
    assert not _passes(doc["prompt"])
