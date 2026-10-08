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
import re
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
PROJECT = "example-project"
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
for _op in OPERATIONS:
    _op["project"] = PROJECT  # `collect` tags each operation with its project; the fixtures are raw gcloud output
READ_FILES = {"pods": "pods", "nodes": "nodes", "events": "events", "pdbs": "pdbs", "owners": "owners", "workloads": "workloads", "storage": "storage", "webhooks": "webhooks", "endpointslices": "endpointslices"}
READS = {
    "seeded-a": {key: items(f"seeded_a_{name}.json") for key, name in READ_FILES.items()},
    "gemma-gpu-upgraded": {key: items(f"gemma_{name}.json") for key, name in READ_FILES.items()},
}
SERVER_CONFIG = load("serverconfig_us-central1-a.json")
# The project variables discovery reads; cleared so a developer's or the
# sandbox's environment cannot change what a test sees.
NO_PROJECT_ENV = {var: "" for var in (ur.MONITORED_PROJECTS_ENV, *ur.PROJECT_ENV_VARS)}
KUBECTL_KINDS = {"pdb": "pdbs", "replicasets,jobs": "owners", "deploy,ds,sts,cronjobs": "workloads", "pv,storageclasses": "storage", "validatingwebhookconfigurations,mutatingwebhookconfigurations": "webhooks"}
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
        if "get-server-config" in joined:
            return run_of(0, json.dumps(SERVER_CONFIG))
        if "get-credentials" in joined:
            name = argv[4]
            return run_of(1, "", f"ERROR: cluster {name} not found") if name in self.broken else run_of(0)
        if argv[0] == "kubectl":
            kind, kubeconfig = argv[2], (env or {}).get("KUBECONFIG", "")
            cluster = next((c for c in READS if f"_{c}_" in kubeconfig), None)
            key = KUBECTL_KINDS.get(kind, kind)
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
        selected, unchanged, _ = ur.select_clusters(self.clusters, ur.empty_ledger(), OPERATIONS, since=SINCE, forced=set())
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
        selected, unchanged, _ = ur.select_clusters(self.clusters, ledger, [], since=SINCE, forced=set())
        self.assertEqual([s.key for s in selected], [SEEDED])
        self.assertEqual(selected[0].status, "upgraded")
        self.assertIn("control plane 1.34.12-gke.1011000 -> 1.35.8-gke.1380001", selected[0].reasons)
        self.assertIn("node pool default-pool 1.34.12-gke.1011000 -> 1.35.8-gke.1380001", selected[0].reasons)
        self.assertEqual([u["cluster"] for u in unchanged], [GEMMA])

    def test_upgraded_by_operation_since_last_run(self):
        ledger = copy.deepcopy(self.ledger_same)
        ledger["clusters"][SEEDED]["last_run"] = "2026-10-08T03:00:00Z"
        selected, _, _ = ur.select_clusters(self.clusters, ledger, OPERATIONS, since=SINCE, forced=set())
        self.assertEqual([s.key for s in selected], [SEEDED])
        self.assertEqual(selected[0].reasons, ["2 upgrade operation(s) since 2026-10-08T03:00:00Z"])
        self.assertEqual([ur.operation_summary(o)["target"] for o in selected[0].operations], ["idle-batch-pool", "pinned-inference-pool"])

    def test_unchanged_cluster_is_listed_not_reviewed(self):
        selected, unchanged, _ = ur.select_clusters(self.clusters, self.ledger_same, OPERATIONS, since=SINCE, forced=set())
        self.assertEqual(selected, [])
        self.assertEqual([(u["cluster"], u["last_run"]) for u in unchanged], [(GEMMA, "2026-10-08T10:00:00Z"), (SEEDED, "2026-10-08T10:00:00Z")])

    def test_forced_cluster_is_reviewed_even_if_unchanged(self):
        selected, unchanged, _ = ur.select_clusters(self.clusters, self.ledger_same, OPERATIONS, since=SINCE, forced={GEMMA})
        self.assertEqual([s.key for s in selected], [GEMMA])
        self.assertEqual(selected[0].status, "forced")
        self.assertEqual(selected[0].reasons, ["forced by --cluster"])
        self.assertEqual(selected[0].window_start, SINCE)
        self.assertEqual([u["cluster"] for u in unchanged], [SEEDED])

    def test_operations_are_keyed_by_project(self):
        other = [c for c in self.clusters if c["name"] == "seeded-a"][0]
        twin = copy.deepcopy(other)
        twin["project"] = "other-project"
        ops = copy.deepcopy(OPERATIONS)
        for op in ops:
            op["project"] = PROJECT
        selected, _, _ = ur.select_clusters([other, twin], ur.empty_ledger(), ops, since=SINCE, forced=set())
        by_key = {s.key: s for s in selected}
        self.assertEqual(len(by_key[SEEDED].operations), 4)
        self.assertEqual(by_key[f"other-project/{LOCATION}/seeded-a"].operations, [])

    def test_new_pool_is_not_an_upgrade(self):
        ledger = copy.deepcopy(self.ledger_same)
        del ledger["clusters"][SEEDED]["node_pools"]["idle-batch-pool"]
        selected, unchanged, _ = ur.select_clusters(self.clusters, ledger, [], since=SINCE, forced=set())
        self.assertEqual(selected, [])
        self.assertEqual(len(unchanged), 2)

    def test_partial_read_reselects_the_cluster(self):
        ledger = copy.deepcopy(self.ledger_same)
        ledger["clusters"][SEEDED]["partial_read"] = "2026-10-08T12:00:00Z"
        selected, _, _ = ur.select_clusters(self.clusters, ledger, [], since=SINCE, forced=set())
        self.assertEqual([(s.key, s.reasons) for s in selected], [(SEEDED, ["previous review at 2026-10-08T12:00:00Z read the cluster partially"])])

    def test_ledger_entry_never_reviewed_is_still_new(self):
        ledger = copy.deepcopy(self.ledger_same)
        ledger["clusters"][GEMMA]["last_run"] = None
        selected, _, _ = ur.select_clusters(self.clusters, ledger, [], since=SINCE, forced=set())
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
        # No node-pool operation in gemma's window: capacity is medium.
        self.assertEqual(entries(rows[0]), {(2, ur.MEDIUM)})
        self.assertTrue(rows[0]["system"])
        self.assertEqual(rows[0]["classifications"][0]["evidence"], "2 of 3 pods: Insufficient cpu; e.g. kube-dns-797f658fb6-cjtqz")
        self.assertIn("no node-pool operation touched this pool", rows[0]["classifications"][0]["detail"])

    def test_pool_gated_entries_are_high_only_after_a_pool_operation(self):
        reads = {**READS["seeded-a"]}
        rows = by_object(symptoms_of("seeded-a", reads=reads, ops=[]), INFERENCE, "pending")
        self.assertEqual(entries(rows[0]), {(12, ur.MEDIUM), (2, ur.MEDIUM)})
        rows = by_object(symptoms_of("seeded-a"), INFERENCE, "pending")
        self.assertEqual(entries(rows[0]), {(12, ur.MEDIUM), (2, ur.HIGH)})

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
        self.assertEqual(rows[0]["classifications"][0]["detail"], "14 or 15 on cgroup v2; 1 container(s)")
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

    def test_pods_whose_activity_predates_the_window_are_dropped(self):
        old = pod("old-job-abcde", phase="Failed", owner="Job", statuses=[{"name": "c0", "state": {"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": "2026-08-01T00:00:00Z"}}}])
        reads = {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [old]}
        names = {s["name"] for s in symptoms_of("seeded-a", reads=reads)}
        self.assertNotIn("old-job-abcde-owner", names)
        self.assertNotIn("old-job-abcde", names)
        old["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"] = "2026-10-08T01:00:00Z"
        names = {s["name"] for s in symptoms_of("seeded-a", reads=reads)}
        self.assertIn("old-job-abcde-owner", names)

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
        rows = self.classify(events=[event("FailedAttachVolume", "AttachVolume.Attach failed for volume pv-1"), event("FailedMount", "MountVolume.WaitForAttach failed for volume pvc-0b7c: timed out waiting for the condition", name="y")])
        self.assertEqual([entries(r) for r in rows], [{(19, ur.MEDIUM)}, {(19, ur.MEDIUM)}])
        self.assertEqual(rows[0]["classifications"][0]["evidence"], "FailedAttachVolume AttachVolume.Attach failed for volume pv-1")

    def test_configmap_mount_failure_is_not_entry_19(self):
        [row] = self.classify(events=[event("FailedMount", 'MountVolume.SetUp failed for volume "config" : object "gke-gmp-system"/"collector" not registered')])
        self.assertEqual(entries(row), {(None, ur.MEDIUM)})
        # The kubelet's generic mount-timeout sentence names ConfigMaps too.
        [row] = self.classify(events=[event("FailedMount", "Unable to attach or mount volumes: unmounted volumes=[config], unattached volumes=[config kube-api-access-x]: timed out waiting for the condition")])
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
        # Without a pool operation the driver error is medium like the other pool-gated entries.
        [row] = self.classify(pods=[pod("cuda-job", statuses=[{"name": "c0", "state": {"terminated": {"reason": "Error", "exitCode": 1, "message": "CUDA Error 803"}}}])], ops=[])
        self.assertEqual(entries(row), {(18, ur.MEDIUM)})
        # An image name containing `nvidia` in a pull back-off is entry 20, not 18.
        [row] = self.classify(pods=[pod("torch", statuses=[waiting("ImagePullBackOff", 'Back-off pulling image "nvcr.io/nvidia/pytorch:24.01"')], images=["nvcr.io/nvidia/pytorch:24.01"])])
        self.assertEqual({c["entry"] for c in row["classifications"]}, {20})

    def test_multi_container_oom_is_14_or_15_only_on_cgroup_v2(self):
        [row] = self.classify(pods=[pod("workers", containers=2, statuses=[oom("c0"), {"name": "c1", "state": {"running": {}}}])])
        self.assertEqual(entries(row), {(14, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["detail"], "14 or 15 on cgroup v2; 2 container(s)")
        cluster = cluster_doc("seeded-a")
        for p in cluster["nodePools"]:
            p["config"]["effectiveCgroupMode"] = ur.CGROUP_V1_MODE
        reads = {"pods": [pod("workers", containers=2, statuses=[oom("c0"), {"name": "c1", "state": {"running": {}}}])], "nodes": READS["seeded-a"]["nodes"], "events": [], "pdbs": [], "owners": []}
        [row] = ur.collect_symptoms(cluster, reads, ops_for("seeded-a"), SINCE)
        self.assertEqual(entries(row), {(None, ur.MEDIUM)})
        self.assertIn("neither 14 or 15", row["classifications"][0]["detail"])

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

    def test_env_values_never_reach_the_symptom_row(self):
        secret_pod = pod("legacy-flowcontrol-sync-abcde", phase="Failed", owner="Job", statuses=[{"name": "c0", "state": {"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": "2026-10-08T12:00:00Z"}}}])
        secret_pod["spec"]["containers"][0]["env"] = [{"name": "DB_PASSWORD", "value": "hunter2-not-for-the-report"}, {"name": "API", "value": "flowcontrol.apiserver.k8s.io/v1beta3"}]
        [row] = self.classify(pods=[secret_pod])
        self.assertEqual(entries(row), {(6, ur.MEDIUM)})
        self.assertEqual(row["api_markers"], ["flowcontrol", "v1beta"])
        self.assertNotIn("hunter2", json.dumps(row))
        self.assertNotIn("spec_text", row)

    def test_series_event_is_dated_by_its_last_observation(self):
        stale_first = {"type": "Warning", "reason": "FailedScheduling", "message": "0/2 nodes are available: 1 Insufficient cpu.", "involvedObject": {"kind": "Pod", "name": "x", "namespace": "apps"}, "eventTime": "2026-09-01T00:00:00Z", "series": {"count": 596, "lastObservedTime": "2026-10-08T12:00:00Z"}, "metadata": {"creationTimestamp": "2026-09-01T00:00:00Z"}}
        [row] = self.classify(events=[stale_first])
        self.assertEqual((row["count"], row["last_seen"]), (596, "2026-10-08T12:00:00Z"))
        self.assertEqual(symptoms_of("seeded-a", reads={**READS["seeded-a"], "events": [stale_first]}, window=datetime(2026, 10, 7, tzinfo=timezone.utc))[0]["category"], "pending")

    def test_long_running_pod_that_lost_readiness_in_the_window_is_kept(self):
        old = pod("api")
        old["status"]["startTime"] = "2026-08-01T00:00:00Z"
        old["status"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": "2026-10-08T05:00:00Z"}]
        [row] = self.classify(pods=[old])
        self.assertEqual((row["category"], row["reason"]), ("not-ready", "NotReady"))
        old["status"]["conditions"][0]["lastTransitionTime"] = "2026-08-02T00:00:00Z"
        self.assertEqual(self.classify(pods=[old]), [])

    def test_completed_init_container_is_not_a_failure_reason(self):
        p = pod("web")
        p["status"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": "2026-10-08T05:00:00Z"}]
        p["status"]["initContainerStatuses"] = [{"name": "init", "state": {"terminated": {"reason": "Completed", "exitCode": 0, "finishedAt": "2026-10-08T04:00:00Z"}}}]
        [row] = self.classify(pods=[p])
        self.assertEqual((row["reason"], row["containers"]), ("NotReady", []))

    def test_budget_selector_with_match_expressions(self):
        labels = {"app": "inference-server", "tier": "web"}
        self.assertTrue(ur._selector_matches({"matchExpressions": [{"key": "app", "operator": "In", "values": ["inference-server"]}]}, labels))
        self.assertFalse(ur._selector_matches({"matchExpressions": [{"key": "tier", "operator": "NotIn", "values": ["web"]}]}, labels))
        self.assertTrue(ur._selector_matches({"matchLabels": {"app": "inference-server"}, "matchExpressions": [{"key": "tier", "operator": "Exists"}]}, labels))
        self.assertFalse(ur._selector_matches({"matchLabels": {"app": "inference-server"}, "matchExpressions": [{"key": "tier", "operator": "DoesNotExist"}]}, labels))
        self.assertFalse(ur._selector_matches({}, labels))
        pdbs = copy.deepcopy(READS["seeded-a"]["pdbs"])
        pdbs[0]["spec"]["selector"] = {"matchExpressions": [{"key": "app", "operator": "In", "values": ["inference-server"]}]}
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "pdbs": pdbs}) if s["category"] == "pdb"]
        self.assertEqual(entries(rows[0]), {(1, ur.HIGH)})

    def test_entry_1_medium_when_the_drain_was_not_held(self):
        ops = copy.deepcopy(ops_for("seeded-a"))
        quick = next(o for o in ops if "pinned-inference-pool" in o["targetLink"])
        quick["endTime"] = "2026-10-08T04:30:00Z"
        rows = [s for s in symptoms_of("seeded-a", ops=ops) if s["category"] == "pdb"]
        self.assertEqual(entries(rows[0]), {(1, ur.MEDIUM)})


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

    def test_dead_pod_resolves_by_name_prefix(self):
        self.assertEqual(self.resolver.resolve("seeded-capacity", "Pod", "inference-server-778b78fdb8-zzzzz"), ("Deployment", "inference-server"))
        self.assertEqual(self.resolver.resolve("kubeagents-system", "Pod", "legacy-flowcontrol-tuner-29857980-k9k9k"), ("CronJob", "legacy-flowcontrol-tuner"))
        resolver = ur.Resolver([], [], READS["seeded-a"]["workloads"] + [{"kind": "StatefulSet", "metadata": {"name": "db", "namespace": "apps"}}])
        self.assertEqual(resolver.resolve("seeded-shapes", "Pod", "cni-shaped-agent-x1y2z"), ("DaemonSet", "cni-shaped-agent"))
        self.assertEqual(resolver.resolve("apps", "Pod", "db-3"), ("StatefulSet", "db"))
        self.assertEqual(resolver.resolve("apps", "Pod", "db-3x"), ("Pod", "db-3x"))
        reads = {**READS["seeded-a"], "events": [event("FailedAttachVolume", "AttachVolume.Attach failed for volume pv-1", name="inference-server-778b78fdb8-zzzzz", namespace="seeded-capacity")]}
        [row] = [s for s in symptoms_of("seeded-a", reads=reads) if s["category"] == "event"]
        self.assertEqual(row["object"], INFERENCE)

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

