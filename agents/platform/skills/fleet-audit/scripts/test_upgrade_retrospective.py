#!/usr/bin/env python3
"""Tests for upgrade_retrospective.py, the upgrade-retrospective collector.

Fixtures under testdata/upgrade_retrospective/ were captured from the dev
fleet on 2026-10-08: `seeded-a` (its control plane and three pools upgraded
2026-10-06..08, with planted shapes) and `gemma-gpu-upgraded` (CronJob pods
in Error, kube-dns Pending), trimmed to the namespaces the tests read.
Signatures the captures do not exhibit (a webhook rejection, an image pull
failure, a volume attach, a NotReady node, a GPU shortage) are built inline
from the same shapes.
"""

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import upgrade_retrospective as ur  # noqa: E402

FIXTURES = Path(__file__).parent / "testdata" / "upgrade_retrospective"
PROJECT = "haoxuw-gke-dev"
LOCATION = "us-central1-a"
SEEDED = f"{PROJECT}/{LOCATION}/seeded-a"
GEMMA = f"{PROJECT}/{LOCATION}/gemma-gpu-upgraded"
NOW = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)


def load(name):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


def items(name):
    return load(name)["items"]


CLUSTERS = load("clusters.json")
OPERATIONS = load("operations.json")
READS = {
    "seeded-a": {"pods": items("seeded_a_pods.json"), "nodes": items("seeded_a_nodes.json"), "events": items("seeded_a_events.json"), "pdbs": items("seeded_a_pdbs.json"), "owners": items("seeded_a_owners.json")},
    "gemma-gpu-upgraded": {"pods": items("gemma_pods.json"), "nodes": items("gemma_nodes.json"), "events": items("gemma_events.json"), "pdbs": items("gemma_pdbs.json"), "owners": items("gemma_owners.json")},
}
INFERENCE = "seeded-capacity/Deployment/inference-server"
PAYMENTS = "seeded-debug/Deployment/payments-api"
TUNER = "kubeagents-system/CronJob/legacy-flowcontrol-tuner"
KUBE_DNS = "kube-system/Deployment/kube-dns"


def cluster_doc(name):
    doc = copy.deepcopy(next(c for c in CLUSTERS if c["name"] == name))
    doc["project"] = PROJECT
    return doc


def ops_for(name, types=ur.UPGRADE_OPERATION_TYPES):
    return [o for o in OPERATIONS if f"/clusters/{name}" in o["targetLink"] and o["operationType"] in types]


def run_of(rc=0, stdout="", stderr=""):
    return ur.Run(["x"], rc, stdout, stderr, 0.01)


class FakeFleet:
    """Answers every subprocess the collector issues from the fixtures.
    `broken` names clusters whose get-credentials fails; `kubectl_fail`
    names (cluster, kind) reads that fail."""

    def __init__(self, clusters=None, operations=None, broken=(), kubectl_fail=(), list_rc=0):
        self.clusters = clusters if clusters is not None else [cluster_doc("seeded-a"), cluster_doc("gemma-gpu-upgraded")]
        self.operations = operations if operations is not None else OPERATIONS
        self.broken, self.kubectl_fail, self.list_rc = set(broken), set(kubectl_fail), list_rc
        self.calls = []

    def __call__(self, argv, *, timeout=None, env=None):
        self.calls.append(argv)
        joined = " ".join(argv)
        if "clusters list" in joined:
            if self.list_rc:
                return run_of(self.list_rc, "", "PERMISSION_DENIED")
            return run_of(0, json.dumps([{k: v for k, v in c.items() if k != "project"} for c in self.clusters]))
        if "operations list" in joined:
            return run_of(0, json.dumps(self.operations))
        if "get-credentials" in joined:
            name = argv[4]
            return run_of(1, "", f"ERROR: cluster {name} not found") if name in self.broken else run_of(0)
        if argv[0] == "kubectl":
            kind, kubeconfig = argv[2], (env or {}).get("KUBECONFIG", "")
            cluster = next((c for c in READS if f"_{c}_" in kubeconfig), None)
            key = {"pdb": "pdbs", "replicasets,jobs": "owners"}.get(kind, kind)
            if cluster is None or (cluster, key) in self.kubectl_fail:
                return run_of(1, "", "Unable to connect to the server")
            return run_of(0, json.dumps({"items": READS[cluster][key]}))
        if "config get-value" in joined:
            return run_of(0, PROJECT + "\n")
        if "projects list" in joined:
            return run_of(0, PROJECT + "\n")
        raise AssertionError(f"unexpected command {joined}")


