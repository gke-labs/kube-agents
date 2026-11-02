"""The backlog objective of cluster-agent-pending-replicas-capped-pool, graded as the
verifier grades it: the whole check, patterns on the pattern surface.

Fourteen positive fixtures are sentences the nightly reports wrote; the rest are
paraphrases of them, one per pattern, so that dropping any pattern turns a
fixture red. The negative ones name no backlog or the healthy state: a
first-turn acknowledgement that echoes the prompt, a deployment with every
replica ready, and prose where "pending" is a suffix or means approval.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "cluster-agent-pending-replicas-capped-pool" / "task.yaml"
CHECK = "the-report-names-the-live-backlog"

BACKLOG_NAMED = (
    # The recorded shapes.
    "| seeded-a | `1/5` Ready (`4` `Pending`) | `400m` CPU req > `156m` free on `pinned-inference-pool` |",
    "Deployment inference-server in namespace seeded-capacity desires 5 replicas and has 1 ready and 4 unavailable replicas.",
    "The other 4 have been Pending, waiting for a node with room, since 2026-09-03.",
    "It has 1 of 5 replicas ready because its node pool can't grow past one node.",
    "4 of the 5 requested pods are stuck in a `Pending` state.",
    "It currently has 1 ready replica and 4 pending replicas.",
    "Since that node can only fit one pod, the remaining 3 are stuck pending.",
    "The deployment is stalled at 1/3 replicas because its designated node pool has reached its maximum size.",
    "The pods cannot be scheduled because the cluster autoscaler is hard-capped.",
    "The remaining 3 pods are failing to schedule due to node resource exhaustion.",
    "3m5s  Warning  FailedScheduling  pod/inference-server-778b78fdb8-bczsc  0/4 nodes are available: 1 Insufficient cpu",
    "The pod events show: `Pod didn't trigger scale-up: 1 max node group size reached`.",
    "The surplus replicas are unschedulable rather than failing.",
    "Three of the four requested replicas are stuck in a **Pending** state on cluster `seeded-a`.",
    # A spelled-out count or a pronoun before "pending", as main's "are pending" read.
    "Four are pending.",
    "One is Running and three are Pending.",
    "The rest are pending because the pool cannot grow.",
    "The remaining three are still Pending.",
    "Four show as Pending.",
    # A verb between the count and the state word; a paraphrased event.
    "Only 1 of 5 replicas is ready.",
    "1 of the 5 replicas is ready.",
    "4 replicas are unavailable.",
    "Replicas: 1 available, 4 unavailable.",
    "The autoscaler event says the pod did not trigger scale-up.",
    "Scheduling failed: 0/1 nodes are available.",
    # One per pattern that no other admits; a wrapped phrase; the key-value shape.
    "Four pods, all Pending since Tuesday.",
    "The Pending inference-server pods wait for CPU.",
    "Desired 5, ready 1, 4 Pending.",
    "Four sit in Pending.",
    "Four show the Pending state.",
    "The replicas are\npending because the pool cannot grow.",
    "- **Status**: Pending",
)

NO_BACKLOG = (
    "I've started this as task `t_68d252a8` to locate the cluster and diagnose why the replicas are stalled. The answer will post into this thread as soon as it's ready.",
    "Depending on the node pool, the autoscaler may add nodes.",
    "The change is pending your approval; nothing was applied.",
    "The team is spending its budget on a larger machine type.",
    "The impending upgrade will drain the node.",
    "The pods are placed depending on the node pool's labels.",
    "Nothing is pending your approval.",
    # The healthy state, which must not read as a backlog.
    "inference-server is 1/1 ready on seeded-a; its nodeSelector seeded-role pins it to pinned-inference-pool. Nothing is holding replicas back.",
    "Replicas: 5 available, 0 unavailable, 0 pending. The nodeSelector seeded-role=pinned-inference is set and the deployment is healthy.",
    "Utilisation is below target, so the HPA did not trigger a scale-up.",
    "5/5 ready on seeded-a.",
    "3 of 3 replicas are available.",
    "Pods: 5 running, 0 pending.",
    # A negated or approval-sense "pending" beside a pod noun; an unscheduled window.
    "The pods are not pending.",
    "Scaling the replicas is pending your approval.",
    "Any change to the pods is pending your approval.",
    "The upgrade was not scheduled.",
)


def _matches(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    transcript.set(report, [], final_message=report, started_at=1.0)
    return ReportContainsVerifier(**check["check"]).verify(5.0).status == "pass"


@pytest.mark.parametrize("report", BACKLOG_NAMED)
def test_the_backlog_in_the_reports_words_matches(report):
    assert _matches(report)


@pytest.mark.parametrize("report", NO_BACKLOG)
def test_prose_that_names_no_backlog_does_not_match(report):
    assert not _matches(report)
