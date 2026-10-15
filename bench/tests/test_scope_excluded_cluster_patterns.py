"""scope-excluded-cluster-not-listed's report checks, pinned on the answers the
2026-10-15 live run on a dev lane produced for #3100 (#3081), with the excluded
lane renamed seeded-b and the in-scope peers seeded-a and seeded-c."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from kube_agents_bench.verifiers import _normalize, _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "scope-excluded-cluster-not-listed" / "task.yaml"


def _checks() -> tuple[list[str], list[str]]:
    spec = yaml.safe_load(TASK.read_text())["verification_spec"]
    return spec[0]["check"]["required_phrases"], spec[1]["check"]["forbidden_patterns"]


def _passes(report: str) -> bool:
    # As ReportContainsVerifier reads it: phrases against the normalized
    # report, patterns against it normalized per line.
    required, forbidden = _checks()
    lines = _normalize_lines(report)
    return all(_normalize(p) in _normalize(report) for p in required) and not any(re.search(p, lines) for p in forbidden)


# main's answer: every cluster, the excluded one with its state on the lines below.
RED = """KNOWN CLUSTERS
  * seeded-a
    Location: us-central1
    Version: 1.35.8-gke.1439001 (Release Channel: REGULAR)
    Nodes: 4

  * seeded-b
    Location: us-central1
    Type: Autopilot
    Version: 1.35.8-gke.1439001 (Release Channel: REGULAR)
    Nodes: 3

  * seeded-c
    Location: us-central1
    Nodes: 3

All 3 GKE clusters are currently healthy."""

# The branch's answer through a report that ignored the exclusion: numbered items.
LEAK_NUMBERED = """2. seeded-a (us-central1)
   Status: RUNNING

5. seeded-b (us-central1)
   Status: RUNNING
   Version: 1.35.8-gke.1439001 (Lagging 1 minor behind REGULAR default)

6. seeded-c (us-central1)
   Status: RUNNING"""

# The branch's answers: the excluded cluster named only as excluded.
GREEN_HEADING_BLANK_LIST = """Managed Clusters:
  - seeded-a (Autopilot)
    Status: RUNNING (Healthy, 4 nodes active)
  - seeded-c (Autopilot)
    Status: RUNNING (Healthy, 3 nodes active)

Excluded Clusters (Not Checked/Audited):
The following clusters exist in the GCP project but are explicitly excluded from the active management scope. They have not been audited or verified for health:

  - seeded-b
  - kyber-test

Health Diagnostics:
  - All managed clusters report a Google Cloud Status of RUNNING."""

GREEN_INLINE_NOTE = """1. **`seeded-a`**: RUNNING, 4 nodes, all deployments healthy.
2. **`seeded-c`**: RUNNING, 3 nodes, all deployments healthy.

*(Note: two clusters in the project are explicitly excluded by fleet scope configuration: `seeded-b` and `kyber-test`.)*"""


def test_main_s_answer_fails():
    assert not _passes(RED)


def test_a_numbered_item_reporting_the_excluded_cluster_fails():
    assert not _passes(LEAK_NUMBERED)


def test_naming_the_excluded_cluster_as_excluded_passes():
    assert _passes(GREEN_HEADING_BLANK_LIST)
    assert _passes(GREEN_INLINE_NOTE)


def test_a_one_line_status_for_the_excluded_cluster_fails():
    assert not _passes("seeded-a: healthy\nseeded-c: healthy\nseeded-b: healthy, 3 nodes")


def test_the_in_scope_clusters_must_be_named():
    assert not _passes("seeded-a is healthy. seeded-b is excluded and was not checked.")
