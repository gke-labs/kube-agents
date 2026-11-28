"""capacity-reservation-zonal-mismatch reads standalone verdict lines, not prose."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "capacity-reservation-zonal-mismatch" / "task.yaml"
CHECK = "cause-and-remediation-for-zonal-reservation-mismatch"
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
        "CAUSE: ZONAL-RESERVATION-MISMATCH\nREMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\n\nShort explanation follows.",
        "**CAUSE: ZONAL-RESERVATION-MISMATCH**\n**REMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE**\n",
        "`CAUSE: ZONAL-RESERVATION-MISMATCH`\n`REMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE`\n",
        "1. CAUSE: ZONAL-RESERVATION-MISMATCH\n2. REMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\n",
        "> CAUSE: ZONAL-RESERVATION-MISMATCH\n> REMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\n",
        "CAUSE: ZONAL-RESERVATION-MISMATCH.\nREMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE.\n",
        "CAUSE: ZONAL-RESERVATION-MISMATCH\nREMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\n\nCAUSE: PROJECT-QUOTA-EXHAUSTION is ruled out by the cluster evidence.\n",
    ):
        assert _passes(report), report


def test_wrong_missing_hedged_or_prose_only_fails():
    for report in (
        "CAUSE: PROJECT-QUOTA-EXHAUSTION\nREMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\n",
        "CAUSE: SHARED-RESERVATION-IAM-DENIED\nREMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\n",
        "CAUSE: RESERVATION-DEPLETED\nREMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\n",
        "CAUSE: ZONAL-RESERVATION-MISMATCH\nREMEDIATION: REQUEST-GPU-QUOTA-INCREASE\n",
        "CAUSE: ZONAL-RESERVATION-MISMATCH\nREMEDIATION: SWITCH-TO-ANY-RESERVATION\n",
        "REMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\n",
        "CAUSE: ZONAL-RESERVATION-MISMATCH\n",
        "CAUSE: ZONAL-RESERVATION-MISMATCH\nREMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\nCAUSE: PROJECT-QUOTA-EXHAUSTION\n",
        "CAUSE: ZONAL-RESERVATION-MISMATCH\nREMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\n1. **CAUSE: PROJECT-QUOTA-EXHAUSTION**\n",
        "CAUSE: ZONAL-RESERVATION-MISMATCH\nREMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\nREMEDIATION: REQUEST-GPU-QUOTA-INCREASE\n",
        "CAUSE: ZONAL-RESERVATION-MISMATCH\nREMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\n2. **REMEDIATION: REQUEST-GPU-QUOTA-INCREASE**\n",
        "CAUSE: ZONAL-RESERVATION-MISMATCH?\nREMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE\n",
        "CAUSE: ZONAL-RESERVATION-MISMATCH\nREMEDIATION: ALIGN-POOL-AND-RESERVATION-ZONE (maybe not)\n",
        "The gpu-train pool in us-central1-a and us-central1-b does not match the shared reservation zone in us-central1-c.",
    ):
        assert not _passes(report), report
