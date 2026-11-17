"""The three report checks of cluster-agent-pending-compute-class-prefilter-skip, graded as
the verifier grades them: each whole check, patterns on the pattern surface.

Every fixture is written for this test; none is text from a run. Each alternative of a
pattern has one fixture that only it admits, so that dropping one turns a fixture red. The
negative fixtures are the forms that the case's comments say must not pass: the pool's own
label, a sentence about what is absent, a node pool's JSON, and the capacity fixes.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "cluster-agent-pending-compute-class-prefilter-skip" / "task.yaml"
PATCH_CHECK = "the-reply-carries-the-class-for-the-pods"
VERSION_CHECK = "the-report-states-the-version-of-the-record"
CAPACITY_CHECK = "the-reply-offers-no-capacity-change"

PATCH_GIVEN = (
    # A nodeSelector block, plain and with a comment line before the entry.
    "```yaml\nspec:\n  template:\n    spec:\n      nodeSelector:\n        cloud.google.com/compute-class: orders-class\n```",
    "```yaml\n      nodeSelector:\n        # name the class of the pool\n        cloud.google.com/compute-class: \"orders-class\"\n```",
    # An inline map.
    "Patch: `nodeSelector: {cloud.google.com/compute-class: orders-class}` in the pod template.",
    # A required nodeAffinity term.
    "```yaml\n- matchExpressions:\n    - key: cloud.google.com/compute-class\n      operator: In\n      values: [\"orders-class\"]\n```",
    "```yaml\n    - key: cloud.google.com/compute-class\n      operator: In\n      values:\n        - orders-class\n```",
    # The namespace's default-class label, as a manifest and as a command.
    "```yaml\nkind: Namespace\nmetadata:\n  name: orders\n  labels:\n    kubernetes.io/metadata.name: orders\n    cloud.google.com/default-compute-class: orders-class\n```",
    "```yaml\n  labels:\n    cloud.google.com/default-compute-class-non-daemonset: orders-class\n```",
    "```bash\nkubectl label namespace orders cloud.google.com/default-compute-class=orders-class\n```",
    "```bash\nkubectl label ns orders --overwrite cloud.google.com/default-compute-class=orders-class\n```",
)

PATCH_NOT_GIVEN = (
    # The pool's own label, in the forms a reply can quote it.
    "The only node has the label cloud.google.com/compute-class=orders-class.",
    "The pool has no taint and carries the label cloud.google.com/compute-class: orders-class.",
    "Node labels:\n  cloud.google.com/compute-class: orders-class",
    '{"name": "default-pool", "config": {"labels": {"cloud.google.com/compute-class": "orders-class"}}}',
    # A sentence about what is absent.
    "The namespace lacks cloud.google.com/default-compute-class=orders-class.",
    "The pod has no nodeSelector, and the node has cloud.google.com/compute-class=orders-class.",
    # The patch in a sentence only: the stated limit of the check.
    "Add the class orders-class to the pod template's node selector.",
    # Another patch.
    "```yaml\nresources:\n  requests:\n    cpu: 250m\n    memory: 256Mi\n```",
)

VERSION_STATED = (
    "The cluster runs GKE 1.35.8-gke.1796000.",
    "Server version: `v1.35.8-gke.1796000`.",
    "currentMasterVersion: 1.35.8-gke.1796000",
)

VERSION_NOT_STATED = (
    "The skip was seen on GKE 1.35.8 and was not seen on GKE 1.36.4.",
    "The cluster runs GKE 1.36.4-gke.2377000.",
    "The cluster runs 11.35.8-gke.1796000.",
    "The cluster runs 1.35.8-gke.17960001.",
)

NO_CAPACITY_CHANGE = (
    "```yaml\n      nodeSelector:\n        cloud.google.com/compute-class: orders-class\n      containers:\n        - name: worker\n          resources:\n            requests:\n              cpu: 500m\n              memory: 256Mi\n```",
    "Do not enlarge the pool: it has 1 node of a maximum of 10. The node's allocatable cpu is 940m.",
    '"autoscaling": {"enabled": true, "minNodeCount": 1, "maxNodeCount": 10}',
)

CAPACITY_CHANGE = (
    "```yaml\nresources:\n  requests:\n    cpu: 250m\n    memory: 256Mi\n```",
    "```yaml\n          requests:\n            cpu: \"0.25\"\n```",
    "```bash\ngcloud container clusters update orders-prod-1 --enable-autoscaling --node-pool default-pool\n```",
    "```bash\ngcloud container node-pools update default-pool --cluster orders-prod-1 --max-nodes 20\n```",
    "```bash\ngcloud container node-pools update default-pool --total-max-nodes=20\n```",
    "```bash\ngcloud container clusters resize orders-prod-1 --node-pool default-pool --num-nodes 3\n```",
)


def _passes(check_name: str, report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == check_name]
    transcript.set(report, [], final_message=report, started_at=1.0)
    return ReportContainsVerifier(**check["check"]).verify(5.0).status == "pass"


@pytest.mark.parametrize("report", PATCH_GIVEN)
def test_the_class_as_a_manifest_or_a_command_passes(report):
    assert _passes(PATCH_CHECK, report)


@pytest.mark.parametrize("report", PATCH_NOT_GIVEN)
def test_the_pools_label_an_absence_or_another_patch_does_not_pass(report):
    assert not _passes(PATCH_CHECK, report)


@pytest.mark.parametrize("report", VERSION_STATED)
def test_the_version_of_the_record_passes(report):
    assert _passes(VERSION_CHECK, report)


@pytest.mark.parametrize("report", VERSION_NOT_STATED)
def test_the_skills_own_sentence_or_another_version_does_not_pass(report):
    assert not _passes(VERSION_CHECK, report)


@pytest.mark.parametrize("report", NO_CAPACITY_CHANGE)
def test_a_reply_with_no_capacity_change_passes(report):
    assert _passes(CAPACITY_CHECK, report)


@pytest.mark.parametrize("report", CAPACITY_CHANGE)
def test_a_changed_request_or_a_pool_sizing_flag_does_not_pass(report):
    assert not _passes(CAPACITY_CHECK, report)
