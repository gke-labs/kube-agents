"""cost-finished-job-family-grouped checks family grouping, TTL fix, and slot-a cluster."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "cost-finished-job-family-grouped"
    / "task.yaml"
)
CHECK = "report-groups-reindex-job-family-and-names-ttl-fix"
TIMEOUT_SEC = 5.0


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _passes(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    cfg = dict(check["check"])
    # Exercise non-cluster patterns directly; cluster placeholder expansion is pinned by test_fleet_verifier.
    cfg.pop("fixture_roles", None)
    cfg.pop("any_of_patterns", None)
    transcript.set(report, [], final_message=report)
    return ReportContainsVerifier(**cfg).verify(TIMEOUT_SEC).success


def test_accepts_grouped_family_finding_with_ttl_fix():
    report = (
        "On seeded-a in namespace `seeded-family`, the three completed `reindex-*` "
        "Jobs form a single job family. Add `spec.ttlSecondsAfterFinished` in the "
        "parent template instead of editing individual Jobs."
    )
    assert _passes(report)


def test_rejects_missing_ttl_fix():
    report = (
        "In `seeded-family`, the three `reindex-*` Jobs belong to one job family."
    )
    assert not _passes(report)


def test_rejects_missing_grouping_language():
    report = (
        "In `seeded-family`, `reindex-8daede4e` lacks `ttlSecondsAfterFinished`."
    )
    assert not _passes(report)
