"""Pins the exclusion-holds-the-upgrade check of ``upgrades-fleet-readiness-exclusion``.

The check admits every phrasing of one fact, that the planted exclusion holds
seeded-b's upgrade, and excludes nothing. The fixtures are the nightly
answers' shapes. Each is graded through ``ReportContainsVerifier.verify()``
with the check loaded from the case file, so the test runs what the verifier
runs: patterns on the line-preserving surface, phrases on the flattened one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

CASE = (
    Path(__file__).resolve().parents[1]
    / "tasks"
    / "upgrades-fleet-readiness-exclusion"
    / "task.yaml"
)
CHECK_NAME = "the-report-says-the-exclusion-holds-the-upgrade"


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _check() -> dict:
    spec = yaml.safe_load(CASE.read_text())["verification_spec"]
    (entry,) = [e for e in spec if e["name"] == CHECK_NAME]
    return entry["check"]


def _passes(report: str) -> bool:
    transcript.set("full output", [], final_message=report, started_at=0.0)
    return ReportContainsVerifier(**_check()).verify(5.0).status == "pass"


HEAD = "**One cluster is not ready.** The other four are ready.\n\n"

STATED = {
    "blocking minor upgrades": "- **`seeded-b`**: A maintenance exclusion named `hold-the-minor-lag` is active until November 24, 2026, blocking minor upgrades.",
    "prevents auto-upgrading": "An active maintenance exclusion named `hold-the-minor-lag` with the scope `NO_MINOR_UPGRADES` prevents auto-upgrading to the target channel default (`1.36.4-gke.2046003`).",
    "holds back the automatic minor upgrade": "- Exclusion hold-the-minor-lag (NO_MINOR_UPGRADES) holds back the automatic minor upgrade of the control plane, default-pool and no-surge-pool until 2027-01-17T08:32Z.",
    "blocks automatic minor upgrades": "- **Maintenance exclusion:** `hold-the-minor-lag` (`NO_MINOR_UPGRADES`) blocks automatic minor upgrades until 2027-01-03T23:57Z.",
    "prevents minor version bumps, in a table": "| `seeded-b` | **Blocked** | Active `NO_MINOR_UPGRADES` maintenance exclusion (`hold-the-minor-lag`, valid until Jan 3, 2027) prevents minor version bumps. |",
    "blocked by an active maintenance exclusion": "## `seeded-b`\n\nThe cluster is blocked by an active maintenance exclusion (`hold-the-minor-lag`, `NO_MINOR_UPGRADES`).",
    "blocked from upgrading by exclusions": "**Three of the five clusters are blocked from upgrading by maintenance windows, maintenance exclusions, or restrictive PodDisruptionBudgets.** `seeded-b`: the `hold-the-minor-lag` exclusion (NO_MINOR_UPGRADES) is active until 2027-01-11.",
    "blocked by a scoped exclusion": "* **`seeded-b`** is blocked by a `NO_MINOR_UPGRADES` maintenance exclusion (`hold-the-minor-lag`) running until 2027-01-11.",
    "in effect until, the first green's spelling": "`seeded-b`: the exclusion `hold-the-minor-lag` (scope NO_MINOR_UPGRADES) is in effect until 2026-12-29 and excludes exactly the minor this cluster needs.",
    "set from, with a dotted date": "An active maintenance exclusion named `hold-the-minor-lag` with the scope `NO_MINOR_UPGRADES` is set from `2026-09-30` to `2026-12-29` on seeded-b.",
    "runs until": "- **Maintenance exclusion** `hold-the-minor-lag` (`NO_MINOR_UPGRADES`) runs until `2027-01-25T17:53Z` on seeded-b.",
    "the claim on one line, the name on the next": "## seeded-b\n\n**Not ready.** There is an active maintenance exclusion blocking minor version upgrades.\n*   **Maintenance Exclusion:** There is an active exclusion named `hold-the-minor-lag` with the scope `NO_MINOR_UPGRADES`.",
    "the heading two lines above the claim": "### `seeded-b`\n\nThe cluster is blocked by an active maintenance exclusion.\n\n- Exclusion: `hold-the-minor-lag`, scope `NO_MINOR_UPGRADES`.",
    "running until": "While there are no PDB blockers, the cluster has an active maintenance exclusion (`hold-the-minor-lag`) running until 2026-12-29 with the scope `NO_MINOR_UPGRADES`.",
    "pauses automatic minor upgrades": "- `seeded-b`: the `hold-the-minor-lag` exclusion (`NO_MINOR_UPGRADES`) pauses automatic minor upgrades until 2027-01-03.",
    "excludes minor upgrades, bold split": "- `seeded-b`: the `hold-the-minor-lag` exclusion **excludes** minor upgrades and defers **automatic** upgrades only.",
}

NOT_STATED = {
    "the exclusion named and scoped, nothing said of it": "- **`seeded-b`**: maintenance exclusion `hold-the-minor-lag` is configured with the scope `NO_MINOR_UPGRADES`. Node pools match the control plane.",
    "no exclusion anywhere": "All clusters are on the REGULAR release channel and none have maintenance exclusions set. seeded-b lags by one minor.",
    "another cluster's negation, in its own section": "## seeded-a\n\nReady. No exclusions block the upgrade.\n\nNode pools match the control plane.\n\n## seeded-c\n\nReady.\n\n## seeded-b\n\nLagging one minor; `hold-the-minor-lag` (`NO_MINOR_UPGRADES`) is on the record.",
    "the subject three lines away on both sides": "## seeded-b\n- version: 1.35.8\n- channel: REGULAR\n- window: daily 03:00\n- note: an active maintenance exclusion blocks minor upgrades on another member.\n- skew: none\n- pdb: none\n- record: `hold-the-minor-lag` (`NO_MINOR_UPGRADES`).",
    "a status row with drain blockers and a target": "| seeded-b | blocked | exclusion: hold-the-minor-lag (NO_MINOR_UPGRADES); drain blockers: pinned-batch-runner; upgrade target 1.36.4 |",
    "negated: no exclusion blocks": "No exclusion blocks seeded-b's upgrade; `hold-the-minor-lag` (`NO_MINOR_UPGRADES`) has lapsed.",
    "negated: no longer blocks": "- seeded-b: the hold-the-minor-lag exclusion no longer blocks the minor upgrade (scope NO_MINOR_UPGRADES).",
    "negated: not active now": "- seeded-b: the exclusion hold-the-minor-lag is not active now; the upgrade will proceed. Scope was NO_MINOR_UPGRADES.",
    "negated: not blocked by any exclusion": "seeded-b is not blocked by any maintenance exclusion; `hold-the-minor-lag` (`NO_MINOR_UPGRADES`) ended.",
    "negated: does not prevent": "- seeded-b: the hold-the-minor-lag exclusion does not prevent the minor upgrade (`NO_MINOR_UPGRADES` covers nodes only).",
    "negated: no exclusion in place until": "- seeded-b: no maintenance exclusion is in place until further notice; `hold-the-minor-lag` (`NO_MINOR_UPGRADES`) was removed.",
    "bound: a pipe between the exclusion and the verb": "| seeded-b | exclusion `hold-the-minor-lag` (`NO_MINOR_UPGRADES`) | the PDB blocks the node pool upgrade |",
    "bound: a period between the exclusion and the verb": "seeded-b carries the exclusion `hold-the-minor-lag` (`NO_MINOR_UPGRADES`). The PDB blocks the upgrade of the pool.",
    "bound: the verb more than 80 characters from the exclusion": "- seeded-b: the exclusion `hold-the-minor-lag` (`NO_MINOR_UPGRADES`) was set by the platform team last quarter for the batch window and the capacity review and it blocks the minor upgrade",
    "the verb far from the exclusion": "seeded-b carries the `hold-the-minor-lag` exclusion (`NO_MINOR_UPGRADES`). Separately, the `pinned-batch-runner` PodDisruptionBudget blocks the node drain.",
}


@pytest.mark.parametrize("report", STATED.values(), ids=list(STATED))
def test_a_stated_hold_passes(report):
    assert _passes(HEAD + report)


@pytest.mark.parametrize("report", NOT_STATED.values(), ids=list(NOT_STATED))
def test_an_unstated_hold_fails(report):
    assert not _passes(HEAD + report)
