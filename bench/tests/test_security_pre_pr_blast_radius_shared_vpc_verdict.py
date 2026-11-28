"""security-pre-pr-blast-radius-shared-vpc reads standalone verdict lines, not prose."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "security-pre-pr-blast-radius-shared-vpc" / "task.yaml"
CHECK = "terraform-shared-vpc-blast-radius-verdict-stated"
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
        "CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION\nPR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\n\nShort explanation follows.",
        "**CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION**\n**PR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION**\n",
        "`CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION`\n`PR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION`\n",
        "1. CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION\n2. PR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\n",
        "> CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION\n> PR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\n",
        "CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION.\nPR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION.\n",
        "CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION\nPR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\n\nCHANGE-IMPACT: IN-PLACE-CONTROL-PLANE-UPDATE is ruled out by the cluster evidence.\n",
    ):
        assert _passes(report), report


def test_wrong_missing_hedged_or_prose_only_fails():
    for report in (
        "CHANGE-IMPACT: IN-PLACE-CONTROL-PLANE-UPDATE\nPR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\n",
        "CHANGE-IMPACT: ROLLING-NODE-POOL-ONLY\nPR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\n",
        "CHANGE-IMPACT: METADATA-ONLY-NO-DISRUPTION\nPR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\n",
        "CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION\nPR-VERDICT: SAFE-TO-MERGE-IN-PLACE\n",
        "CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION\nPR-VERDICT: SAFE-WITH-MAINTENANCE-WINDOW-ONLY\n",
        "PR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\n",
        "CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION\n",
        "CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION\nPR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\nCHANGE-IMPACT: IN-PLACE-CONTROL-PLANE-UPDATE\n",
        "CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION\nPR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\n1. **CHANGE-IMPACT: IN-PLACE-CONTROL-PLANE-UPDATE**\n",
        "CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION\nPR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\nPR-VERDICT: SAFE-TO-MERGE-IN-PLACE\n",
        "CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION\nPR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\n2. **PR-VERDICT: SAFE-TO-MERGE-IN-PLACE**\n",
        "CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION?\nPR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION\n",
        "CHANGE-IMPACT: FORCE-NEW-CLUSTER-RECREATION\nPR-VERDICT: BLOCK-AND-REQUIRE-MIGRATION (maybe not)\n",
        "Modifying cluster_secondary_range_name is ForceNew in Terraform and recreates the production GKE cluster, so block this PR.",
    ):
        assert not _passes(report), report