def args(**overrides):
    base = {"project": [PROJECT], "cluster": None, "since": "14", "ledger": None, "guards": None, "output": None, "report": None, "dry_run": False}
    base.update(overrides)
    return mock.Mock(**base)


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.clusters = [cluster_doc("seeded-a"), cluster_doc("gemma-gpu-upgraded")]
        self.ledger_same = {"version": 1, "clusters": {
            SEEDED: {**ur.versions_of(self.clusters[0]), "last_run": "2026-10-08T10:00:00Z"},
            GEMMA: {**ur.versions_of(self.clusters[1]), "last_run": "2026-10-08T10:00:00Z"},
        }}

    def test_new_cluster_is_first_seen_with_the_whole_window(self):
        selected, unchanged = ur.select_clusters(self.clusters, ur.empty_ledger(), OPERATIONS, since=SINCE, forced=set())
        self.assertEqual({s.key for s in selected}, {SEEDED, GEMMA})
        self.assertEqual(unchanged, [])
        seeded = next(s for s in selected if s.key == SEEDED)
        self.assertEqual(seeded.status, "new")
        self.assertEqual(seeded.reasons, ["first seen"])
        self.assertEqual(seeded.window_start, SINCE)
        self.assertEqual([ur.operation_summary(o)["type"] for o in seeded.operations], ["UPGRADE_MASTER", "UPGRADE_NODES", "UPGRADE_NODES", "UPGRADE_NODES"])

    def test_upgraded_by_version(self):
        ledger = copy.deepcopy(self.ledger_same)
        ledger["clusters"][SEEDED]["control_plane"] = "1.34.12-gke.1011000"
        ledger["clusters"][SEEDED]["node_pools"]["default-pool"] = "1.34.12-gke.1011000"
        selected, unchanged = ur.select_clusters(self.clusters, ledger, [], since=SINCE, forced=set())
        self.assertEqual([s.key for s in selected], [SEEDED])
        self.assertEqual(selected[0].status, "upgraded")
        self.assertIn("control plane 1.34.12-gke.1011000 -> 1.35.8-gke.1380001", selected[0].reasons)
        self.assertIn("node pool default-pool 1.34.12-gke.1011000 -> 1.35.8-gke.1380001", selected[0].reasons)
        self.assertEqual([u["cluster"] for u in unchanged], [GEMMA])

    def test_upgraded_by_operation_since_last_run(self):
        ledger = copy.deepcopy(self.ledger_same)
        ledger["clusters"][SEEDED]["last_run"] = "2026-10-08T03:00:00Z"
        selected, _ = ur.select_clusters(self.clusters, ledger, OPERATIONS, since=SINCE, forced=set())
        self.assertEqual([s.key for s in selected], [SEEDED])
        self.assertEqual(selected[0].reasons, ["2 upgrade operation(s) since 2026-10-08T03:00:00Z"])
        self.assertEqual([ur.operation_summary(o)["target"] for o in selected[0].operations], ["idle-batch-pool", "pinned-inference-pool"])

    def test_unchanged_cluster_is_listed_not_reviewed(self):
        selected, unchanged = ur.select_clusters(self.clusters, self.ledger_same, OPERATIONS, since=SINCE, forced=set())
        self.assertEqual(selected, [])
        self.assertEqual([(u["cluster"], u["last_run"]) for u in unchanged], [(GEMMA, "2026-10-08T10:00:00Z"), (SEEDED, "2026-10-08T10:00:00Z")])

    def test_forced_cluster_is_reviewed_even_if_unchanged(self):
        selected, unchanged = ur.select_clusters(self.clusters, self.ledger_same, OPERATIONS, since=SINCE, forced={GEMMA})
        self.assertEqual([s.key for s in selected], [GEMMA])
        self.assertEqual(selected[0].status, "forced")
        self.assertEqual(selected[0].reasons, ["forced by --cluster"])
        self.assertEqual(selected[0].window_start, SINCE)
        self.assertEqual([u["cluster"] for u in unchanged], [SEEDED])

    def test_ledger_entry_never_reviewed_is_still_new(self):
        ledger = copy.deepcopy(self.ledger_same)
        ledger["clusters"][GEMMA]["last_run"] = None
        selected, _ = ur.select_clusters(self.clusters, ledger, [], since=SINCE, forced=set())
        self.assertEqual([(s.key, s.status) for s in selected], [(GEMMA, "new")])


class OperationsTest(unittest.TestCase):
    def test_target_link_names_pool_or_control_plane(self):
        self.assertEqual(ur.parse_target_link("https://container.googleapis.com/v1/projects/1/zones/us-central1-a/clusters/seeded-a/nodePools/default-pool"), ("us-central1-a", "seeded-a", "default-pool"))
        self.assertEqual(ur.parse_target_link("https://container.googleapis.com/v1/projects/1/locations/us-central1/clusters/host"), ("us-central1", "host", None))
        self.assertIsNone(ur.parse_target_link("nonsense"))

    def test_summary_carries_duration_and_error_text(self):
        op = copy.deepcopy(ops_for("seeded-a")[-1])
        errored = next(o for o in OPERATIONS if o.get("error"))
        op["error"], op["statusMessage"] = errored["error"], errored["statusMessage"]
        summary = ur.operation_summary(op)
        self.assertEqual(summary["type"], "UPGRADE_NODES")
        self.assertEqual(summary["target"], "pinned-inference-pool")
        self.assertEqual(summary["duration_s"], 3814)
        self.assertIn("does not have enough resources", summary["error"])
        self.assertIsNone(ur.operation_summary(ops_for("seeded-a")[0])["error"])

    def test_list_operations_filters_type_and_window_itself(self):
        fleet = FakeFleet()
        ops, error = ur.list_operations(PROJECT, datetime(2026, 10, 7, tzinfo=timezone.utc), run=fleet)
        self.assertIsNone(error)
        self.assertTrue(all(o["operationType"] in ur.UPGRADE_OPERATION_TYPES for o in ops))
        self.assertTrue(all(o["startTime"] >= "2026-10-07" for o in ops))
        self.assertIn("--filter", fleet.calls[0])
        self.assertIn("UPGRADE_MASTER OR UPGRADE_NODES", " ".join(fleet.calls[0]))

    def test_list_operations_records_a_failed_read(self):
        ops, error = ur.list_operations(PROJECT, SINCE, run=lambda argv, **kw: run_of(1, "", "denied"))
        self.assertEqual(ops, [])
        self.assertIn("rc=1", error)


def symptoms_of(name, reads=None, ops=None, window=SINCE):
    cluster = cluster_doc(name)
    return ur.collect_symptoms(cluster, reads if reads is not None else READS[name], ops if ops is not None else ops_for(name), window)


def rename_pod(pods, old, new):
    """The same failing pod under a new name, as a controller's replacement would be."""
    out = []
    for p in pods:
        if p["metadata"]["name"] == old:
            p = copy.deepcopy(p)
            p["metadata"]["name"] = new
        out.append(p)
    return out


def by_object(symptoms, obj, category=None):
    return [s for s in symptoms if s["object"] == obj and (category is None or s["category"] == category)]


def entries(symptom):
    return {(c["entry"], c["confidence"]) for c in symptom["classifications"]}