class ShapeTest(unittest.TestCase):
    """Each detector on a captured object from seeded-a's planted shapes,
    plus the cases the captures do not carry."""
    def setUp(self):
        self.shapes = ur.collect_shapes(cluster_doc("seeded-a"), READS["seeded-a"])
        self.by_object = {(s["object"], s["entry"]): s for s in self.shapes}

    def shape(self, obj, entry):
        self.assertIn((obj, entry), self.by_object, sorted(self.by_object))
        return self.by_object[(obj, entry)]

    def test_seeded_a_planted_shapes(self):
        self.assertEqual(sorted(self.by_object), [
            ("PersistentVolume/intree-pd", 19),
            ("seeded-capacity/PodDisruptionBudget/inference-server", 1),
            ("seeded-shapes/CronJob/cuda-pinned-trainer", 18),
            ("seeded-shapes/DaemonSet/cni-shaped-agent", 17),
            ("seeded-shapes/DaemonSet/node-runtime-probe", 13),
            ("seeded-shapes/Deployment/arch-pinned-worker", 12),
            ("seeded-shapes/Deployment/cache-on-emptydir", 4),
            ("seeded-shapes/Deployment/cgroup-blind-jvm", 14),
            ("seeded-shapes/Deployment/legacy-registry-pull", 20),
        ])

    def test_entry_14_cgroup_blind_jvm_on_a_v2_pool(self):
        shape = self.shape("seeded-shapes/Deployment/cgroup-blind-jvm", 14)
        self.assertEqual(shape["confidence"], ur.MEDIUM)
        self.assertEqual(shape["evidence"], "image docker.io/library/eclipse-temurin:8u302-b08-jre (JDK 8u302 < 8u372) on pool default-pool on cgroup v2")

    def test_entry_14_runtime_table(self):
        cases = {
            "eclipse-temurin:8u302-b08-jre": "JDK 8u302 < 8u372",
            "docker.io/library/eclipse-temurin:8u372-b07-jre": None,
            "openjdk:11.0.15-jre": "JDK 11.0.15 < 11.0.16",
            "openjdk:11.0.16-jdk": None,
            "adoptopenjdk:8u292-b10-jre-hotspot": "JDK 8u292 < 8u372",
            "eclipse-temurin:17-jre": None,
            "eclipse-temurin:8-jre": None,
            "openjdk:13.0.2": "JDK 13 predates cgroup v2 support",
            "mcr.microsoft.com/dotnet/runtime:2.1": ".NET 2.1 < 3.1",
            "mcr.microsoft.com/dotnet/aspnet:3.0-alpine": ".NET 3.0 < 3.1",
            "mcr.microsoft.com/dotnet/runtime:3.1": None,
            "mcr.microsoft.com/dotnet/sdk:8.0": None,
            "busybox:1.36": None,
            "registry.example.com:5000/eclipse-temurin:8u302": "JDK 8u302 < 8u372",
        }
        for image, expected in cases.items():
            with self.subTest(image=image):
                self.assertEqual(ur.cgroup_v1_runtime(image), expected)

    def test_entry_14_needs_an_exposed_pool(self):
        cluster = cluster_doc("seeded-a")
        jvm = pod("jvm", images=["eclipse-temurin:8u302-jre"])
        obj = ur._object_of("apps", "Pod", "jvm")
        self.assertEqual(len(ur.runtime_shapes(obj, jvm["spec"], ["default-pool"], cluster)), 1)
        for pool in cluster["nodePools"]:
            pool["config"]["effectiveCgroupMode"] = ur.CGROUP_V1_MODE
            pool["version"] = "1.31.14-gke.2704000"
        [shape] = ur.runtime_shapes(obj, jvm["spec"], ["default-pool"], cluster)
        self.assertIn("pool default-pool on cgroup v1 at 1.31.14-gke.2704000, migrated to v2 at 1.33", shape["evidence"])
        for pool in cluster["nodePools"]:
            pool["version"] = "1.34.12-gke.1011000"
        self.assertEqual(ur.runtime_shapes(obj, jvm["spec"], ["default-pool"], cluster), [])
        # No pod placed yet: every pool is a candidate.
        cluster["nodePools"][1]["config"]["effectiveCgroupMode"] = ur.CGROUP_V2_MODE
        [shape] = ur.runtime_shapes(obj, jvm["spec"], [], cluster)
        self.assertIn("pool idle-batch-pool on cgroup v2", shape["evidence"])

    def test_managed_agents_are_counted_not_reported(self):
        def ds(namespace, name, host_network=True, socket=False):
            volumes = [{"name": "sock", "hostPath": {"path": "/run/containerd/containerd.sock"}}] if socket else []
            return {"kind": "DaemonSet", "metadata": {"name": name, "namespace": namespace}, "spec": {"template": {"metadata": {"labels": {}}, "spec": {"hostNetwork": host_network, "containers": [{"name": "c", "image": "busybox:1.36"}], "volumes": volumes}}}}
        workloads = READS["seeded-a"]["workloads"] + [ds("kube-system", "anetd"), ds("kube-system", "efficiency-daemon", socket=True), ds("gke-gmp-system", "collector"), ds("config-management-system", "otel-agent"), ds("gke-managed-cim", "cim-agent", host_network=False, socket=True)]
        reads = {**READS["seeded-a"], "workloads": workloads}
        shapes, agents = ur.collect_risks(cluster_doc("seeded-a"), reads)
        self.assertEqual(agents, 5)
        self.assertEqual({s["object"] for s in shapes if s["entry"] in (13, 17)}, {"seeded-shapes/DaemonSet/cni-shaped-agent", "seeded-shapes/DaemonSet/node-runtime-probe"})
        guards = ur.risk_guards_for(SEEDED, shapes, "2026-10-08T18:00:00Z")
        self.assertFalse(any(g["namespace"] if "namespace" in g else g["object"].startswith(("kube-system/", "gke-", "config-management-")) for g in guards))
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: home, ur.STORE_HOME_ENV: home}), mock.patch.dict(READS, {"seeded-a": reads}), redirect_stderr(io.StringIO()):
            result = ur.collect(args(), run=FakeFleet(), now=NOW)
        block = ur.render_report(result).split(f"### {SEEDED}")[1]
        self.assertIn(ur.MANAGED_AGENTS_LINE.format(count=5), block)
        self.assertNotIn("kube-system/DaemonSet/anetd", block)

    def test_entry_12_deprecated_label_selector(self):
        shape = self.shape("seeded-shapes/Deployment/arch-pinned-worker", 12)
        self.assertEqual((shape["confidence"], shape["evidence"]), (ur.HIGH, "selector beta.kubernetes.io/arch=amd64"))

    def test_entry_20_retired_registry(self):
        shape = self.shape("seeded-shapes/Deployment/legacy-registry-pull", 20)
        self.assertEqual((shape["confidence"], shape["evidence"], shape["detail"]), (ur.HIGH, "image k8s.gcr.io/pause:3.9", "image host k8s.gcr.io"))

    def test_entry_4_stateful_emptydir(self):
        shape = self.shape("seeded-shapes/Deployment/cache-on-emptydir", 4)
        self.assertEqual((shape["confidence"], shape["evidence"]), (ur.MEDIUM, "emptyDir volume `queue`"))

    def test_entry_18_cuda_pin_on_suspended_cronjob(self):
        shape = self.shape("seeded-shapes/CronJob/cuda-pinned-trainer", 18)
        self.assertEqual(shape["confidence"], ur.MEDIUM)
        self.assertEqual(shape["evidence"], "nvidia.com/gpu requested with image docker.io/nvidia/cuda:12.2.0-base-ubuntu22.04")

    def test_entries_17_and_13_daemonsets(self):
        self.assertEqual(self.shape("seeded-shapes/DaemonSet/cni-shaped-agent", 17)["evidence"], "DaemonSet on hostNetwork")
        self.assertEqual(self.shape("seeded-shapes/DaemonSet/node-runtime-probe", 13)["evidence"], "DaemonSet mounts /run/containerd/containerd.sock (volume `sock`)")
        self.assertNotIn(("seeded-shapes/DaemonSet/cni-shaped-agent", 13), self.by_object)

    def test_entry_1_budget(self):
        shape = self.shape("seeded-capacity/PodDisruptionBudget/inference-server", 1)
        self.assertEqual(shape["confidence"], ur.HIGH)
        self.assertTrue(shape["evidence"].startswith("disruptionsAllowed=0"))

    def test_entry_1_single_replica_behind_a_budget(self):
        pdb = {"kind": "PodDisruptionBudget", "metadata": {"name": "payments", "namespace": "seeded-debug"}, "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app": "payments-api"}}}, "status": {"disruptionsAllowed": 1}}
        shapes = ur.budget_shapes([pdb], READS["seeded-a"]["workloads"])
        self.assertEqual([(s["object"], s["entry"], s["confidence"]) for s in shapes], [("seeded-debug/Deployment/payments-api", 1, ur.HIGH)])
        self.assertIn("one replica behind budget payments", shapes[0]["evidence"])

    def test_entry_19_in_tree_volume_and_class(self):
        pv = self.shape("PersistentVolume/intree-pd", 19)
        self.assertEqual(pv["confidence"], ur.MEDIUM)
        self.assertIn("PD CSI driver add-on enabled", pv["evidence"])
        # GKE's own `standard` class serves gce-pd through CSI migration while the add-on is on.
        self.assertNotIn(("StorageClass/standard", 19), self.by_object)
        cluster = cluster_doc("seeded-a")
        cluster["addonsConfig"]["gcePersistentDiskCsiDriverConfig"] = {"enabled": False}
        high = ur.storage_shapes(READS["seeded-a"]["storage"], cluster)
        self.assertEqual({(s["object"], s["confidence"]) for s in high}, {("PersistentVolume/intree-pd", ur.HIGH), ("StorageClass/standard", ur.HIGH)})

    def test_entry_7_fail_closed_webhook_without_endpoints(self):
        config = {"kind": "ValidatingWebhookConfiguration", "metadata": {"name": "gate"}, "webhooks": [{"name": "gate.example.io", "failurePolicy": "Fail", "clientConfig": {"service": {"namespace": "apps", "name": "gate"}}}]}
        [shape] = ur.webhook_shapes([config], READS["seeded-a"]["endpointslices"])
        self.assertEqual((shape["object"], shape["entry"], shape["confidence"]), ("ValidatingWebhookConfiguration/gate", 7, ur.HIGH))
        self.assertIn("timeout 10s, no namespaceSelector; Service apps/gate has no ready endpoint", shape["evidence"])
        # A short timeout or a namespaceSelector is the lesser shape.
        config["webhooks"][0]["timeoutSeconds"] = 3
        [shape] = ur.webhook_shapes([config], READS["seeded-a"]["endpointslices"])
        self.assertEqual(shape["confidence"], ur.MEDIUM)
        config["webhooks"][0]["timeoutSeconds"] = 30
        config["webhooks"][0]["namespaceSelector"] = {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "NotIn", "values": ["kube-system"]}]}
        [shape] = ur.webhook_shapes([config], READS["seeded-a"]["endpointslices"])
        self.assertEqual(shape["confidence"], ur.MEDIUM)
        # The captured gmp-operator webhook is fail-closed with a ready endpoint: no shape.
        self.assertEqual(ur.webhook_shapes(READS["seeded-a"]["webhooks"], READS["seeded-a"]["endpointslices"]), [])
        # Without the endpoint read the check cannot run.
        self.assertEqual(ur.webhook_shapes([config], None), [])

    def test_shapes_from_a_bare_pod_and_affinity_and_local_ssd(self):
        bare = pod("edge", images=["gcr.io/google-containers/pause:3.2"])
        bare["spec"]["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "failure-domain.beta.kubernetes.io/zone", "operator": "In", "values": ["us-central1-a"]}]}]}}}
        bare["spec"]["volumes"] = [{"name": "scratch", "hostPath": {"path": "/mnt/disks/ssd0"}}]
        shapes = ur.collect_shapes(cluster_doc("seeded-a"), {"pods": [bare], "owners": [], "workloads": [], "pdbs": [], "storage": [], "webhooks": [], "endpointslices": []})
        self.assertEqual([(s["object"], s["entry"]) for s in shapes], [("apps/Pod/edge", 4), ("apps/Pod/edge", 12), ("apps/Pod/edge", 20)])
        self.assertEqual(next(s for s in shapes if s["entry"] == 12)["evidence"], "selector affinity on failure-domain.beta.kubernetes.io/zone")

    def test_stateful_volume_names_are_anchored(self):
        for name, expected in (("queue", True), ("alertmanager-data", True), ("nginx-cache", False), ("cache", False), ("wallet", False), ("pg-wal", True), ("dbx", False), ("db", True), ("swaldo", False)):
            with self.subTest(name=name):
                self.assertEqual(bool(ur.STATEFUL_VOLUME_NAME_RE.search(name)), expected)

    def test_gpu_without_a_cuda_pin_is_not_a_shape(self):
        plain = pod("gpu-job")
        plain["spec"]["containers"][0]["resources"] = {"limits": {"nvidia.com/gpu": "1"}}
        self.assertEqual(ur.shapes_in_spec(ur._object_of("apps", "Pod", "gpu-job"), plain["spec"]), [])
        plain["spec"]["containers"][0]["env"] = [{"name": "CUDA_VERSION", "value": "12.2"}]
        [shape] = ur.shapes_in_spec(ur._object_of("apps", "Pod", "gpu-job"), plain["spec"])
        self.assertEqual(shape["evidence"], "nvidia.com/gpu requested with env CUDA_VERSION=12.2")

    def test_risk_guards_carry_the_risk_kind(self):
        guards = ur.risk_guards_for(SEEDED, self.shapes, "2026-10-08T18:00:00Z")
        self.assertEqual(len(guards), 9)
        self.assertTrue(all(g["reads"] for g in guards))
        self.assertIn("nodes", next(g for g in guards if g["entry"] == 14)["reads"])
        self.assertTrue(all(g["kind"] == ur.GUARD_KIND_RISK and g["id"].startswith(f"{SEEDED}#risk#") for g in guards))
        failure = ur.guards_for(SEEDED, symptoms_of("seeded-a"), "2026-10-08T18:00:00Z")
        self.assertTrue(all(g["kind"] == ur.GUARD_KIND_FAILURE for g in failure))
        # The same object and entry can be both: the budget broke the last upgrade and is still there.
        ids = {g["id"] for g in guards} | {g["id"] for g in failure}
        self.assertIn(ur.guard_id(SEEDED, 1, "seeded-capacity/PodDisruptionBudget/inference-server", ur.GUARD_KIND_RISK), ids)
        self.assertIn(ur.guard_id(SEEDED, 1, "seeded-capacity/PodDisruptionBudget/inference-server", ur.GUARD_KIND_FAILURE), ids)


class NextUpgradeTest(unittest.TestCase):
    def test_daily_window_and_channel_target(self):
        nu = ur.next_upgrade(cluster_doc("seeded-a"), SERVER_CONFIG, NOW)
        self.assertEqual((nu["channel"], nu["target"], nu["current"], nu["below_target"]), ("REGULAR", "1.35.8-gke.1225000", "1.35.8-gke.1380001", False))
        self.assertEqual((nu["window"], nu["next_opens"], nu["exclusions"]), ("daily at 03:00 UTC for 4h", "2026-10-09T03:00:00Z", []))

    def test_recurring_window_and_active_exclusion(self):
        nu = ur.next_upgrade(cluster_doc("gemma-gpu-upgraded"), SERVER_CONFIG, NOW)
        self.assertEqual((nu["channel"], nu["target"], nu["below_target"]), ("EXTENDED", "1.36.4-gke.1247000", True))
        self.assertTrue(nu["window"].startswith("DAILY at"))
        self.assertGreater(nu["next_opens"], "2026-10-08T18:00:00Z")
        self.assertEqual(nu["exclusions"], [{"name": "hold-gpu-minor", "scope": "NO_MINOR_UPGRADES", "start": "2026-09-24T17:41:17Z", "end": "2026-10-21T00:00:00Z", "active": True}])

    def test_weekly_rule_no_channel_and_no_window(self):
        cluster = cluster_doc("seeded-a")
        cluster["releaseChannel"] = {}
        cluster["maintenancePolicy"] = {"window": {"recurringWindow": {"recurrence": "FREQ=WEEKLY;BYDAY=SA,SU", "window": {"startTime": "2024-01-06T09:00:00Z", "endTime": "2024-01-06T17:00:00Z"}}}}
        nu = ur.next_upgrade(cluster, SERVER_CONFIG, NOW)  # NOW is a Thursday
        self.assertEqual(nu["target"], SERVER_CONFIG["defaultClusterVersion"])
        self.assertEqual((nu["window"], nu["next_opens"]), ("WEEKLY on SA,SU at 09:00 UTC for 8h", "2026-10-10T09:00:00Z"))
        # RFC 5545: WEEKLY without BYDAY repeats on DTSTART's weekday (a Saturday), not daily.
        cluster["maintenancePolicy"]["window"]["recurringWindow"]["recurrence"] = "FREQ=WEEKLY"
        nu = ur.next_upgrade(cluster, SERVER_CONFIG, NOW)
        self.assertEqual(nu["next_opens"], "2026-10-10T09:00:00Z")
        cluster["maintenancePolicy"] = {}
        nu = ur.next_upgrade(cluster, None, NOW)
        self.assertEqual((nu["target"], nu["below_target"], nu["window"], nu["next_opens"]), (None, None, ur.NO_WINDOW_TEXT, None))

    def test_server_config_is_fetched_once_per_location(self):
        fleet = FakeFleet()
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: home, ur.STORE_HOME_ENV: home}), redirect_stderr(io.StringIO()):
            ur.collect(args(), run=fleet, now=NOW)
        self.assertEqual(sum(1 for c in fleet.calls if "get-server-config" in c), 1)


class LedgerAndGuardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: self.tmp.name, ur.STORE_HOME_ENV: self.tmp.name, **NO_PROJECT_ENV})
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
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        self.assertEqual(ledger["clusters"][SEEDED]["control_plane"], "1.35.8-gke.1380001")
        self.assertEqual(ledger["clusters"][SEEDED]["node_pools"]["pinned-inference-pool"], "1.35.8-gke.1380001")
        self.assertEqual(ledger["clusters"][SEEDED]["last_run"], "2026-10-08T18:00:00Z")
        second, _ = self.collect()
        self.assertEqual(second["reviews"], [])
        self.assertEqual([u["cluster"] for u in second["unchanged"]], [GEMMA, SEEDED])

    def test_dry_run_writes_nothing(self):
        result, _ = self.collect(dry_run=True, output=str(self.home / "out.json"))
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.home / ur.LEDGER_FILENAME).exists())
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
        self.assertNotIn("spec_text", json.dumps(doc))
        self.assertTrue(seeded["mitigations"])
        self.assertTrue(all(m["entry"] in ur.MITIGATIONS for m in seeded["mitigations"]))
        self.assertEqual({g["entry"] for g in seeded["guards"] if g["kind"] == ur.GUARD_KIND_FAILURE}, {1, 2, 12, 14})
        self.assertEqual({g["entry"] for g in seeded["guards"] if g["kind"] == ur.GUARD_KIND_RISK}, {1, 4, 12, 13, 14, 17, 18, 19, 20})
        self.assertEqual(doc["guards"], result["guards"])
        self.assertEqual(seeded["baseline"]["shapes"], 9)
        self.assertEqual(seeded["next_upgrade"]["target"], "1.35.8-gke.1225000")
        sections = doc["sections"]
        self.assertEqual([(i["cluster"], i["object"], i["entries"]) for i in sections["errors"]], [(SEEDED, INFERENCE, "2, 12"), (SEEDED, "seeded-capacity/PodDisruptionBudget/inference-server", "1")])
        self.assertEqual([i["object"] for i in sections["warnings"]], [KUBE_DNS, TUNER, "Node/gke-seeded-a-default-pool-62ac8ee0-d595", PAYMENTS, "seeded-stall/Deployment/inventory-api"])
        self.assertEqual([(c["cluster"], c["incidents"]) for c in sections["info"]["clean"]], [(GEMMA, 2), (SEEDED, 5)])
        self.assertEqual((sections["info"]["unchanged"], sections["info"]["failed_reads"]), ([], []))

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
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        self.assertIsNone(ledger["clusters"][GEMMA]["last_run"])
        self.assertEqual(ledger["clusters"][SEEDED]["last_run"], "2026-10-08T18:00:00Z")
        # Next run: still new, so it is retried rather than read as unchanged.
        again, _ = self.collect(FakeFleet())
        self.assertEqual([(r["cluster"], r["what_happened"]["status"]) for r in again["reviews"]], [(GEMMA, "new")])

    def test_owners_read_failure_keys_by_the_intermediate(self):
        result, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "owners")]))
        seeded = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertFalse(seeded["reviewed"])  # owners is a core read: a partial review
        self.assertEqual(seeded["partial"], ["owners"])
        self.assertIn("seeded-debug/ReplicaSet/payments-api-79b77b8c67", {g["object"] for g in seeded["guards"]})

    def test_pods_failing_after_a_first_run_keeps_guards_and_last_run(self):
        first, _ = self.collect()
        before = {g["id"] for g in first["guards"] if g["cluster"] == SEEDED}
        later = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        second, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods")]), now=later, cluster=[SEEDED])
        after = {g["id"] for g in second["guards"] if g["cluster"] == SEEDED}
        self.assertEqual(after, before)
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        self.assertEqual(ledger["clusters"][SEEDED]["last_run"], "2026-10-08T18:00:00Z")
        self.assertEqual(ledger["clusters"][SEEDED]["partial_read"], "2026-10-15T18:00:00Z")
        review = second["reviews"][0]
        self.assertFalse(review["reviewed"])
        self.assertEqual(review["partial"], ["pods"])
        self.assertIn(ur.PARTIAL_READ_TEXT.format(failed="pods"), ur.render_report(second))
        # The next ordinary run re-selects it and, reading in full, reviews it.
        third, _ = self.collect(now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        self.assertIn(SEEDED, [r["cluster"] for r in third["reviews"]])
        self.assertNotIn("partial_read", ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED])
        # Shape reads failing drop only the guards those reads feed, and only if they answered: none here.
        fourth, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "storage"), ("seeded-a", "workloads")]), now=datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc), cluster=[SEEDED])
        self.assertEqual({g["id"] for g in fourth["guards"] if g["cluster"] == SEEDED}, before)
        self.assertTrue(fourth["reviews"][0]["reviewed"])

    def test_corrupt_or_foreign_state_is_set_aside_and_the_run_refused(self):
        (self.home / ur.LEDGER_FILENAME).write_text(json.dumps({"version": 99, "clusters": {"keep": {}}}))
        with self.assertRaises(ur.StateUnreadable):
            self.collect()
        aside = next(p for p in self.home.glob(ur.LEDGER_FILENAME + ".unreadable-*"))
        self.assertEqual(json.loads(aside.read_text())["version"], 99)
        self.assertFalse((self.home / ur.LEDGER_FILENAME).exists())
        self.assertFalse((self.home / ur.GUARDS_FILENAME).exists())
        # Through main: one line, exit 2, nothing written; a missing ledger is a first run.
        (self.home / ur.GUARDS_FILENAME).write_text("{not json")
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                rc = ur.main(["--project", PROJECT, "--no-report"])
        self.assertEqual(rc, ur.EXIT_USAGE)
        self.assertIn("crash record, not a re-baseline", err.getvalue())
        self.assertFalse((self.home / ur.LEDGER_FILENAME).exists())
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = ur.main(["--project", PROJECT, "--no-report"])
        self.assertEqual(rc, 0)
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["version"], ur.LEDGER_VERSION)

    def test_lock_makes_a_second_run_exit_quietly(self):
        lock_path = self.home / ur.LOCK_FILENAME
        held = ur.acquire_lock(lock_path)
        self.assertIsNotNone(held)
        try:
            with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
                with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                    rc = ur.main(["--project", PROJECT])
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue().strip(), ur.lock_held_line(lock_path))
            self.assertIn("another retrospective run holds", out.getvalue())
            self.assertFalse((self.home / ur.LEDGER_FILENAME).exists())
            self.assertFalse((self.home / ur.REPORTS_SUBDIR).exists())
            # A dry run takes no lock and still prints its report.
            with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
                with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                    rc = ur.main(["--project", PROJECT, "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn(ur.SECTION_ERRORS, out.getvalue())
        finally:
            held.close()
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ur.main(["--project", PROJECT, "--no-report"]), 0)
        self.assertTrue((self.home / ur.LEDGER_FILENAME).exists())

    def test_in_flight_operation_holds_the_cluster_back(self):
        ops = copy.deepcopy(OPERATIONS)
        running = next(o for o in ops if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/pinned-inference-pool" in o["targetLink"])
        running["status"], running["endTime"] = "RUNNING", None
        result, fleet = self.collect(FakeFleet(operations=ops))
        self.assertEqual([r["cluster"] for r in result["reviews"]], [GEMMA])
        self.assertEqual([(u["cluster"], u["operation"]["type"], u["operation"]["target"]) for u in result["upgrading"]], [(SEEDED, "UPGRADE_NODES", "pinned-inference-pool")])
        self.assertFalse(any(c[0] == "kubectl" and "seeded-a" in (c[-1] if False else " ".join(c)) for c in fleet.calls))
        self.assertFalse(any("seeded-a" in c for c in fleet.calls if "get-credentials" in c))
        self.assertEqual({g["cluster"] for g in result["guards"]}, {GEMMA})
        self.assertNotIn(SEEDED, ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"])
        report = ur.render_report(result)
        self.assertIn(f"{ur.INFO_UPGRADING}\n\n- {SEEDED}: upgrading now (UPGRADE_NODES pinned-inference-pool since 2026-10-08T04:20:35Z); reviewed on the next run", report)
        # Once it ends inside the window the cluster is reviewed as new.
        running["status"], running["endTime"] = "DONE", "2026-10-08T05:24:10Z"
        again, _ = self.collect(FakeFleet(operations=ops))
        self.assertIn(SEEDED, [r["cluster"] for r in again["reviews"]])

    def test_only_terminal_operations_ending_in_the_window_count(self):
        ops = copy.deepcopy(ops_for("seeded-a"))
        ops[0]["endTime"] = "2026-09-01T00:00:00Z"  # the master upgrade ended before the window
        ops[0]["startTime"] = "2026-08-31T23:00:00Z"
        selected, _, upgrading = ur.select_clusters([cluster_doc("seeded-a")], ur.empty_ledger(), ops, since=SINCE, forced=set())
        self.assertEqual(upgrading, [])
        self.assertEqual([ur.operation_summary(o)["target"] for o in selected[0].operations], ["default-pool", "idle-batch-pool", "pinned-inference-pool"])

    def test_deleted_cluster_drops_its_ledger_entry_and_guards(self):
        self.collect()
        result, _ = self.collect(FakeFleet(clusters=[cluster_doc("seeded-a")]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(result["removed_clusters"], [GEMMA])
        self.assertNotIn(GEMMA, {g["cluster"] for g in result["guards"]})
        self.assertNotIn(GEMMA, ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"])
        self.assertIn(f"{ur.INFO_REMOVED}\n\n- {GEMMA}: no longer listed", ur.render_report(result))
        self.assertEqual([i for i in result["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD], [])
        # A listing that failed drops nothing.
        self.collect()
        kept, _ = self.collect(FakeFleet(list_rc=1), now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(kept["removed_clusters"], [])
        self.assertIn(GEMMA, {g["cluster"] for g in kept["guards"]})

    def test_malformed_pod_is_a_failed_read_not_a_crash(self):
        reads = {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + ["garbage", {"metadata": None, "spec": 3}]}
        with mock.patch.dict(READS, {"seeded-a": reads}):
            result, _ = self.collect()
        seeded = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertFalse(seeded["reviewed"])
        self.assertTrue(any(e.startswith("review failed:") for e in seeded["read_errors"]))
        self.assertTrue(any(r["cluster"] == GEMMA and r["reviewed"] for r in result["reviews"]))
        self.assertIn(f"- {SEEDED}: review failed:", ur.render_report(result))

    def test_recheck_clears_a_guard_whose_finding_is_gone(self):
        first, _ = self.collect()
        seeded_guards = [g for g in first["guards"] if g["cluster"] == SEEDED]
        self.assertTrue(seeded_guards)
        # Unchanged, still broken: every guard refreshed, none cleared, no stale Warning.
        later = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        second, fleet = self.collect(now=later)
        self.assertEqual(second["reviews"], [])
        [recheck] = [r for r in second["rechecks"] if r["cluster"] == SEEDED]
        self.assertEqual((recheck["guards"], recheck["cleared"]), (len(seeded_guards), []))
        self.assertTrue(all(g["last_seen"] == "2026-10-15T18:00:00Z" for g in second["guards"] if g["cluster"] == SEEDED))
        self.assertEqual([i for i in second["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD], [])
        self.assertIn(f"{ur.INFO_RECHECKED}\n\n- {GEMMA}: re-checked for", ur.render_report(second))
        read_kinds = {c[2] for c in fleet.calls if c[0] == "kubectl"}
        self.assertNotIn("events", read_kinds)
        # The operator fixes payments-api and the registry image: those guards clear, the rest stay.
        fixed_pods = [p for p in READS["seeded-a"]["pods"] if "payments-api" not in p["metadata"]["name"]]
        fixed_workloads = copy.deepcopy(READS["seeded-a"]["workloads"])
        for w in fixed_workloads:
            if w["metadata"]["name"] == "legacy-registry-pull":
                w["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.k8s.io/pause:3.9"
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "pods": fixed_pods, "workloads": fixed_workloads}}):
            third, _ = self.collect(now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in third["rechecks"] if r["cluster"] == SEEDED]
        self.assertEqual(sorted(recheck["cleared"]), sorted([ur.guard_id(SEEDED, 14, PAYMENTS), ur.guard_id(SEEDED, 20, "seeded-shapes/Deployment/legacy-registry-pull", ur.GUARD_KIND_RISK)]))
        self.assertIn(f"- {SEEDED}: re-checked for {len(seeded_guards)} guard(s): 2 cleared", ur.render_report(third))
        remaining = {g["id"] for g in third["guards"] if g["cluster"] == SEEDED}
        self.assertEqual(len(remaining), len(seeded_guards) - 2)
        self.assertIn(ur.guard_id(SEEDED, 1, "seeded-capacity/PodDisruptionBudget/inference-server"), remaining)
        # A re-check whose read fails clears nothing and says so.
        fourth, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods")]), now=datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in fourth["rechecks"] if r["cluster"] == SEEDED]
        self.assertEqual(recheck["cleared"], [])
        self.assertTrue(recheck["errors"])

    def test_event_only_guard_is_not_recheckable(self):
        reads = {**READS["seeded-a"], "events": READS["seeded-a"]["events"] + [event("FailedAttachVolume", "AttachVolume.Attach failed for volume pv-1", name="inference-server-778b78fdb8-zzzzz", namespace="seeded-capacity")]}
        with mock.patch.dict(READS, {"seeded-a": reads}):
            first, _ = self.collect()
        gid = ur.guard_id(SEEDED, 19, INFERENCE)
        guard = next(g for g in first["guards"] if g["id"] == gid)
        self.assertEqual(guard["source"], ur.GUARD_SOURCE_EVENT)
        # A guard a pod also showed is not event-only.
        self.assertEqual(next(g for g in first["guards"] if g["id"] == ur.guard_id(SEEDED, 2, INFERENCE))["source"], ur.CATEGORY_PENDING)
        # Unchanged, no events read: the event-only guard is neither cleared nor refreshed.
        second, _ = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in second["rechecks"] if r["cluster"] == SEEDED]
        self.assertEqual((recheck["cleared"], recheck["not_recheckable"]), ([], [gid]))
        self.assertIn(gid, {g["id"] for g in second["guards"]})
        self.assertIn(f"- {SEEDED}: re-checked for {recheck['guards']} guard(s): 0 cleared; {ur.RECHECK_NOT_RECHECKABLE_TEXT.format(count=1)}", ur.render_report(second))
        # The next full review of the cluster, with the event gone, clears it.
        third, _ = self.collect(now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc), cluster=[SEEDED])
        self.assertNotIn(gid, {g["id"] for g in third["guards"]})

    def test_stale_guard_on_a_partially_read_cluster_says_so(self):
        self.collect()
        later = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        second, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods")]), now=later, cluster=[SEEDED])
        stale = [i for i in second["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD and i["cluster"] == SEEDED]
        self.assertTrue(stale)
        self.assertTrue(all(i["partial"] == ["pods"] for i in stale))
        report = ur.render_report(second)
        self.assertEqual(report.count(ur.STALE_PARTIAL_TEXT.format(failed="pods")), len(stale))
        # gemma was neither reviewed nor re-checked under --cluster: its guards keep the other wording.
        other = [i for i in second["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD and i["cluster"] == GEMMA]
        self.assertEqual(report.count("cluster not reviewed this run"), len(other))
        self.assertTrue(other)

    def test_all_core_reads_failing_drops_no_guard_and_renders_no_block(self):
        first, _ = self.collect()
        before = {g["id"] for g in first["guards"] if g["cluster"] == SEEDED}
        self.assertTrue(any("#risk#19#" in gid for gid in before))
        core_fail = [("seeded-a", r) for r in ur.CORE_READS]
        second, _ = self.collect(FakeFleet(kubectl_fail=core_fail), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc), cluster=[SEEDED])
        self.assertEqual({g["id"] for g in second["guards"] if g["cluster"] == SEEDED}, before)
        review = second["reviews"][0]
        self.assertEqual(review["answered"], [])
        self.assertEqual(second["sections"]["info"]["clean"], [])
        self.assertNotIn(f"### {SEEDED} —", ur.render_report(second).split(ur.SECTION_INFO)[1])

    def test_recheck_for_risk_guards_reads_nodes(self):
        self.collect()
        second, fleet = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertTrue(second["rechecks"])
        self.assertIn("nodes", {c[2] for c in fleet.calls if c[0] == "kubectl"})

    def test_not_ready_node_after_a_pool_operation_is_an_error(self):
        nodes = copy.deepcopy(READS["seeded-a"]["nodes"])
        broken = next(n for n in nodes if n["metadata"]["labels"]["cloud.google.com/gke-nodepool"] == "idle-batch-pool")
        for cond in broken["status"]["conditions"]:
            if cond["type"] == "Ready":
                cond["status"] = "False"
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "nodes": nodes}}):
            result, _ = self.collect()
        node_incident = next(i for i in result["sections"]["errors"] if i["object"].startswith("Node/"))
        self.assertEqual((node_incident["entries"], node_incident["system"]), ("17", True))
        self.assertIn(f"### 17 — {SEEDED} — `Node/{broken['metadata']['name']}` (system)", ur.render_report(result).split(ur.SECTION_WARNINGS)[0])

    def test_merge_carries_a_guard_source_forward(self):
        gid = ur.guard_id(SEEDED, 19, INFERENCE)
        seen = "2026-10-08T18:00:00Z"
        old = {"id": gid, "kind": "failure", "cluster": SEEDED, "entry": 19, "object": INFERENCE, "source": "event", "reads": list(ur.FAILURE_GUARD_READS), "title": "t", "confidence": "medium", "evidence": "e", "first_seen": seen, "last_seen": seen}
        fresh = {**old, "source": "pending", "evidence": "pod"}
        merged = ur.merge_guards({"version": 1, "guards": [old]}, [fresh], {SEEDED}, "2026-10-15T18:00:00Z")
        self.assertEqual(merged["guards"][0]["source"], "pending")

    def test_checked_list_omits_checks_whose_reads_failed(self):
        clean_reads = {**READS["seeded-a"], "pods": [], "events": [], "pdbs": [], "workloads": [], "storage": []}
        with mock.patch.dict(READS, {"seeded-a": clean_reads}):
            result, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "storage"), ("seeded-a", "webhooks")]), cluster=[SEEDED])
        block = ur.render_report(result).split(f"### {SEEDED}")[1]
        risks = next(line for line in block.splitlines() if line.startswith(ur.PART_RISKS))
        checked, _, skipped = risks.partition(ur.NOT_CHECKED_TEXT)
        self.assertNotIn("(19)", checked)
        self.assertNotIn("(7)", checked)
        self.assertIn("(19)", skipped)
        self.assertIn("(7)", skipped)
        self.assertIn("(12)", checked)

    def test_stale_guard_wording_for_rechecked_clusters(self):
        reads = {**READS["seeded-a"], "events": READS["seeded-a"]["events"] + [event("FailedAttachVolume", "AttachVolume.Attach failed for volume pv-1", name="inference-server-778b78fdb8-zzzzz", namespace="seeded-capacity")]}
        with mock.patch.dict(READS, {"seeded-a": reads}):
            self.collect()
        later = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        second, _ = self.collect(now=later)
        report = ur.render_report(second)
        self.assertIn(ur.STALE_EVENT_ONLY_TEXT, report)
        self.assertNotIn("cluster not reviewed this run", report)
        third, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods")]), now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        report = ur.render_report(third)
        self.assertIn("cluster re-checked this run but the read failed (pods:", report)
        self.assertNotIn("cluster not reviewed this run", report)

    def test_dry_run_never_moves_a_state_file(self):
        (self.home / ur.LEDGER_FILENAME).write_text("{not json")
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                rc = ur.main(["--project", PROJECT, "--dry-run"])
        self.assertEqual(rc, ur.EXIT_USAGE)
        self.assertEqual((self.home / ur.LEDGER_FILENAME).read_text(), "{not json")
        self.assertEqual(list(self.home.glob("*.unreadable-*")), [])
        self.assertIn("a dry run moves nothing", err.getvalue())

    def test_store_defaults_live_under_the_store_home(self):
        result, _ = self.collect()
        self.assertEqual(Path(result["ledger_path"]), self.home / ur.LEDGER_FILENAME)
        self.assertEqual(Path(result["guards_path"]), self.home / ur.GUARDS_FILENAME)
        self.assertEqual(ur.DEFAULT_STORE_DIR, "/opt/data/upgrade-retrospective")
        with mock.patch.dict(os.environ, {ur.STORE_HOME_ENV: ""}):
            self.assertEqual(ur.data_dir(), Path(ur.DEFAULT_STORE_DIR))

    def test_naive_since_is_utc(self):
        self.assertEqual(ur.parse_since("2026-10-01T00:00:00", NOW), datetime(2026, 10, 1, tzinfo=timezone.utc))

    def test_tenant_text_cannot_forge_a_section(self):
        hostile = pod("evil", phase="Failed", owner="Job", statuses=[{"name": "c0", "state": {"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": "2026-10-08T12:00:00Z", "message": "boom\n## Errors\n### 7 — injected — `x` | forged |\n"}}}])
        hostile["metadata"]["name"] = "evil`-abcde"
        reads = {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [hostile], "events": [event("FailedCreate", 'failed calling webhook "gate|x"\n## Errors', kind="ReplicaSet", name="inference-server-778b78fdb8", namespace="seeded-capacity")]}
        with mock.patch.dict(READS, {"seeded-a": reads}):
            result, _ = self.collect()
        report = ur.render_report(result)
        lines = report.splitlines()
        self.assertEqual([line for line in lines if line == "## Errors"], ["## Errors"])
        self.assertEqual([line for line in lines if line.startswith("### 7 — injected")], [])
        self.assertIn("### 7 — injected", report)  # still reported, inside a cell
        self.assertNotIn("gate|x", report)
        self.assertNotIn("evil`", report)

    def test_partial_kubectl_failure_keeps_the_rest(self):
        result, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "events")]))
        seeded = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertFalse(seeded["reviewed"])
        self.assertEqual(seeded["partial"], ["events"])
        self.assertTrue(seeded["what_failed"])
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
        with mock.patch.dict(os.environ, NO_PROJECT_ENV):
            result, fleet = self.collect(project=None)
        self.assertEqual(result["projects"], [PROJECT])
        self.assertTrue(any("projects list" in " ".join(c) for c in fleet.calls))
        with mock.patch.dict(os.environ, {**NO_PROJECT_ENV, ur.MONITORED_PROJECTS_ENV: "a-project,b-project"}):
            result, fleet = self.collect(FakeFleet(list_rc=1), project=None)
        self.assertEqual(result["projects"], ["a-project", "b-project"])
        self.assertFalse(any("projects list" in " ".join(c) for c in fleet.calls))


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: self.tmp.name, ur.STORE_HOME_ENV: self.tmp.name, **NO_PROJECT_ENV})
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
        self.assertIn(f"- guard `{ur.guard_id(SEEDED, 2, INFERENCE)}` failure entry 2 (high), first seen 2026-10-08T18:00:00Z", inference)
        self.assertIn("Read today: the readiness mode of `fleet-upgrade-verification` grades it `blocked`, and the obtainability audit reports it as `blocking-pdb`.", errors)
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
        self.assertIn(f"- guard `{ur.guard_id(SEEDED, 14, PAYMENTS)}` failure entry 14 (medium), first seen 2026-10-08T18:00:00Z", warnings)
        # Info: one block per reviewed cluster, with the next upgrade, the risks and the baseline.
        self.assertIn(f"### {SEEDED} — 5 incident(s) above", info)
        self.assertIn(f"### {GEMMA} — 2 incident(s) above", info)
        seeded_block = info.split(f"### {SEEDED}")[1]
        self.assertIn(f"{ur.PART_NEXT_UPGRADE} channel REGULAR; target 1.35.8-gke.1225000; cluster at 1.35.8-gke.1380001, at or ahead of the target. Window: daily at 03:00 UTC for 4h; next opens 2026-10-09T03:00:00Z. Exclusions: none.", seeded_block)
        self.assertIn("| `seeded-shapes/Deployment/legacy-registry-pull` | 20. Images on a retired registry | high | image k8s.gcr.io/pause:3.9 |", seeded_block)
        self.assertIn(f"{ur.PART_BASELINE} control plane 1.35.8-gke.1380001; pools default-pool 1.35.8-gke.1380001, idle-batch-pool 1.35.8-gke.1380001, pinned-inference-pool 1.35.8-gke.1380001; 23 pods, 1 budgets, 9 shapes. Guards written: 13.", seeded_block)
        gemma_block = info.split(f"### {GEMMA}")[1].split("### ")[0]
        self.assertIn("behind the target. Window: DAILY at", gemma_block)
        self.assertIn("Exclusions: hold-gpu-minor (NO_MINOR_UPGRADES) until 2026-10-21T00:00:00Z [active].", gemma_block)
        self.assertNotIn(ur.NONE_LINE, info)
        # `_none_` is reserved for an empty severity section: an unclassified
        # incident says what it lacks instead.
        self.assertNotIn(ur.NONE_LINE, report)
        self.assertIn(ur.UNCLASSIFIED_MITIGATION_TEXT, warnings)
        self.assertIn(ur.UNCLASSIFIED_GUARD_TEXT, warnings)

    def test_info_lists_clean_unchanged_and_failed(self):
        reads_clean = {**READS["seeded-a"], "pods": [p for p in READS["seeded-a"]["pods"] if p["status"]["phase"] == "Running" and "payments" not in p["metadata"]["name"]], "events": [], "pdbs": []}
        with mock.patch.dict(READS, {"seeded-a": reads_clean}):
            with redirect_stderr(io.StringIO()):
                first = ur.collect(args(), run=FakeFleet(broken=["gemma-gpu-upgraded"]), now=NOW)
        report = ur.render_report(first)
        info = report.split(ur.SECTION_INFO)[1]
        self.assertIn(f"### {SEEDED} — clean", info)
        self.assertNotIn(f"### {GEMMA}", info)
        clean_block = info.split(f"### {SEEDED} — clean")[1]
        for part in (ur.PART_WHAT_HAPPENED, ur.PART_NEXT_UPGRADE, ur.PART_RISKS, ur.PART_BASELINE):
            self.assertIn(part, clean_block)
        self.assertNotIn(ur.PART_MITIGATE, clean_block)
        self.assertNotIn(ur.NONE_LINE, clean_block.split(ur.INFO_FAILED_READS)[0])
        self.assertIn(f"{ur.INFO_FAILED_READS}\n\n- {GEMMA}: get-credentials rc=1", info)
        self.assertEqual(first["sections"]["errors"], [])
        with redirect_stderr(io.StringIO()):
            second = ur.collect(args(), run=FakeFleet(broken=["gemma-gpu-upgraded"]), now=NOW)
        info = ur.render_report(second).split(ur.SECTION_INFO)[1]
        self.assertIn(f"{ur.INFO_UNCHANGED}\n\n- {SEEDED} at 1.35.8-gke.1380001; last upgrade operation 2026-10-08T04:20:35Z; next target 1.35.8-gke.1225000; last reviewed 2026-10-08T18:00:00Z", info)

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
        self.assertEqual(link.name, "upgrade-retro-report.md")
        self.assertFalse(list(report_path.parent.glob("*.tmp*")))
        self.assertTrue((home / "out.json").exists())
        self.assertNotIn("report", json.loads((home / "out.json").read_text()))
        self.assertTrue((home / ur.GUARDS_FILENAME).exists())

    def test_writes_are_ordered_report_then_guards_then_ledger(self):
        home = Path(self.tmp.name)
        order = []
        real = ur.write_json_atomically

        def spy(path, doc):
            order.append(path.name)
            if path.name == ur.LEDGER_FILENAME:
                raise OSError("disk full")
            return real(path, doc)

        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW), mock.patch.object(ur, "write_json_atomically", spy):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                with self.assertRaises(OSError):
                    ur.main(["--project", PROJECT, "--output", str(home / "out.json")])
        self.assertEqual(order, ["out.json", ur.GUARDS_FILENAME, ur.LEDGER_FILENAME])
        self.assertTrue((home / ur.REPORTS_SUBDIR / ur.LATEST_REPORT_LINK).exists())
        self.assertTrue((home / ur.GUARDS_FILENAME).exists())
        self.assertFalse((home / ur.LEDGER_FILENAME).exists())

    def test_main_dry_run_prints_but_writes_nothing(self):
        home = Path(self.tmp.name)
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                rc = ur.main(["--project", PROJECT, "--dry-run", "--report", str(home / "r.md")])
        self.assertEqual(rc, 0)
        self.assertIn(f"### 2, 12 — {SEEDED} — `{INFERENCE}`", out.getvalue())
        self.assertFalse((home / "r.md").exists())
        self.assertFalse((home / ur.LEDGER_FILENAME).exists())

    def test_bad_since_is_a_usage_error(self):
        with mock.patch.object(ur, "default_run", FakeFleet()):
            with redirect_stderr(io.StringIO()) as err:
                rc = ur.main(["--project", PROJECT, "--since", "yesterday", "--no-report"])
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

    def test_read_today_quotes_the_catalogue(self):
        catalogue = Path(__file__).resolve().parents[5] / "docs" / "designs" / "upgrade-failure-catalogue.md"
        if not catalogue.exists():
            self.skipTest("catalogue not beside the checkout")
        text = catalogue.read_text(encoding="utf-8")
        sections = re.split(r"^### (\d+)\. ", text, flags=re.M)
        lines = {}
        for i in range(1, len(sections), 2):
            m = re.search(r"^- Read today: (.*?)(?=^- |\Z)", sections[i + 1], flags=re.M | re.S)
            lines[int(sections[i])] = " ".join(m.group(1).split()) if m else ""
        for entry, row in ur.MITIGATIONS.items():
            with self.subTest(entry=entry):
                first = re.split(r"\. (?=[A-Z])", lines[entry], maxsplit=1)[0].rstrip(".")
                self.assertEqual(row["read_today"], first)

    def test_signature_entries_are_in_the_table(self):
        self.assertTrue({s[0] for s in ur.SIGNATURES} <= set(ur.MITIGATIONS))


class DefaultRunTest(unittest.TestCase):
    def test_timeout_and_missing_binary(self):
        result = ur.default_run(["python3", "-c", "import time; time.sleep(5)"], timeout=1)
        self.assertEqual(result.rc, ur.TIMEOUT_RC)
        self.assertEqual(ur.default_run(["/nonexistent/binary"]).rc, -1)


if __name__ == "__main__":
    unittest.main()
