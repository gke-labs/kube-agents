"""capacity-gpu-sharing-g4-l4-t4-fallback reads standalone verdict lines, not prose."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "capacity-gpu-sharing-g4-l4-t4-fallback" / "task.yaml"
CHECK = "gpu-sharing-fallback-blocker-and-fix-stated"
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
        "FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR\nFIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\n\nShort explanation follows.",
        "**FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR**\n**FIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS**\n",
        "`FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR`\n`FIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS`\n",
        "1. FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR\n2. FIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\n",
        "> FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR\n> FIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\n",
        "FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR.\nFIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS.\n",
        "FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR\nFIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\n\nFALLBACK-STATUS: AUTOMATIC-TO-T4-DEDICATED is ruled out by the cluster evidence.\n",
    ):
        assert _passes(report), report


def test_wrong_missing_hedged_or_prose_only_fails():
    for report in (
        "FALLBACK-STATUS: AUTOMATIC-TO-T4-DEDICATED\nFIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\n",
        "FALLBACK-STATUS: AUTOMATIC-TO-CPU-POOL\nFIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\n",
        "FALLBACK-STATUS: BLOCKED-BY-QUOTA-ONLY\nFIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\n",
        "FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR\nFIX: INCREASE-L4-MAX-NODES-ONLY\n",
        "FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR\nFIX: REMOVE-COMPUTECLASS-LABEL\n",
        "FIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\n",
        "FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR\n",
        "FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR\nFIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\nFALLBACK-STATUS: AUTOMATIC-TO-T4-DEDICATED\n",
        "FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR\nFIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\n1. **FALLBACK-STATUS: AUTOMATIC-TO-T4-DEDICATED**\n",
        "FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR\nFIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\nFIX: INCREASE-L4-MAX-NODES-ONLY\n",
        "FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR\nFIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\n2. **FIX: INCREASE-L4-MAX-NODES-ONLY**\n",
        "FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR?\nFIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS\n",
        "FALLBACK-STATUS: BLOCKED-BY-SHARING-NODESELECTOR\nFIX: ALIGN-SHARING-CONFIG-ACROSS-FALLBACK-POOLS (maybe not)\n",
        "The time-sharing nodeSelector prevents fallback from G4 and L4 onto the dedicated T4 pool until GPU sharing matches.",
    ):
        assert not _passes(report), report
