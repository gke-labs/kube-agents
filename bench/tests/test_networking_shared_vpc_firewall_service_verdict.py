"""networking-shared-vpc-firewall-service reads standalone verdict lines, not prose."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "networking-shared-vpc-firewall-service" / "task.yaml"
CHECK = "shared-vpc-firewall-project-and-exposure-stated"
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
        "RULE-PROJECT: SHARED-VPC-HOST-PROJECT\nEXPOSURE: PUBLIC-INTERNET-OPEN\n\nShort explanation follows.",
        "**RULE-PROJECT: SHARED-VPC-HOST-PROJECT**\n**EXPOSURE: PUBLIC-INTERNET-OPEN**\n",
        "`RULE-PROJECT: SHARED-VPC-HOST-PROJECT`\n`EXPOSURE: PUBLIC-INTERNET-OPEN`\n",
        "1. RULE-PROJECT: SHARED-VPC-HOST-PROJECT\n2. EXPOSURE: PUBLIC-INTERNET-OPEN\n",
        "> RULE-PROJECT: SHARED-VPC-HOST-PROJECT\n> EXPOSURE: PUBLIC-INTERNET-OPEN\n",
        "RULE-PROJECT: SHARED-VPC-HOST-PROJECT.\nEXPOSURE: PUBLIC-INTERNET-OPEN.\n",
        "RULE-PROJECT: SHARED-VPC-HOST-PROJECT\nEXPOSURE: PUBLIC-INTERNET-OPEN\n\nRULE-PROJECT: SERVICE-PROJECT-ONLY is ruled out by the cluster evidence.\n",
    ):
        assert _passes(report), report


def test_wrong_missing_hedged_or_prose_only_fails():
    for report in (
        "RULE-PROJECT: SERVICE-PROJECT-ONLY\nEXPOSURE: PUBLIC-INTERNET-OPEN\n",
        "RULE-PROJECT: NO-FIREWALL-RULE-CREATED\nEXPOSURE: PUBLIC-INTERNET-OPEN\n",
        "RULE-PROJECT: SHARED-VPC-HOST-PROJECT\nEXPOSURE: PRIVATE-VPC-ONLY\n",
        "RULE-PROJECT: SHARED-VPC-HOST-PROJECT\nEXPOSURE: BLOCKED-BY-DEFAULT-DENY\n",
        "EXPOSURE: PUBLIC-INTERNET-OPEN\n",
        "RULE-PROJECT: SHARED-VPC-HOST-PROJECT\n",
        "RULE-PROJECT: SHARED-VPC-HOST-PROJECT\nEXPOSURE: PUBLIC-INTERNET-OPEN\nRULE-PROJECT: SERVICE-PROJECT-ONLY\n",
        "RULE-PROJECT: SHARED-VPC-HOST-PROJECT\nEXPOSURE: PUBLIC-INTERNET-OPEN\n1. **RULE-PROJECT: SERVICE-PROJECT-ONLY**\n",
        "RULE-PROJECT: SHARED-VPC-HOST-PROJECT\nEXPOSURE: PUBLIC-INTERNET-OPEN\nEXPOSURE: PRIVATE-VPC-ONLY\n",
        "RULE-PROJECT: SHARED-VPC-HOST-PROJECT\nEXPOSURE: PUBLIC-INTERNET-OPEN\n2. **EXPOSURE: PRIVATE-VPC-ONLY**\n",
        "RULE-PROJECT: SHARED-VPC-HOST-PROJECT?\nEXPOSURE: PUBLIC-INTERNET-OPEN\n",
        "RULE-PROJECT: SHARED-VPC-HOST-PROJECT\nEXPOSURE: PUBLIC-INTERNET-OPEN (maybe not)\n",
        "Check net-shared-host because GKE creates k8s-fw firewall rules in the Shared VPC host project and payments-api is publicly exposed.",
    ):
        assert not _passes(report), report