def pod(name, namespace="apps", phase="Running", containers=1, node="gke-seeded-a-default-pool-62ac8ee0-d595", statuses=None, scheduled_message=None, owner=None, images=None, node_selector=None, args_=None):
    doc = {
        "metadata": {"name": name, "namespace": namespace, "labels": {"app": name}, "ownerReferences": [{"kind": owner, "name": f"{name}-owner"}] if owner else []},
        "spec": {"nodeName": node, "containers": [{"name": f"c{i}", "image": (images or ["busybox:1.36"] * containers)[i], "args": args_ or []} for i in range(containers)], "nodeSelector": node_selector or {}},
        "status": {"phase": phase, "conditions": [], "containerStatuses": statuses or []},
    }
    if scheduled_message is not None:
        doc["status"]["phase"] = "Pending"
        doc["spec"]["nodeName"] = None
        doc["status"]["conditions"] = [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": scheduled_message}]
    elif phase == "Running" and not statuses:
        doc["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
    return doc


def event(reason, message, kind="Pod", name="x", namespace="apps", last="2026-10-08T12:00:00Z"):
    return {"type": "Warning", "reason": reason, "message": message, "involvedObject": {"kind": kind, "name": name, "namespace": namespace}, "lastTimestamp": last, "firstTimestamp": last, "count": 1, "metadata": {"creationTimestamp": last}}


def waiting(reason, message="", container="c0"):
    return {"name": container, "state": {"waiting": {"reason": reason, "message": message}}}


def oom(container="c0"):
    return {"name": container, "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}


class ClassifierFixtureTest(unittest.TestCase):
    """One captured symptom per signature the captures carry."""

    def setUp(self):
        self.seeded = symptoms_of("seeded-a")
        self.gemma = symptoms_of("gemma-gpu-upgraded")

    def test_entry_2_capacity_from_kube_dns_without_gpu_entry(self):
        rows = by_object(self.gemma, KUBE_DNS, "pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(entries(rows[0]), {(2, ur.HIGH)})
        self.assertTrue(rows[0]["system"])
        self.assertEqual(rows[0]["classifications"][0]["evidence"], "2 of 3 pods: Insufficient cpu; e.g. kube-dns-797f658fb6-cjtqz")

    def test_entry_12_is_medium_when_only_some_nodes_missed_the_selector(self):
        rows = by_object(self.seeded, INFERENCE, "pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(entries(rows[0]), {(12, ur.MEDIUM), (2, ur.HIGH)})
        twelve = next(c for c in rows[0]["classifications"] if c["entry"] == 12)
        self.assertEqual(twelve["detail"], "selector seeded-role=pinned-inference")
        self.assertEqual(twelve["evidence"], "3 of 4 pods: didn't match Pod's node affinity; e.g. inference-server-778b78fdb8-cp2pf")
        self.assertEqual(rows[0]["pods"], ["inference-server-778b78fdb8-cp2pf", "inference-server-778b78fdb8-ld26r", "inference-server-778b78fdb8-zxlkz"])

    def test_entry_14_oomkilled_single_container_on_cgroup_v2(self):
        rows = by_object(self.seeded, PAYMENTS, "not-ready")
        self.assertEqual(rows[0]["reason"], "CrashLoopBackOff")
        self.assertEqual(entries(rows[0]), {(14, ur.MEDIUM)})
        self.assertIn("1 of 1 pods: container api OOMKilled exit 137", rows[0]["classifications"][0]["evidence"])
        self.assertIn("EFFECTIVE_CGROUP_MODE_V2", rows[0]["classifications"][0]["evidence"])

    def test_entry_1_budget_held_a_drain_past_an_hour_per_node(self):
        rows = by_object(self.seeded, "seeded-capacity/PodDisruptionBudget/inference-server", "pdb")
        self.assertEqual(len(rows), 1)
        self.assertEqual(entries(rows[0]), {(1, ur.HIGH)})
        self.assertIn("pinned-inference-pool", rows[0]["classifications"][0]["evidence"])
        self.assertIn("63 min over 1 node(s)", rows[0]["classifications"][0]["evidence"])

    def test_budget_on_a_pool_nothing_drained_is_not_a_failure(self):
        self.assertEqual(by_object(self.gemma, "kubeagents-system/PodDisruptionBudget/gemma-server"), [])
        ops_without_pool = [o for o in ops_for("seeded-a") if "pinned-inference-pool" not in o["targetLink"]]
        self.assertEqual(by_object(symptoms_of("seeded-a", ops=ops_without_pool), "seeded-capacity/PodDisruptionBudget/inference-server"), [])

    def test_entry_6_job_pods_in_error_collapse_onto_their_cronjob(self):
        [row] = by_object(self.gemma, TUNER)
        self.assertEqual(row["reason"], "Error")
        self.assertEqual(row["owner_kind"], "CronJob")
        self.assertEqual(entries(row), {(6, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["evidence"], "2 of 2 pods: CronJob pod in Error; spec mentions flowcontrol; e.g. legacy-flowcontrol-tuner-29857980-9r4jr")

    def test_unclassified_symptom_is_still_reported(self):
        rows = by_object(self.seeded, "seeded-stall/Deployment/inventory-api", "not-ready")
        self.assertEqual(rows[0]["reason"], "CreateContainerConfigError")
        self.assertEqual(entries(rows[0]), {(None, ur.MEDIUM)})
        self.assertEqual(rows[0]["classifications"][0]["title"], ur.UNCLASSIFIED)

    def test_healthy_pods_are_not_symptoms(self):
        names = {s["name"] for s in self.seeded}
        self.assertNotIn("cgroup-blind-jvm-79dd4fb5c-dxxjg", names)
        self.assertNotIn("legacy-registry-pull-7979df9ddc-flfzz", names)

    def test_user_namespaces_come_before_system(self):
        flags = [s["system"] for s in self.gemma]
        self.assertEqual(flags, sorted(flags))

    def test_events_outside_the_window_are_dropped(self):
        late = symptoms_of("seeded-a", window=datetime(2026, 10, 9, tzinfo=timezone.utc))
        self.assertEqual([s for s in late if s["category"] == "event"], [])


class ClassifierSignatureTest(unittest.TestCase):
    """Signatures built from the captured shapes."""

    def classify(self, pods=(), nodes=None, events=(), pdbs=(), ops=None, name="seeded-a"):
        reads = {"pods": list(pods), "nodes": nodes if nodes is not None else READS["seeded-a"]["nodes"], "events": list(events), "pdbs": list(pdbs)}
        return symptoms_of(name, reads=reads, ops=ops)

    def test_entry_7_webhook_rejection(self):
        [row] = self.classify(events=[event("FailedCreate", 'Error creating: Internal error occurred: failed calling webhook "gate.example.io": Post "https://gate.apps.svc:443/validate": no endpoints available', kind="ReplicaSet")])
        self.assertEqual(entries(row), {(7, ur.HIGH)})
        self.assertEqual(row["classifications"][0]["detail"], "webhook gate.example.io")

    def test_entry_12_high_when_every_node_missed_the_selector(self):
        [row] = self.classify(pods=[pod("arch-pinned", scheduled_message="0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling.", node_selector={"beta.kubernetes.io/arch": "amd64"})])
        self.assertEqual(entries(row), {(12, ur.HIGH)})
        self.assertEqual(row["classifications"][0]["detail"], "selector beta.kubernetes.io/arch=amd64")

    def test_entry_19_volume_affinity_is_not_entry_12(self):
        [row] = self.classify(pods=[pod("pd-reader", scheduled_message="0/4 nodes are available: 1 node(s) had volume node affinity conflict, 3 node(s) didn't match PersistentVolume's node affinity.")])
        self.assertEqual(entries(row), {(19, ur.HIGH)})

    def test_entry_19_attach_and_mount_events(self):
        rows = self.classify(events=[event("FailedAttachVolume", "AttachVolume.Attach failed for volume pv-1"), event("FailedMount", "Unable to attach or mount volumes: unmounted volumes=[data]: timed out waiting for the condition", name="y")])
        self.assertEqual([entries(r) for r in rows], [{(19, ur.MEDIUM)}, {(19, ur.MEDIUM)}])
        self.assertEqual(rows[0]["classifications"][0]["evidence"], "FailedAttachVolume AttachVolume.Attach failed for volume pv-1")

    def test_configmap_mount_failure_is_not_entry_19(self):
        [row] = self.classify(events=[event("FailedMount", 'MountVolume.SetUp failed for volume "config" : object "gke-gmp-system"/"collector" not registered')])
        self.assertEqual(entries(row), {(None, ur.MEDIUM)})

    def test_pod_events_implied_by_the_pod_row_are_collapsed(self):
        seeded = symptoms_of("seeded-a")
        self.assertEqual([s for s in seeded if s["category"] == "event" and s["reason"] in ur.EVENT_REASONS_IMPLIED_BY_POD], [])
        self.assertEqual(len(by_object(seeded, INFERENCE)), 1)

    def test_entry_7_webhook_rejection(self):
        [row] = self.classify(events=[event("FailedCreate", 'Error creating: Internal error occurred: failed calling webhook "gate.example.io": Post "https://gate.apps.svc:443/validate": no endpoints available', kind="ReplicaSet")])
        self.assertEqual(entries(row), {(7, ur.HIGH)})
        self.assertEqual(row["classifications"][0]["detail"], "webhook gate.example.io")

    def test_entry_12_high_when_every_node_missed_the_selector(self):
        [row] = self.classify(pods=[pod("arch-pinned", scheduled_message="0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling.", node_selector={"beta.kubernetes.io/arch": "amd64"})])
        self.assertEqual(entries(row), {(12, ur.HIGH)})
        self.assertEqual(row["classifications"][0]["detail"], "selector beta.kubernetes.io/arch=amd64")

    def test_entry_19_volume_affinity_is_not_entry_12(self):
        [row] = self.classify(pods=[pod("pd-reader", scheduled_message="0/4 nodes are available: 1 node(s) had volume node affinity conflict, 3 node(s) didn't match PersistentVolume's node affinity.")])
        self.assertEqual(entries(row), {(19, ur.HIGH)})

    def test_entry_19_attach_and_mount_events(self):
        rows = self.classify(events=[event("FailedAttachVolume", "AttachVolume.Attach failed for volume pv-1"), event("FailedMount", "Unable to attach or mount volumes: unmounted volumes=[data]: timed out waiting for the condition", name="y")])
        self.assertEqual([entries(r) for r in rows], [{(19, ur.MEDIUM)}, {(19, ur.MEDIUM)}])
        self.assertEqual(rows[0]["classifications"][0]["evidence"], "FailedAttachVolume AttachVolume.Attach failed for volume pv-1")

    def test_configmap_mount_failure_is_not_entry_19(self):
        [row] = self.classify(events=[event("FailedMount", 'MountVolume.SetUp failed for volume "config" : object "gke-gmp-system"/"collector" not registered')])
        self.assertEqual(entries(row), {(None, ur.MEDIUM)})

    def test_pod_events_implied_by_the_pod_row_are_collapsed(self):
        seeded = symptoms_of("seeded-a")
        self.assertEqual([s for s in seeded if s["category"] == "event" and s["reason"] in ur.EVENT_REASONS_IMPLIED_BY_POD], [])
        self.assertEqual(len(by_object(seeded, INFERENCE)), 1)
    def test_entry_20_image_pull_names_the_host(self):
        [row] = self.classify(pods=[pod("legacy-pull", statuses=[waiting("ImagePullBackOff", 'Back-off pulling image "k8s.gcr.io/pause:3.9"')], images=["k8s.gcr.io/pause:3.9"])])
        self.assertEqual(entries(row), {(20, ur.HIGH)})
        self.assertEqual(row["classifications"][0]["detail"], "image host k8s.gcr.io")
        [row] = self.classify(pods=[pod("hub-pull", statuses=[waiting("ErrImagePull")])])
        self.assertEqual(row["classifications"][0]["detail"], f"image host {ur.DEFAULT_IMAGE_HOST}")
    def test_entry_18_gpu_shortage_and_driver_error(self):
        [row] = self.classify(pods=[pod("cuda-job", scheduled_message="0/2 nodes are available: 2 Insufficient nvidia.com/gpu.")])
        self.assertEqual(entries(row), {(18, ur.HIGH)})
        [row] = self.classify(pods=[pod("cuda-job", statuses=[{"name": "c0", "state": {"terminated": {"reason": "Error", "exitCode": 1, "message": "CUDA Error 803: system has unsupported display driver / cuda driver combination"}}}])])
        self.assertEqual(entries(row), {(18, ur.HIGH)})
    def test_entry_15_multi_container_oom(self):
        [row] = self.classify(pods=[pod("workers", containers=2, statuses=[oom("c0"), {"name": "c1", "state": {"running": {}}}])])
        self.assertEqual(entries(row), {(15, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["detail"], "2 containers")
    def test_oom_with_unknown_cgroup_mode_is_14_or_15(self):
        [row] = self.classify(pods=[pod("mystery", node="not-a-known-node", statuses=[oom()])])
        self.assertEqual(entries(row), {(14, ur.MEDIUM)})
        self.assertIn("14 or 15", row["classifications"][0]["detail"])
    def test_entry_17_node_not_ready_after_pool_upgrade(self):
        nodes = copy.deepcopy(READS["seeded-a"]["nodes"])
        broken = next(n for n in nodes if n["metadata"]["labels"]["cloud.google.com/gke-nodepool"] == "idle-batch-pool")
        for cond in broken["status"]["conditions"]:
            if cond["type"] == "Ready":
                cond["status"], cond["message"] = "False", "container runtime network not ready"
        [row] = self.classify(nodes=nodes)
        self.assertEqual(row["category"], "node")
        self.assertEqual(entries(row), {(17, ur.HIGH)})
        self.assertIn("after UPGRADE_NODES on idle-batch-pool", row["classifications"][0]["evidence"])
        [row] = self.classify(nodes=nodes, ops=[])
        self.assertEqual(entries(row), {(17, ur.MEDIUM)})
    def test_entry_17_network_unavailable(self):
        nodes = copy.deepcopy(READS["seeded-a"]["nodes"][:1])
        nodes[0]["status"]["conditions"].append({"type": "NetworkUnavailable", "status": "True", "reason": "NoRouteCreated"})
        [row] = self.classify(nodes=nodes)
        self.assertEqual(row["reason"], "NetworkUnavailable=True")
        self.assertEqual({c["entry"] for c in row["classifications"]}, {17})
    def test_entry_2_insufficient_memory(self):
        [row] = self.classify(pods=[pod("big", scheduled_message="0/4 nodes are available: 4 Insufficient memory.")])
        self.assertEqual(entries(row), {(2, ur.HIGH)})
    def test_entry_6_no_matches_for_kind(self):
        [row] = self.classify(events=[event("FailedCreate", 'error: unable to recognize "manifest.yaml": no matches for kind "FlowSchema" in version "flowcontrol.apiserver.k8s.io/v1beta3"', kind="CronJob")])
        self.assertEqual(entries(row), {(6, ur.HIGH)})
    def test_entry_6_needs_a_job_owner_and_an_error(self):
        [row] = self.classify(pods=[pod("legacy-flowcontrol-caller", phase="Failed", statuses=[{"name": "c0", "state": {"terminated": {"reason": "Error", "exitCode": 1}}}])])
        self.assertEqual(entries(row), {(None, ur.MEDIUM)})


class OwnerResolutionTest(unittest.TestCase):
    def setUp(self):
        self.resolver = ur.Resolver(READS["seeded-a"]["pods"] + READS["gemma-gpu-upgraded"]["pods"], READS["seeded-a"]["owners"] + READS["gemma-gpu-upgraded"]["owners"])

    def test_pod_replicaset_deployment(self):
        self.assertEqual(self.resolver.resolve("seeded-capacity", "Pod", "inference-server-778b78fdb8-cp2pf"), ("Deployment", "inference-server"))
        self.assertEqual(self.resolver.pod_total(INFERENCE), 4)

    def test_pod_job_cronjob(self):
        self.assertEqual(self.resolver.resolve("kubeagents-system", "Pod", "legacy-flowcontrol-tuner-29857980-9r4jr"), ("CronJob", "legacy-flowcontrol-tuner"))

    def test_pod_daemonset_stops_at_the_daemonset(self):
        self.assertEqual(self.resolver.resolve("seeded-shapes", "Pod", "cni-shaped-agent-dpp5t"), ("DaemonSet", "cni-shaped-agent"))

    def test_statefulset_and_bare_pod(self):
        sts_pod = pod("db-0", owner="StatefulSet")
        sts_pod["metadata"]["ownerReferences"][0]["name"] = "db"
        resolver = ur.Resolver([sts_pod, pod("lonely")], [])
        self.assertEqual(resolver.resolve("apps", "Pod", "db-0"), ("StatefulSet", "db"))
        self.assertEqual(resolver.resolve("apps", "Pod", "lonely"), ("Pod", "lonely"))

    def test_intermediate_the_read_missed_is_the_top(self):
        resolver = ur.Resolver(READS["seeded-a"]["pods"], [])
        self.assertEqual(resolver.resolve("seeded-capacity", "Pod", "inference-server-778b78fdb8-cp2pf"), ("ReplicaSet", "inference-server-778b78fdb8"))

    def test_replicaset_event_is_charged_to_the_deployment(self):
        reads = {**READS["seeded-a"], "events": [event("FailedCreate", 'Error creating: admission webhook "gate.example.io" denied the request: failed calling webhook "gate.example.io"', kind="ReplicaSet", name="inference-server-778b78fdb8", namespace="seeded-capacity")]}
        [row] = [s for s in symptoms_of("seeded-a", reads=reads) if s["category"] == "event"]
        self.assertEqual(row["object"], INFERENCE)
        self.assertEqual(entries(row), {(7, ur.HIGH)})
        self.assertNotIn(" of ", row["classifications"][0]["evidence"].split(":")[0])

    def test_pod_event_carries_pod_counts(self):
        pods = [p for p in READS["seeded-a"]["pods"] if "inference-server" not in p["metadata"]["name"] or p["status"]["phase"] == "Running"]
        events = [event("Unhealthy", "Readiness probe failed", name=n, namespace="seeded-capacity") for n in ("inference-server-778b78fdb8-txlv7",)]
        reads = {**READS["seeded-a"], "pods": pods, "events": events}
        [row] = by_object(symptoms_of("seeded-a", reads=reads), INFERENCE)
        self.assertEqual(row["category"], "event")
        self.assertEqual(row["classifications"][0]["evidence"], "1 of 1 pods: Unhealthy Readiness probe failed; e.g. inference-server-778b78fdb8-txlv7")

    def test_entry_1_medium_when_the_drain_was_not_held(self):
        ops = copy.deepcopy(ops_for("seeded-a"))
        quick = next(o for o in ops if "pinned-inference-pool" in o["targetLink"])
        quick["endTime"] = "2026-10-08T04:30:00Z"
        rows = [s for s in symptoms_of("seeded-a", ops=ops) if s["category"] == "pdb"]
        self.assertEqual(entries(rows[0]), {(1, ur.MEDIUM)})


class LedgerAndGuardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def collect(self, fleet=None, now=NOW, **overrides):
        fleet = fleet or FakeFleet()
        with redirect_stderr(io.StringIO()):
            return ur.collect(args(**overrides), run=fleet, now=now), fleet

    def test_ledger_round_trip_makes_the_second_run_quiet(self):
        result, _ = self.collect()
        self.assertEqual([r["cluster"] for r in result["reviews"]], [GEMMA, SEEDED])
        ledger = ur.load_json(self.home / ur.DATA_SUBDIR / ur.LEDGER_FILENAME, {})
        self.assertEqual(ledger["clusters"][SEEDED]["control_plane"], "1.35.8-gke.1380001")
        self.assertEqual(ledger["clusters"][SEEDED]["node_pools"]["pinned-inference-pool"], "1.35.8-gke.1380001")
        self.assertEqual(ledger["clusters"][SEEDED]["last_run"], "2026-10-08T18:00:00Z")
        second, _ = self.collect()
        self.assertEqual(second["reviews"], [])
        self.assertEqual([u["cluster"] for u in second["unchanged"]], [GEMMA, SEEDED])

    def test_dry_run_writes_nothing(self):
        result, _ = self.collect(dry_run=True, output=str(self.home / "out.json"))
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.home / ur.DATA_SUBDIR).exists())
        self.assertFalse((self.home / "out.json").exists())

    def test_output_json_carries_every_section(self):
        out = self.home / "out.json"
        result, _ = self.collect(output=str(out))
        doc = json.loads(out.read_text())
        self.assertEqual(doc["generated_at"], "2026-10-08T18:00:00Z")
        self.assertEqual(doc["since"], "2026-09-24T18:00:00Z")
        seeded = next(r for r in doc["reviews"] if r["cluster"] == SEEDED)
        self.assertEqual(seeded["what_happened"]["status"], "new")
        self.assertEqual(len(seeded["what_happened"]["operations"]), 4)
        self.assertEqual(seeded["what_happened"]["symptom_window_start"], "2026-10-06T04:22:12Z")
        self.assertTrue(seeded["what_failed"])
        self.assertTrue(seeded["mitigations"])
        self.assertTrue(all(m["entry"] in ur.MITIGATIONS for m in seeded["mitigations"]))
        self.assertEqual({g["entry"] for g in seeded["guards"]}, {1, 2, 12, 14})
        self.assertEqual(doc["guards"], result["guards"])
        sections = doc["sections"]
        self.assertEqual([(i["cluster"], i["object"], i["entries"]) for i in sections["errors"]], [(SEEDED, INFERENCE, "2, 12"), (SEEDED, "seeded-capacity/PodDisruptionBudget/inference-server", "1")])
        self.assertEqual([i["object"] for i in sections["warnings"]], [KUBE_DNS, TUNER, "Node/gke-seeded-a-default-pool-62ac8ee0-d595", PAYMENTS, "seeded-stall/Deployment/inventory-api"])
        self.assertEqual(sections["info"], {"clean": [], "unchanged": [], "failed_reads": []})

    def test_guards_file_merges_new_seen_again_and_gone(self):
        seen = "2026-10-08T18:00:00Z"
        fresh = ur.guards_for(SEEDED, symptoms_of("seeded-a"), seen)
        first = ur.merge_guards(ur.empty_guards(), fresh, {SEEDED}, seen)
        ids = {g["id"] for g in first["guards"]}
        self.assertEqual(ids, {ur.guard_id(SEEDED, 14, PAYMENTS), ur.guard_id(SEEDED, 2, INFERENCE), ur.guard_id(SEEDED, 12, INFERENCE), ur.guard_id(SEEDED, 1, "seeded-capacity/PodDisruptionBudget/inference-server")})
        self.assertTrue(all(g["first_seen"] == g["last_seen"] == seen for g in first["guards"]))
        # Seen again under a replacement pod: the owner key matches, so
        # last_seen moves and first_seen stays; an unreviewed cluster's guard is kept.
        other = {**first["guards"][0], "id": ur.guard_id(GEMMA, 7, "apps/Deployment/x"), "cluster": GEMMA, "entry": 7}
        first["guards"].append(other)
        later = "2026-10-15T18:00:00Z"
        replaced = {**READS["seeded-a"], "pods": rename_pod(READS["seeded-a"]["pods"], "payments-api-79b77b8c67-vfrh9", "payments-api-79b77b8c67-zz9zz")}
        second = ur.merge_guards(first, ur.guards_for(SEEDED, symptoms_of("seeded-a", reads=replaced), later), {SEEDED}, later)
        self.assertEqual(len(second["guards"]), 5)
        payments = next(g for g in second["guards"] if g["entry"] == 14)
        self.assertEqual((payments["first_seen"], payments["last_seen"]), (seen, later))
        self.assertIn("e.g. payments-api-79b77b8c67-zz9zz", payments["evidence"])
        self.assertIn(other["id"], {g["id"] for g in second["guards"]})
        # Gone: the owner reviewed again with no pod showing the symptom drops it.
        healthy = {**READS["seeded-a"], "pods": [p for p in READS["seeded-a"]["pods"] if "payments-api" not in p["metadata"]["name"]]}
        third = ur.merge_guards(second, ur.guards_for(SEEDED, symptoms_of("seeded-a", reads=healthy), later), {SEEDED}, later)
        self.assertNotIn(14, {g["entry"] for g in third["guards"]})
        self.assertIn(other["id"], {g["id"] for g in third["guards"]})

    def test_unreachable_cluster_is_recorded_not_fatal(self):
        result, _ = self.collect(FakeFleet(broken=["gemma-gpu-upgraded"]))
        gemma = next(r for r in result["reviews"] if r["cluster"] == GEMMA)
        self.assertFalse(gemma["reviewed"])
        self.assertEqual(len(gemma["read_errors"]), 1)
        self.assertIn("get-credentials rc=1", gemma["read_errors"][0])
        seeded = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertTrue(seeded["reviewed"])
        report = ur.render_report(result)
        self.assertIn(f"- {GEMMA}: get-credentials rc=1", report.split(ur.INFO_FAILED_READS)[1])
        ledger = ur.load_json(self.home / ur.DATA_SUBDIR / ur.LEDGER_FILENAME, {})
        self.assertIsNone(ledger["clusters"][GEMMA]["last_run"])
        self.assertEqual(ledger["clusters"][SEEDED]["last_run"], "2026-10-08T18:00:00Z")
        # Next run: still new, so it is retried rather than read as unchanged.
        again, _ = self.collect(FakeFleet())
        self.assertEqual([(r["cluster"], r["what_happened"]["status"]) for r in again["reviews"]], [(GEMMA, "new")])

    def test_owners_read_failure_keys_by_the_intermediate(self):
        result, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "owners")]))
        seeded = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertTrue(seeded["reviewed"])
        self.assertIn("seeded-debug/ReplicaSet/payments-api-79b77b8c67", {g["object"] for g in seeded["guards"]})

    def test_partial_kubectl_failure_keeps_the_rest(self):
        result, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "events")]))
        seeded = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertTrue(seeded["reviewed"])
        self.assertEqual([e.split(":")[0] for e in seeded["read_errors"]], ["events"])
        self.assertEqual([s for s in seeded["what_failed"] if s["category"] == "event"], [])
        self.assertIn(f"- {SEEDED}: events: kubectl get events rc=1", ur.render_report(result).split(ur.INFO_FAILED_READS)[1])

    def test_failed_cluster_listing_is_a_failed_read(self):
        result, _ = self.collect(FakeFleet(list_rc=1))
        self.assertEqual(result["reviews"], [])
        self.assertEqual(len(result["failed_reads"]), 1)
        self.assertIn("clusters list rc=1", result["failed_reads"][0])

    def test_cluster_flag_restricts_and_forces(self):
        self.collect()  # a ledger with both clusters reviewed
        result, fleet = self.collect(cluster=[GEMMA], now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual([(r["cluster"], r["what_happened"]["status"]) for r in result["reviews"]], [(GEMMA, "forced")])
        self.assertEqual(result["unchanged"], [])
        self.assertFalse(any("get-credentials" in " ".join(c) and "seeded-a" in c for c in fleet.calls))
        # seeded-a's guards from the first run are still live and unreviewed: Warnings.
        stale = [i for i in result["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD]
        self.assertEqual({i["cluster"] for i in stale}, {SEEDED})
        self.assertEqual(len(stale), 4)
        self.assertIn(f"### 14 — {SEEDED} — `{PAYMENTS}`", ur.render_report(result))
        self.assertIn("still live", ur.render_report(result))
        missing, _ = self.collect(cluster=[f"{PROJECT}/{LOCATION}/nope"])
        self.assertEqual(missing["reviews"], [])
        self.assertIn("named by --cluster but not listed", missing["failed_reads"][0])

    def test_discovery_runs_without_project_flag(self):
        result, fleet = self.collect(project=None)
        self.assertEqual(result["projects"], [PROJECT])
        self.assertTrue(any("projects list" in " ".join(c) for c in fleet.calls))


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_markdown_sections(self):
        with redirect_stderr(io.StringIO()):
            result = ur.collect(args(), run=FakeFleet(), now=NOW)
        report = ur.render_report(result)
        self.assertTrue(report.startswith("# Upgrade retrospective 2026-10-08\n"))
        errors, rest = report.split(ur.SECTION_ERRORS)[1].split(ur.SECTION_WARNINGS)
        warnings, info = rest.split(ur.SECTION_INFO)
        # Errors: entry numbers first, then cluster, then object; the four parts inline.
        self.assertEqual([line for line in errors.splitlines() if line.startswith("### ")], [
            f"### 2, 12 — {SEEDED} — `{INFERENCE}`",
            f"### 1 — {SEEDED} — `seeded-capacity/PodDisruptionBudget/inference-server`",
        ])
        inference = errors.split("### 2, 12")[1].split("### 1 —")[0]
        for part in (ur.PART_WHAT_HAPPENED, ur.PART_WHAT_FAILED, ur.PART_MITIGATE, ur.PART_MITIGATION_SET_UP):
            self.assertIn(part, inference)
        self.assertIn("| UPGRADE_NODES | pinned-inference-pool | 2026-10-08T04:20:35Z | 2026-10-08T05:24:10Z | 63 min | DONE |  |", inference)
        self.assertIn("| control plane | - | 1.35.8-gke.1380001 |", inference)
        self.assertIn("| Unschedulable | 2. No spare capacity for the displaced pods | high | 3 of 4 pods: Insufficient cpu; e.g. inference-server-778b78fdb8-cp2pf |", inference)
        self.assertIn("| Unschedulable | 12. A node label is removed (selector seeded-role=pinned-inference) | medium |", inference)
        self.assertIn(f"- **12. A node label is removed** — For {INFERENCE} (selector seeded-role=pinned-inference):", inference)
        self.assertIn(f"- guard `{ur.guard_id(SEEDED, 2, INFERENCE)}` entry 2 (high), first seen 2026-10-08T18:00:00Z", inference)
        self.assertIn("Read today: the obtainability audit (`blocking-pdb`) and the readiness report.", errors)
        # Warnings: medium, system, unclassified.
        headings = [line for line in warnings.splitlines() if line.startswith("### ")]
        self.assertEqual(headings, [
            f"### 2 — {GEMMA} — `{KUBE_DNS}` (system)",
            f"### 6 — {GEMMA} — `{TUNER}`",
            f"### unclassified — {SEEDED} — `Node/gke-seeded-a-default-pool-62ac8ee0-d595` (system)",
            f"### 14 — {SEEDED} — `{PAYMENTS}`",
            f"### unclassified — {SEEDED} — `seeded-stall/Deployment/inventory-api`",
        ])
        self.assertIn(f"{ur.PART_WHAT_HAPPENED} new (first seen); channel EXTENDED; cluster status RUNNING.", warnings)
        self.assertIn(ur.NO_OPERATION_LINE, warnings.split("### 2")[1].split("### 6")[0])
        self.assertIn("| Error | 6. A served API version is removed (best effort: a name, not an API call) | medium | 2 of 2 pods:", warnings)
        self.assertIn(f"- guard `{ur.guard_id(SEEDED, 14, PAYMENTS)}` entry 14 (medium), first seen 2026-10-08T18:00:00Z", warnings)
        # Info: nothing clean, unchanged or failed on a first run over two reviewed clusters.
        self.assertEqual(info.strip(), ur.NONE_LINE)

    def test_info_lists_clean_unchanged_and_failed(self):
        reads_clean = {**READS["seeded-a"], "pods": [p for p in READS["seeded-a"]["pods"] if p["status"]["phase"] == "Running" and "payments" not in p["metadata"]["name"]], "events": [], "pdbs": []}
        with mock.patch.dict(READS, {"seeded-a": reads_clean}):
            with redirect_stderr(io.StringIO()):
                first = ur.collect(args(), run=FakeFleet(broken=["gemma-gpu-upgraded"]), now=NOW)
        report = ur.render_report(first)
        info = report.split(ur.SECTION_INFO)[1]
        self.assertIn(f"{ur.INFO_CLEAN}\n\n- {SEEDED}: new (first seen); control plane 1.35.8-gke.1380001; UPGRADE_MASTER control plane 8 min DONE; UPGRADE_NODES default-pool 9 min DONE;", info)
        self.assertIn(f"{ur.INFO_FAILED_READS}\n\n- {GEMMA}: get-credentials rc=1", info)
        self.assertEqual(first["sections"]["errors"], [])
        with redirect_stderr(io.StringIO()):
            second = ur.collect(args(), run=FakeFleet(broken=["gemma-gpu-upgraded"]), now=NOW)
        info = ur.render_report(second).split(ur.SECTION_INFO)[1]
        self.assertIn(f"{ur.INFO_UNCHANGED}\n\n- {SEEDED} at 1.35.8-gke.1380001, last reviewed 2026-10-08T18:00:00Z", info)

    def test_failed_operation_is_an_error_incident(self):
        ops = copy.deepcopy(OPERATIONS)
        errored = next(o for o in ops if o.get("error"))
        bad = next(o for o in ops if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/idle-batch-pool" in o["targetLink"])
        bad["error"], bad["statusMessage"] = errored["error"], errored["statusMessage"]
        with redirect_stderr(io.StringIO()):
            result = ur.collect(args(), run=FakeFleet(operations=ops), now=NOW)
        op_incident = next(i for i in result["sections"]["errors"] if i["kind"] == ur.INCIDENT_OPERATION)
        self.assertEqual(op_incident["object"], "operation/UPGRADE_NODES idle-batch-pool")
        report = ur.render_report(result)
        self.assertIn(f"### operation DONE — {SEEDED} — `operation/UPGRADE_NODES idle-batch-pool`", report)
        self.assertIn(f"{ur.PART_WHAT_FAILED} UPGRADE_NODES on idle-batch-pool ended DONE: Instance", report)

    def test_main_writes_report_and_latest_link(self):
        home = Path(self.tmp.name)
        report_path = home / "reports" / "upgrade-retro-report-2026-10-08.md"
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = ur.main(["--project", PROJECT, "--since", "14d", "--output", str(home / "out.json"), "--report", str(report_path)])
        self.assertEqual(rc, 0)
        self.assertTrue(out.getvalue().startswith("# Upgrade retrospective 2026-10-08"))
        self.assertEqual(report_path.read_text(), out.getvalue())
        link = report_path.parent / ur.LATEST_REPORT_LINK
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), report_path.name)
        self.assertTrue((home / "out.json").exists())
        self.assertTrue((home / ur.DATA_SUBDIR / ur.GUARDS_FILENAME).exists())

    def test_main_dry_run_prints_but_writes_nothing(self):
        home = Path(self.tmp.name)
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                rc = ur.main(["--project", PROJECT, "--dry-run", "--report", str(home / "r.md")])
        self.assertEqual(rc, 0)
        self.assertIn(f"### 2, 12 — {SEEDED} — `{INFERENCE}`", out.getvalue())
        self.assertFalse((home / "r.md").exists())
        self.assertFalse((home / ur.DATA_SUBDIR).exists())

    def test_bad_since_is_a_usage_error(self):
        with mock.patch.object(ur, "default_run", FakeFleet()):
            with redirect_stderr(io.StringIO()) as err:
                rc = ur.main(["--project", PROJECT, "--since", "yesterday"])
        self.assertEqual(rc, 2)
        self.assertIn("--since takes", err.getvalue())


class SinceTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(ur.parse_since(None, NOW), SINCE)
        self.assertEqual(ur.parse_since("14", NOW), SINCE)
        self.assertEqual(ur.parse_since("14d", NOW), SINCE)
        self.assertEqual(ur.parse_since("2026-10-01T00:00:00Z", NOW), datetime(2026, 10, 1, tzinfo=timezone.utc))


class MitigationTableTest(unittest.TestCase):
    def test_every_catalogue_entry_has_a_row(self):
        self.assertEqual(sorted(ur.MITIGATIONS), list(range(1, 21)))
        for entry, row in ur.MITIGATIONS.items():
            for key in ("title", "before", "read_today", "mitigate_before", "mitigate_after"):
                self.assertTrue(row[key], f"{entry}.{key}")

    def test_signature_entries_are_in_the_table(self):
        self.assertTrue({s[0] for s in ur.SIGNATURES} <= set(ur.MITIGATIONS))


class DefaultRunTest(unittest.TestCase):
    def test_timeout_and_missing_binary(self):
        result = ur.default_run(["python3", "-c", "import time; time.sleep(5)"], timeout=1)
        self.assertEqual(result.rc, ur.TIMEOUT_RC)
        self.assertEqual(ur.default_run(["/nonexistent/binary"]).rc, -1)


if __name__ == "__main__":
    unittest.main()
