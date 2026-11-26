"""node-nf-conntrack-table-saturation's terminal safeguards are the pod range case's.

The case forbids ``gcloud compute`` writes and ``gcloud container`` cluster or
node-pool writes with the four entries of
networking-pod-cidr-slash24-exhaustion, word for word.
``test_pod_cidr_case_safeguard_patterns.py`` grades those entries through the
verifier against write and read commands. This test holds the two copies
equal, so what that test pins holds for this case too.
"""

from pathlib import Path

import pytest
import yaml

TASKS = Path(__file__).resolve().parent.parent / "tasks"
CASE = "node-nf-conntrack-table-saturation"
SOURCE_CASE = "networking-pod-cidr-slash24-exhaustion"
COPIED_ENTRIES = [
    "the-audit-changed-nothing-in-gcp",
    "no-gcp-write-chained-behind-a-read",
    "the-audit-changed-no-cluster",
    "no-cluster-write-chained-behind-a-read",
]


def _entries(case: str) -> dict[str, dict]:
    spec = yaml.safe_load((TASKS / case / "task.yaml").read_text())["verification_spec"]
    return {entry["name"]: entry for entry in spec}


@pytest.mark.parametrize("name", COPIED_ENTRIES)
def test_the_entry_is_the_pod_range_cases_word_for_word(name):
    assert _entries(CASE)[name] == _entries(SOURCE_CASE)[name]
