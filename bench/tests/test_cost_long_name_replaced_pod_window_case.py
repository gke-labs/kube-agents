# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The id phrases of cost-long-name-replaced-pod-window against the ledger's id cut.

The case forbids a phrase of the long-named Deployment's finding id. The
ledger cuts an id at 100 characters, a character at a time off its longest
segment, so a phrase that holds too much of the Deployment's name is in no
id, and the case then passes on an audit that files the finding.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

CASE_ID = "cost-long-name-replaced-pod-window"
REPO_ROOT = Path(__file__).resolve().parents[2]
TASK = REPO_ROOT / "bench" / "tasks" / CASE_ID / "task.yaml"
AUDIT_SCRIPTS = REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"
CHECK_NAME = "the-long-named-deployment-is-sized-on-its-replaced-pod"

# The planted Deployment, as the case's own text names it, and its namespace.
DEPLOYMENT = "settlement-batch-reconciliation-exporter-for-partner-feeds"
NAMESPACE = "seeded-long-name"
# Cluster names from the shortest a project can give to the longest a GKE
# cluster name with its project and location can have in an id.
CLUSTER_NAMES = ["seeded-a"] + ["p" * length + "-us-central1-seeded-a" for length in (6, 14, 22, 30, 42)]


@pytest.fixture(scope="module")
def audit_report():
    sys.path.insert(0, str(AUDIT_SCRIPTS))
    try:
        import audit_report as module
    finally:
        sys.path.remove(str(AUDIT_SCRIPTS))
    return module


@pytest.fixture(scope="module")
def check() -> dict:
    case = yaml.safe_load(TASK.read_text())
    assert DEPLOYMENT in case["expected_output"].replace("\n", " ")
    [entry] = [entry for entry in case["verification_spec"] if entry["name"] == CHECK_NAME]
    return entry["check"]


def _ledger_id(audit_report, cluster: str, namespace: str, obj: str) -> str:
    return audit_report.published_id({"check": "overrequest", "cluster": cluster, "namespace": namespace, "object": obj})


@pytest.mark.parametrize("cluster", CLUSTER_NAMES)
def test_the_forbidden_phrase_is_in_the_cut_id_of_the_long_named_deployment(audit_report, check, cluster: str) -> None:
    """An audit that files the finding writes an id that holds the forbidden phrase, on each cluster name."""
    assert check["scope"] == "finding_ids"
    fid = _ledger_id(audit_report, cluster, NAMESPACE, f"Deployment/{DEPLOYMENT}")
    assert len(fid) <= audit_report.MAX_FINDING_ID
    # The id is cut on each of these cluster names, so the whole name is in none of them.
    assert DEPLOYMENT not in fid
    for phrase in check["forbidden_phrases"]:
        assert phrase in fid, (phrase, fid)


@pytest.mark.parametrize("cluster", CLUSTER_NAMES)
def test_the_required_phrase_is_in_the_id_of_the_family(audit_report, check, cluster: str) -> None:
    """The family's finding, which shows that the run wrote a ledger, keeps the required phrase."""
    fid = _ledger_id(audit_report, cluster, "seeded-family", "Deployment/calibration")
    for phrase in check["required_phrases"]:
        assert phrase in fid, (phrase, fid)
