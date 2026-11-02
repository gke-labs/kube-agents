"""The backlog objective of cluster-agent-pending-replicas-capped-pool, graded as the
verifier grades it: the whole check, patterns on the pattern surface.

The positive fixtures are the shapes the nightly reports wrote; the negative ones
name no backlog: a first-turn acknowledgement that echoes the prompt, and prose
where "pending" is a suffix or means approval.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "cluster-agent-pending-replicas-capped-pool" / "task.yaml"
CHECK = "the-report-names-the-live-backlog"


def _matches(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    transcript.set(report, [], final_message=report, started_at=1.0)
    return ReportContainsVerifier(**check["check"]).verify(5.0).status == "pass"


def test_the_backlog_in_the_reports_words_matches():
    for report in (
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
    ):
        assert _matches(report), report


def test_prose_that_names_no_backlog_does_not_match():
    for report in (
        "I've started this as task `t_68d252a8` to locate the cluster and diagnose why the replicas are stalled. The answer will post into this thread as soon as it's ready.",
        "Depending on the node pool, the autoscaler may add nodes.",
        "The change is pending your approval; nothing was applied.",
        "The team is spending its budget on a larger machine type.",
        "The impending upgrade will drain the node.",
    ):
        assert not _matches(report), report
