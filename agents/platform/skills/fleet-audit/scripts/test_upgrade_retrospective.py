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

import argparse
import copy
import errno
import inspect
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from contextlib import suppress as contextlib_suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
# Every resolver asks the operator's answer when a run passes neither scope flag: the root-owned
# file the sandbox writes, else KUBEAGENTS_SCOPE_DECLARED. A shell that carries either would turn
# this suite's --project runs into refusals; both are neutralised at import, as the sibling suites do.
os.environ.pop("KUBEAGENTS_SCOPE_DECLARED", None)  # module-level, on purpose
import upgrade_retrospective as ur  # noqa: E402

ur.fleet_scope_args.SCOPE_DECLARED_FILE = "/nonexistent/kube-agents-sandbox/scope-declared"

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
API_OFF_STDERR = "ERROR: (gcloud.container.{api}.list) ResponseError: code=403, message=Kubernetes Engine API has not been used in project {project} before or it is disabled. Enable it by visiting https://console.developers.google.com/apis/api/container.googleapis.com/overview?project={project} then retry. reason: SERVICE_DISABLED"
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


def fleet_scope_env():
    return ur.fleet_scope_args.SCOPE_DECLARED_ENV


def run_of(rc=0, stdout="", stderr=""):
    return ur.Run(["x"], rc, stdout, stderr, 0.01)


class FakeFleet:
    """Answers every subprocess the collector issues from the fixtures.
    `broken` names clusters whose get-credentials fails; `kubectl_fail`
    names (cluster, kind) reads that fail."""

    def __init__(self, clusters=None, operations=None, broken=(), kubectl_fail=(), list_rc=0, list_fail=(), ops_fail=(), api_off=(), projects_list_rc=0, projects_listed=None, active_project=PROJECT, api_off_names=None, stall=None):
        self.clusters = clusters if clusters is not None else [cluster_doc("seeded-a"), cluster_doc("gemma-gpu-upgraded")]
        self.operations = operations if operations is not None else OPERATIONS
        self.broken, self.kubectl_fail, self.list_rc, self.list_fail = set(broken), set(kubectl_fail), list_rc, set(list_fail)
        # Projects whose `operations list` fails (a quota error, say).
        self.ops_fail = set(ops_fail)
        # Projects with the Kubernetes Engine API off: both listings refuse with gcloud's words for it,
        # naming the listed project, or `api_off_names` (a quota project with the API off names itself).
        self.api_off = set(api_off)
        self.api_off_names = api_off_names
        # (cluster, read) -> seconds a read hangs before answering; it honours the timeout it is
        # given, as a subprocess does, and answers rc 124 when the hang outlasts it.
        self.stall = stall or {}
        self.timeouts = []
        # The fake's clock, for a run handed `clock`: a stall advances it by the time the read took
        # rather than sleeping, so a deadline test decides on this clock and not on a wall-clock second.
        self.now = 0.0
        # An undeclared install's `gcloud projects list`: its exit code, and the projects it names
        # (the active project alone by default; a list without it is a filtered listing).
        self.projects_list_rc, self.projects_listed = projects_list_rc, projects_listed
        # The active gcloud project (`config get-value project`); "" is a configuration with none set.
        self.active_project = active_project
        self.calls = []

    def clock(self):
        return self.now

    def __call__(self, argv, *, timeout=None, env=None):
        self.calls.append(argv)
        joined = " ".join(argv)
        if "clusters list" in joined:
            project = argv[argv.index("--project") + 1]
            if project in self.api_off:
                return run_of(1, "", API_OFF_STDERR.format(project=self.api_off_names or project, api="clusters"))
            if self.list_rc or project in self.list_fail:
                return run_of(self.list_rc or 1, "", "PERMISSION_DENIED")
            return run_of(0, json.dumps([{k: v for k, v in c.items() if k != "project"} for c in self.clusters if c.get("project", PROJECT) == project]))
        if "operations list" in joined:
            if argv[argv.index("--project") + 1] in self.api_off:
                return run_of(1, "", API_OFF_STDERR.format(project=self.api_off_names or argv[argv.index("--project") + 1], api="operations"))
            if argv[argv.index("--project") + 1] in self.ops_fail:
                return run_of(1, "", "RESOURCE_EXHAUSTED: quota exceeded")
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
            if (cluster, key) in self.stall:
                self.timeouts.append((cluster, key, timeout))
                hang = self.stall[(cluster, key)]
                self.now += min(hang, timeout) if timeout is not None else hang
                if timeout is not None and hang > timeout:
                    return run_of(ur.TIMEOUT_RC, "", "timed out")
            return run_of(0, json.dumps({"items": READS[cluster][key]}))
        if "projects describe" in joined:
            return run_of(0, "123456789\n")
        if "config get-value" in joined:
            return run_of(0, self.active_project + "\n") if self.active_project else run_of(0, "")
        if "projects list" in joined:
            if self.projects_list_rc:
                return run_of(self.projects_list_rc, "", "PERMISSION_DENIED")
            return run_of(0, "\n".join(self.projects_listed if self.projects_listed is not None else [PROJECT]) + "\n")
        raise AssertionError(f"unexpected command {joined}")


def args(**overrides):
    """The scheduled run's arguments: `--full` unless the test narrows the run
    with --cluster or --since, or says otherwise."""
    base = {"project": None, "scope_projects": PROJECT, "scope_unread": None, "cluster": None, "since": None, "ledger": None, "guards": None, "output": None, "report": None, "no_report": False, "dry_run": False, "reset_ledger": False, "probe_lock": False, "manifest_file": None, "after_upgrade": False}
    base.update(overrides)
    base.setdefault("full", base["cluster"] is None and base["since"] is None and bool(base["scope_projects"]))
    if "full" in overrides:
        base["full"] = overrides["full"]
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

    def test_a_retry_with_a_status_message_and_no_error_is_not_a_clean_retry(self):
        # One predicate decides a failed operation everywhere: `operation_summary` reads the failure as
        # `error.message` or `statusMessage`, so a later operation that reached DONE with a
        # `statusMessage` and no `error` object is a failed operation to the guard and the Error, and
        # does not clear the earlier guard as a clean retry; without the message it does.
        bad = copy.deepcopy(next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/idle-batch-pool" in o["targetLink"]))
        errored = next(o for o in OPERATIONS if o.get("error"))
        bad["error"], bad["statusMessage"] = errored["error"], errored["statusMessage"]
        [guard] = ur.operation_guards_for(SEEDED, [ur.operation_summary(bad)], "2026-10-08T18:00:00Z")
        retry = {**copy.deepcopy(bad), "name": "operation-retry", "startTime": "2026-10-20T03:00:00Z", "endTime": "2026-10-20T03:12:00Z"}
        retry.pop("error", None)
        self.assertTrue(ur._operation_failed(ur.operation_summary(retry)))
        later = "2026-10-22T18:00:00Z"
        result = ur.recheck_operation_guards([guard], [bad, retry], {PROJECT}, set(), later)
        self.assertEqual((result["refreshed"], result["cleared"]), ([guard["id"]], []))
        retry.pop("statusMessage", None)
        result = ur.recheck_operation_guards([guard], [bad, retry], {PROJECT}, set(), later)
        self.assertEqual(([item["id"] for item in result["cleared"]], result["refreshed"]), ([guard["id"]], []))

    def test_list_operations_filters_type_and_window_itself(self):
        fleet = FakeFleet()
        ops, error = ur.list_operations(PROJECT, datetime(2026, 10, 7, tzinfo=timezone.utc), run=fleet)
        self.assertIsNone(error)
        self.assertTrue(all(o["operationType"] in ur.UPGRADE_OPERATION_TYPES for o in ops))
        # Bounded by end, never by start: an operation that ended inside the window counts whatever
        # its start, and one still in flight has no end to bound.
        self.assertTrue(all(ur.parse_ts(o["endTime"]) >= datetime(2026, 10, 7, tzinfo=timezone.utc) for o in ops))
        self.assertIn("--filter", fleet.calls[0])
        self.assertIn("UPGRADE_MASTER OR UPGRADE_NODES", " ".join(fleet.calls[0]))
        self.assertIn("endTime>=2026-10-07T00:00:00Z OR status!=DONE", " ".join(fleet.calls[0]))

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
        self.assertIn(ur.GATE_CLOSED_TEXT, rows[0]["classifications"][0]["detail"])

    def test_pool_gated_entries_are_high_only_after_a_pool_operation(self):
        reads = {**READS["seeded-a"]}
        rows = by_object(symptoms_of("seeded-a", reads=reads, ops=[]), INFERENCE, "pending")
        self.assertEqual(entries(rows[0]), {(2, ur.MEDIUM)})
        rows = by_object(symptoms_of("seeded-a"), INFERENCE, "pending")
        self.assertEqual(entries(rows[0]), {(2, ur.HIGH)})

    def test_displaced_pinned_replica_is_entry_2_and_a_pure_selector_miss_is_12(self):
        # The fixture's displaced pinned replica (inference-server, pinned to pinned-inference-pool by
        # `seeded-role`): the scheduler writes "Insufficient cpu" for the pool the pod targets and a
        # "didn't match" clause for every pool it does not, so the row is entry 2 and never 12.
        # seeded-b's pinned-batch-runner has the same shape; the captures carry seeded-a.
        rows = by_object(self.seeded, INFERENCE, "pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(entries(rows[0]), {(2, ur.HIGH)})
        [two] = rows[0]["classifications"]
        self.assertEqual(two["evidence"], "3 of 4 pods: Insufficient cpu; e.g. inference-server-778b78fdb8-cp2pf")
        self.assertEqual(rows[0]["pods"], ["inference-server-778b78fdb8-cp2pf", "inference-server-778b78fdb8-ld26r", "inference-server-778b78fdb8-zxlkz"])
        nodes = READS["seeded-a"]["nodes"]
        displaced = "0/4 nodes are available: 1 Insufficient cpu, 3 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 1 No preemption victims found for incoming pod, 3 Preemption is not helpful for scheduling."
        runner = pod("pinned-batch-runner-6d5f8b7c9-x2k4q", namespace="seeded-upgrade", scheduled_message=displaced, node_selector={"cloud.google.com/gke-nodepool": "pinned-inference-pool"})
        [row] = symptoms_of("seeded-a", reads={"pods": [runner], "nodes": nodes, "events": [], "pdbs": []})
        self.assertEqual(entries(row), {(2, ur.HIGH)})
        self.assertEqual(row["classifications"][0]["evidence"], "1 of 1 pods: Insufficient cpu; e.g. pinned-batch-runner-6d5f8b7c9-x2k4q")
        # A label no seeded-a node carries (`seeded-role` is `pinned-inference` on the one node that has
        # it): the nodes read says no node can take the pod, entry 12 at medium, the clause set quoted.
        self.assertNotIn("batch", {n["metadata"]["labels"].get("seeded-role") for n in nodes})
        pure = pod("pinned-batch-runner-6d5f8b7c9-x2k4q", namespace="seeded-upgrade", scheduled_message="0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling.", node_selector={"seeded-role": "batch"})
        [row] = symptoms_of("seeded-a", reads={"pods": [pure], "nodes": nodes, "events": [], "pdbs": []})
        self.assertEqual(entries(row), {(12, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["detail"], f"{ur.NO_NODE_CARRIES_FORMAT.format(labels='seeded-role=batch')}; {ur.LABEL_NOT_DROPPED_TEXT}")
        self.assertEqual(row["classifications"][0]["evidence_core"], "0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector")
        # The order is one table the classifier iterates, most specific first, 12 last.
        self.assertEqual(ur.ENTRY_ORDER, (7, 6, 19, 18, 14, 15, 20, 17, 3, 2, 1, 5, 12))
        self.assertEqual(set(ur.ENTRY_MATCHERS), set(ur.ENTRY_ORDER))

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

    def test_budget_hold_is_filed_when_every_covered_pod_was_displaced(self):
        # Take away the one replica that landed on the surge node: the three Pending replicas carry no
        # nodeName, but two were created inside the pinned-inference-pool drain, so the hold is still filed.
        pods = copy.deepcopy(READS["seeded-a"]["pods"])
        landed = next(p for p in pods if p["metadata"]["name"] == "inference-server-778b78fdb8-txlv7")
        landed["spec"]["nodeName"] = None
        landed["status"] = {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "0/4 nodes are available: 1 Insufficient cpu.", "lastTransitionTime": "2026-10-08T05:30:00Z"}]}
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "pods": pods}) if s["category"] == "pdb"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["upgraded_pools"], ["pinned-inference-pool"])
        self.assertEqual(entries(rows[0]), {(1, ur.HIGH)})
        # A pool named on the pod's own selector or its owner's template counts too.
        for p in pods:
            if p["metadata"]["name"].startswith("inference-server"):
                p["metadata"]["creationTimestamp"] = "2026-09-25T17:02:48Z"
                p["spec"]["nodeSelector"] = {"cloud.google.com/gke-nodepool": "pinned-inference-pool"}
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "pods": pods}) if s["category"] == "pdb"]
        self.assertEqual(len(rows), 1)
        for p in pods:
            if p["metadata"]["name"].startswith("inference-server"):
                p["spec"]["nodeSelector"] = {}
        workloads = copy.deepcopy(READS["seeded-a"]["workloads"])
        next(w for w in workloads if w["metadata"]["name"] == "inference-server")["spec"]["template"]["spec"]["nodeSelector"] = {"cloud.google.com/gke-nodepool": "pinned-inference-pool"}
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "pods": pods, "workloads": workloads}) if s["category"] == "pdb"]
        self.assertEqual(len(rows), 1)
        # The fixture's own template selector is a custom label (`seeded-role: pinned-inference`): it
        # resolves to the pool through the pools' labels, the same way the Pending-pod gate reads it.
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "pods": pods}) if s["category"] == "pdb"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["upgraded_pools"], ["pinned-inference-pool"])
        # With no placement, no selector on pod or template and no drain-time creation, nothing links the budget.
        bare = copy.deepcopy(workloads)
        next(w for w in bare if w["metadata"]["name"] == "inference-server")["spec"]["template"]["spec"].pop("nodeSelector")
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "pods": pods, "workloads": bare}) if s["category"] == "pdb"]
        self.assertEqual(rows, [])

    def test_budget_on_a_pool_nothing_drained_is_not_a_failure(self):
        self.assertEqual(by_object(self.gemma, "kubeagents-system/PodDisruptionBudget/gemma-server"), [])
        ops_without_pool = [o for o in ops_for("seeded-a") if "pinned-inference-pool" not in o["targetLink"]]
        self.assertEqual(by_object(symptoms_of("seeded-a", ops=ops_without_pool), "seeded-capacity/PodDisruptionBudget/inference-server"), [])

    def test_entry_6_job_pods_in_error_collapse_onto_their_cronjob(self):
        [row] = by_object(self.gemma, TUNER)
        self.assertEqual(row["reason"], "Error")
        self.assertEqual(row["owner_kind"], "CronJob")
        self.assertEqual(entries(row), {(6, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["evidence"], "2 of 2 pods: CronJob pod in Error; name mentions flowcontrol; e.g. legacy-flowcontrol-tuner-29857980-9r4jr")

    def test_unclassified_symptom_writes_a_guard_with_its_source(self):
        # inventory-api's CreateContainerConfigError matches no row: a Warning with a failure guard,
        # `unclassified` in the entry slot, sourced from pod state (the re-check that clears it is in
        # test_recheck_clears_a_guard_whose_finding_is_gone).
        [row] = by_object(symptoms_of("seeded-a"), "seeded-stall/Deployment/inventory-api")
        [guard] = ur.guards_for(SEEDED, [row], "2026-10-08T18:00:00Z")
        self.assertEqual((guard["id"], guard["entry"], guard["title"], guard["source"], guard["check"], guard["reviews"]), (f"{SEEDED}#failure#unclassified#seeded-stall/Deployment/inventory-api", None, ur.UNCLASSIFIED, "not-ready", ur.CHECK_SYMPTOM_UNCLASSIFIED, 1))
        # An event-only unclassified symptom (a ConfigMap mount failure) is held as not re-checkable.
        mount = event("FailedMount", 'MountVolume.SetUp failed for volume "config" : object "apps"/"settings" not registered', name="web-7d9f8b6c5-abcde")
        [row] = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "events": READS["seeded-a"]["events"] + [mount]}) if s["object"] == "apps/Pod/web-7d9f8b6c5-abcde"]
        [guard] = ur.guards_for(SEEDED, [row], "2026-10-08T18:00:00Z")
        self.assertEqual((guard["entry"], guard["source"], guard["check"]), (None, ur.GUARD_SOURCE_EVENT, ur.CHECK_SYMPTOM_UNCLASSIFIED))

    def test_unclassified_symptom_is_still_reported(self):
        rows = by_object(self.seeded, "seeded-stall/Deployment/inventory-api", "not-ready")
        self.assertEqual(rows[0]["reason"], "CreateContainerConfigError")
        self.assertEqual(entries(rows[0]), {(None, ur.MEDIUM)})
        self.assertEqual(rows[0]["classifications"][0]["title"], ur.UNCLASSIFIED)

    def test_healthy_pods_are_not_symptoms(self):
        pods = {p for s in self.seeded for p in s.get("pods") or []}
        self.assertNotIn("cgroup-blind-jvm-79dd4fb5c-dxxjg", pods)
        self.assertNotIn("legacy-registry-pull-7979df9ddc-flfzz", pods)
        self.assertIn("inference-server-778b78fdb8-cp2pf", pods)

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
        # The capture's own events all collapse into pod rows or fall outside EVENT_REASONS, so a kept
        # event is planted: an attach failure last seen 2026-10-08T12:00Z.
        attach = event("FailedAttachVolume", "AttachVolume.Attach failed for volume pv-1", name="inference-server-778b78fdb8-zzzzz", namespace="seeded-capacity")
        reads = {**READS["seeded-a"], "events": READS["seeded-a"]["events"] + [attach]}
        kept = [s for s in symptoms_of("seeded-a", reads=reads) if s["category"] == "event"]
        self.assertEqual([(s["object"], s["reason"]) for s in kept], [(INFERENCE, "FailedAttachVolume")])
        late = [s for s in symptoms_of("seeded-a", reads=reads, window=datetime(2026, 10, 9, tzinfo=timezone.utc)) if s["category"] == "event"]
        self.assertEqual(late, [])


class ClassifierSignatureTest(unittest.TestCase):
    """Signatures built from the captured shapes."""
    def classify(self, pods=(), nodes=None, events=(), pdbs=(), ops=None, name="seeded-a", owners=(), workloads=()):
        reads = {"pods": list(pods), "nodes": nodes if nodes is not None else READS["seeded-a"]["nodes"], "events": list(events), "pdbs": list(pdbs), "owners": list(owners), "workloads": list(workloads)}
        return symptoms_of(name, reads=reads, ops=ops)

    def test_entry_7_webhook_rejection(self):
        rejection = event("FailedCreate", 'Error creating: Internal error occurred: failed calling webhook "gate.example.io": Post "https://gate.apps.svc:443/validate": no endpoints available', kind="ReplicaSet")
        # Event text alone: a namespace user can write an Event, so the match is medium, marked, and
        # the excerpt is quoted as the object's own text.
        [row] = self.classify(events=[rejection])
        self.assertEqual(entries(row), {(7, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["detail"], "webhook gate.example.io; " + ur.FROM_EVENT_TEXT_DETAIL.format(source=ur.TEXT_SOURCE_EVENT))
        self.assertEqual(row["classifications"][0]["evidence"], f'{ur.TEXT_SOURCE_EVENT} reads "FailedCreate {rejection["message"]}"')
        # The ReplicaSet's own ReplicaFailure condition, set by its controller, agrees: high.
        failing = {"kind": "ReplicaSet", "metadata": {"name": "x", "namespace": "apps"}, "status": {"conditions": [{"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate"}]}}
        [row] = self.classify(events=[rejection], owners=[failing])
        self.assertEqual(entries(row), {(7, ur.HIGH)})
        self.assertEqual(row["classifications"][0]["detail"], "webhook gate.example.io; from event text; ReplicaSet condition ReplicaFailure=True agrees")
        self.assertTrue(row["classifications"][0]["evidence"].startswith(f'{ur.TEXT_SOURCE_EVENT} reads "'))
        # The quoted excerpt travels as the object's own text into the guard.
        [guard] = ur.guards_for(SEEDED, [row], "2026-10-08T18:00:00Z")
        self.assertTrue(guard["evidence"].startswith(f'{ur.TEXT_SOURCE_EVENT} reads "'))

    def test_termination_message_alone_is_medium_unless_the_container_state_agrees(self):
        # The driver text sits in the container's termination message, which the workload writes. The
        # container's current state, terminated with a non-zero exit, is the kubelet's and agrees: high.
        crashed = pod("cuda-job", statuses=[{"name": "c0", "state": {"terminated": {"reason": "Error", "exitCode": 1, "message": "CUDA Error 803: system has unsupported display driver / cuda driver combination"}}}])
        [row] = self.classify(pods=[crashed])
        self.assertEqual(entries(row), {(18, ur.HIGH)})
        self.assertEqual(row["classifications"][0]["detail"], "from container termination message; container c0 state Error agrees")
        self.assertIn(f'{ur.TEXT_SOURCE_TERMINATION} reads "Error 803"', row["classifications"][0]["evidence"])
        # The same text in a previous termination while the container now runs: nothing the API server
        # set agrees that the driver fails now, so medium and marked.
        running = pod("cuda-job", statuses=[{"name": "c0", "state": {"running": {}}, "lastState": {"terminated": {"reason": "Error", "exitCode": 1, "message": "CUDA Error 803", "finishedAt": "2026-10-08T12:00:00Z"}}}])
        [row] = self.classify(pods=[running])
        self.assertEqual(entries(row), {(18, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["detail"], ur.FROM_EVENT_TEXT_DETAIL.format(source=ur.TEXT_SOURCE_TERMINATION))

    def test_entry_12_holds_only_when_no_node_carries_the_pinned_label(self):
        # The discriminator is the nodes read, not the scheduler's message. Every fixture node carries
        # `beta.kubernetes.io/arch=amd64`; one carries `seeded-role=pinned-inference`; none carries `team`.
        nodes = READS["seeded-a"]["nodes"]
        carried = {(k, v) for n in nodes for k, v in n["metadata"]["labels"].items()}
        self.assertIn(("beta.kubernetes.io/arch", "amd64"), carried)
        self.assertIn(("seeded-role", "pinned-inference"), carried)
        self.assertNotIn("team", {k for k, _ in carried})
        miss = "0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling."
        # Misses alone with a label no node carries: row 12, medium (a label the pool never carried, a
        # deleted pool or a pool at zero nodes read the same), the clause set quoted as evidence.
        [row] = self.classify(pods=[pod("legacy", scheduled_message=miss, node_selector={"team": "legacy"})])
        self.assertEqual(entries(row), {(12, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["detail"], f"no node carries team=legacy; {ur.LABEL_NOT_DROPPED_TEXT}")
        self.assertEqual(row["classifications"][0]["evidence_core"], "0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector")
        # The same message with a label every node carries: the pod's own pool evaluated it and refused
        # it for a reason the message does not keep; unclassified, never 12.
        [row] = self.classify(pods=[pod("arch-pinned", scheduled_message=miss, node_selector={"beta.kubernetes.io/arch": "amd64"})])
        self.assertEqual(entries(row), {(None, ur.MEDIUM)})
        # A pin a node carries beside the clause naming the pool's refusal: rows 2 and 18 were tried
        # first; a taint, a pod-count ceiling or a cordon is neither, so unclassified.
        pinned = {"seeded-role": "pinned-inference"}
        for own in ("1 node(s) had untolerated taint {dedicated: batch}", "1 Too many pods", "1 node(s) were unschedulable"):
            with self.subTest(own=own):
                message = f"0/4 nodes are available: {own}, 3 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling."
                [row] = self.classify(pods=[pod("batch", scheduled_message=message, node_selector=pinned)])
                self.assertEqual(entries(row), {(None, ur.MEDIUM)})
        # The same clauses with a label no node carries: row 12 holds and the detail names the pool's
        # own reason; a dotted taint key does not end the clause set, which runs to the preemption
        # sentence.
        spot = "0/5 nodes are available: 2 node(s) had untolerated taint {cloud.google.com/gke-spot: true}, 3 node(s) didn't match Pod's node affinity/selector. preemption: 0/5 nodes are available: 5 Preemption is not helpful for scheduling."
        [row] = self.classify(pods=[pod("legacy", scheduled_message=spot, node_selector={"team": "legacy"})])
        self.assertEqual(entries(row), {(12, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["detail"], f"no node carries team=legacy; {ur.LABEL_NOT_DROPPED_TEXT}; " + ur.OWN_POOL_REASON_FORMAT.format(clauses="2 node(s) had untolerated taint {cloud.google.com/gke-spot: true}"))
        self.assertEqual(row["classifications"][0]["evidence_core"], "0/5 nodes are available: 2 node(s) had untolerated taint {cloud.google.com/gke-spot: true}, 3 node(s) didn't match Pod's node affinity/selector")
        # A GPU clause files as row 18, which precedes 12.
        gpu = "0/3 nodes are available: 1 Insufficient nvidia.com/gpu, 2 node(s) didn't match Pod's node affinity/selector. preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling."
        [row] = self.classify(pods=[pod("trainer", scheduled_message=gpu, node_selector={"team": "legacy"})])
        self.assertEqual({c["entry"] for c in row["classifications"]}, {18})
        # The repository's entry-12 reproduction (bench/upgrade-scenarios/evidence/12/label.txt) once
        # the pool is rebuilt: the replacement node does not carry `team=legacy`, the message is misses
        # alone, and the preemption sentence's own `0/3 nodes are available` is not part of the set.
        rebuilt = "0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector. no new claims to deallocate, preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling."
        [row] = self.classify(pods=[pod("label-pinned-5746f57c68-ghnpj", scheduled_message=rebuilt, node_selector={"team": "legacy"})])
        self.assertEqual(entries(row), {(12, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["evidence_core"], "0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector")
        # Its drain-time message on a settled cluster whose node still carries the label (a cordon by
        # hand): the cordon refused the pod, unclassified.
        drain = "0/3 nodes are available: 1 node(s) were unschedulable, 2 node(s) didn't match Pod's node affinity/selector. no new claims to deallocate, preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling."
        [row] = self.classify(pods=[pod("label-pinned-5746f57c68-ghnpj", scheduled_message=drain, node_selector=pinned)])
        self.assertEqual(entries(row), {(None, ur.MEDIUM)})
        # No nodes read: nothing can say, unclassified. A nodes read that lists no node: row 12.
        legacy = pod("legacy", scheduled_message=miss, node_selector={"team": "legacy"})
        [row] = symptoms_of("seeded-a", reads={"pods": [legacy], "events": [], "pdbs": []})
        self.assertEqual(entries(row), {(None, ur.MEDIUM)})
        [row] = self.classify(pods=[legacy], nodes=[])
        self.assertEqual(entries(row), {(12, ur.MEDIUM)})
        # A FailedScheduling event row takes its pins from its owner's template, as the pool gate
        # does; with none it is unclassified, never 12.
        workloads = copy.deepcopy([w for w in READS["seeded-a"]["workloads"] if w["metadata"]["name"] == "payments-api"])
        workloads[0]["spec"]["template"]["spec"]["nodeSelector"] = {"team": "legacy"}
        gone = event("FailedScheduling", miss, name="payments-api-79b77b8c67-gone1", namespace="seeded-debug")
        [row] = by_object(self.classify(events=[gone], owners=READS["seeded-a"]["owners"], workloads=workloads), PAYMENTS)
        self.assertEqual(entries(row), {(12, ur.MEDIUM)})
        self.assertTrue(row["classifications"][0]["detail"].startswith(f"no node carries team=legacy; {ur.LABEL_NOT_DROPPED_TEXT}"), row["classifications"][0]["detail"])
        [row] = by_object(self.classify(events=[gone], owners=READS["seeded-a"]["owners"]), PAYMENTS)
        self.assertEqual(entries(row), {(None, ur.MEDIUM)})

    def test_rows_2_and_18_read_a_clause_set_with_a_dotted_key_whole(self):
        # The clause set runs to the scheduler's trailing sentence (`SCHEDULING_TAIL_RE`), so a period
        # inside a reason, `nvidia.com/gpu` or a `{node.kubernetes.io/not-ready: }` taint key, is part
        # of it, and rows 2 and 18 read their clauses from the whole set.
        self.assertEqual(ur.SCHEDULING_TAIL_RE.pattern, r"\. (?:no new claims to deallocate, )?preemption:")
        pinned = {"seeded-role": "pinned-inference"}
        gpu = "0/3 nodes are available: 1 Insufficient nvidia.com/gpu, 2 node(s) didn't match Pod's node affinity/selector. no new claims to deallocate, preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling."
        [row] = self.classify(pods=[pod("trainer", scheduled_message=gpu, node_selector=pinned)])
        self.assertEqual({c["entry"] for c in row["classifications"]}, {18})
        self.assertEqual(row["classifications"][0]["evidence_core"], "Insufficient nvidia.com/gpu")
        self.assertEqual(ur.SCHEDULING_TAIL_RE.split(gpu, 1)[0], "0/3 nodes are available: 1 Insufficient nvidia.com/gpu, 2 node(s) didn't match Pod's node affinity/selector")
        taint = "0/2 nodes are available: 1 node(s) had untolerated taint {node.kubernetes.io/not-ready: }, 1 Insufficient cpu. preemption: 0/2 nodes are available: 2 Preemption is not helpful for scheduling."
        [row] = self.classify(pods=[pod("api", scheduled_message=taint, node_selector=pinned)])
        self.assertEqual({c["entry"] for c in row["classifications"]}, {2})
        self.assertEqual(row["classifications"][0]["evidence_core"], "Insufficient cpu")
        self.assertEqual(ur.SCHEDULING_TAIL_RE.split(taint, 1)[0], "0/2 nodes are available: 1 node(s) had untolerated taint {node.kubernetes.io/not-ready: }, 1 Insufficient cpu")

    def test_the_pool_gate_opens_only_on_a_pool_the_pod_could_land_on(self):
        # A Pending pod with no pin is not placeable on every pool: a taint it does not tolerate and an
        # extended resource it requests are hard constraints, so only an upgrade of a pool it could
        # land on opens its gate. pinned-inference-pool's node is tainted and offers a GPU here.
        nodes = copy.deepcopy(READS["seeded-a"]["nodes"])
        gpu_node = next(n for n in nodes if n["metadata"]["labels"]["cloud.google.com/gke-nodepool"] == "pinned-inference-pool")
        gpu_node.setdefault("spec", {})["taints"] = [{"key": "dedicated", "value": "inference", "effect": "NoSchedule"}]
        gpu_node.setdefault("status", {}).setdefault("allocatable", {})["nvidia.com/gpu"] = "1"
        ops = ops_for("seeded-a")
        gpu_pool_only = [o for o in ops if "/nodePools/pinned-inference-pool" in o["targetLink"]]
        cpu_pool_only = [o for o in ops if "/nodePools/default-pool" in o["targetLink"]]
        cpu_pod = pod("web", scheduled_message="0/4 nodes are available: 3 Insufficient cpu, 1 node(s) had untolerated taint {dedicated: inference}. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling.")
        [row] = self.classify(pods=[cpu_pod], nodes=nodes, ops=gpu_pool_only)
        self.assertEqual(entries(row), {(2, ur.MEDIUM)})
        self.assertIn(ur.GATE_CLOSED_TEXT, row["classifications"][0]["detail"])
        [row] = self.classify(pods=[cpu_pod], nodes=nodes, ops=cpu_pool_only)
        self.assertEqual(entries(row), {(2, ur.HIGH)})
        gpu_pod = pod("trainer", scheduled_message="0/4 nodes are available: 1 Insufficient nvidia.com/gpu, 3 Insufficient nvidia.com/gpu. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling.")
        gpu_pod["spec"]["tolerations"] = [{"key": "dedicated", "operator": "Equal", "value": "inference", "effect": "NoSchedule"}]
        gpu_pod["spec"]["containers"][0]["resources"] = {"requests": {"nvidia.com/gpu": "1"}, "limits": {"nvidia.com/gpu": "1"}}
        [row] = self.classify(pods=[gpu_pod], nodes=nodes, ops=cpu_pool_only)
        self.assertEqual(entries(row), {(18, ur.MEDIUM)})
        self.assertIn(ur.GATE_CLOSED_TEXT, row["classifications"][0]["detail"])
        [row] = self.classify(pods=[gpu_pod], nodes=nodes, ops=gpu_pool_only)
        self.assertEqual(entries(row), {(18, ur.HIGH)})
        # Without a nodes read nothing narrows the fallback: any upgraded pool opens the gate, as before.
        [row] = symptoms_of("seeded-a", reads={"pods": [cpu_pod], "events": [], "pdbs": []}, ops=gpu_pool_only)
        self.assertEqual(entries(row), {(2, ur.HIGH)})

    def test_row_12_reads_every_node_selector_requirement_operator(self):
        # The node test is the API's own predicate: the nodeSelector and any one required term, every
        # expression by its operator. A pod kept off spot nodes by `DoesNotExist` or `NotIn [true]` and
        # refused by its standard pool for `Insufficient cpu` is row 2, the standard node satisfying the
        # term by absence; `Exists` on a key no node carries is row 12; `Gt` and `Lt` compare integers;
        # `matchFields` reads the node's name.
        mixed = "0/3 nodes are available: 1 Insufficient cpu, 2 node(s) didn't match Pod's node affinity/selector. preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling."
        misses = "0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling."
        nodes = copy.deepcopy(READS["seeded-a"]["nodes"])
        nodes[0]["metadata"]["labels"]["tier-rank"] = "3"
        hostname = nodes[0]["metadata"]["name"]

        def with_terms(message, *expressions, fields=()):
            p = pod("api", scheduled_message=message)
            term = {"matchExpressions": list(expressions)}
            if fields:
                term["matchFields"] = list(fields)
            p["spec"]["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [term]}}}
            return p

        cases = (
            ("DoesNotExist", with_terms(mixed, {"key": "cloud.google.com/gke-spot", "operator": "DoesNotExist"}), {(2, ur.HIGH)}),
            ("NotIn", with_terms(mixed, {"key": "cloud.google.com/gke-preemptible", "operator": "NotIn", "values": ["true"]}), {(2, ur.HIGH)}),
            ("Exists absent", with_terms(misses, {"key": "team", "operator": "Exists"}), {(12, ur.MEDIUM)}),
            ("Gt satisfied", with_terms(mixed, {"key": "tier-rank", "operator": "Gt", "values": ["2"]}), {(2, ur.HIGH)}),
            ("Gt unsatisfied", with_terms(misses, {"key": "tier-rank", "operator": "Gt", "values": ["5"]}), {(12, ur.MEDIUM)}),
            ("Lt satisfied", with_terms(mixed, {"key": "tier-rank", "operator": "Lt", "values": ["4"]}), {(2, ur.HIGH)}),
            ("matchFields", with_terms(mixed, fields=({"key": "metadata.name", "operator": "In", "values": [hostname]},)), {(2, ur.HIGH)}),
            ("matchFields miss", with_terms(misses, fields=({"key": "metadata.name", "operator": "In", "values": ["gone-node"]},)), {(12, ur.MEDIUM)}),
        )
        for label, p, expected in cases:
            with self.subTest(label):
                [row] = self.classify(pods=[p], nodes=nodes)
                self.assertEqual(entries(row), expected)
        [row] = self.classify(pods=[with_terms(misses, {"key": "team", "operator": "Exists"})], nodes=nodes)
        self.assertEqual(row["classifications"][0]["detail"], f"no node carries team Exists; {ur.LABEL_NOT_DROPPED_TEXT}")

    def test_entry_12_is_high_only_with_a_sourced_dropped_label_entry(self):
        # The shipped table is empty: no GKE release note or document names a label removal, and
        # kubeadm's `node-role.kubernetes.io/master` is a label no GKE node carried. The high branch is
        # reached only with an entry injected here.
        self.assertEqual(ur.DROPPED_NODE_LABELS, {})
        miss = "0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling."
        master = {"node-role.kubernetes.io/master": ""}
        [row] = self.classify(pods=[pod("on-master", scheduled_message=miss, node_selector=master)])
        self.assertEqual(entries(row), {(12, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["detail"], f"no node carries node-role.kubernetes.io/master=; {ur.LABEL_NOT_DROPPED_TEXT}")
        with mock.patch.dict(ur.DROPPED_NODE_LABELS, {"node-role.kubernetes.io/master": (1, 24)}):
            [row] = self.classify(pods=[pod("on-master", scheduled_message=miss, node_selector=master)])
            self.assertEqual(entries(row), {(12, ur.HIGH)})
            self.assertEqual(row["classifications"][0]["detail"], "no node carries node-role.kubernetes.io/master=; node-role.kubernetes.io/master dropped in 1.24")
            # The clause set decides no row: the pool's own reason beside the misses is named, the
            # grade stands.
            mixed = "0/4 nodes are available: 1 node(s) were unschedulable, 3 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling."
            [row] = self.classify(pods=[pod("on-master", scheduled_message=mixed, node_selector=master)])
            self.assertEqual(entries(row), {(12, ur.HIGH)})
            self.assertEqual(row["classifications"][0]["detail"], "no node carries node-role.kubernetes.io/master=; node-role.kubernetes.io/master dropped in 1.24; " + ur.OWN_POOL_REASON_FORMAT.format(clauses="1 node(s) were unschedulable"))
            # On a cluster below that minor the label is not yet dropped.
            old = cluster_doc("seeded-a")
            old["currentMasterVersion"] = "1.23.17-gke.1700"
            for p in old["nodePools"]:
                p["version"] = "1.23.17-gke.1700"
            reads = {"pods": [pod("on-master", scheduled_message=miss, node_selector=master)], "nodes": READS["seeded-a"]["nodes"], "events": [], "pdbs": [], "owners": []}
            [row] = ur.collect_symptoms(old, reads, ops_for("seeded-a"), SINCE)
            self.assertEqual(entries(row), {(12, ur.MEDIUM)})
            self.assertEqual(row["classifications"][0]["detail"], f"no node carries node-role.kubernetes.io/master=; {ur.LABEL_NOT_DROPPED_TEXT}")

    def test_entry_19_volume_affinity_is_not_entry_12(self):
        [row] = self.classify(pods=[pod("pd-reader", scheduled_message="0/4 nodes are available: 1 node(s) had volume node affinity conflict, 3 node(s) didn't match PersistentVolume's node affinity.")])
        self.assertEqual(entries(row), {(19, ur.HIGH)})

    def test_entry_19_attach_and_mount_events(self):
        rows = self.classify(events=[event("FailedAttachVolume", "AttachVolume.Attach failed for volume pv-1"), event("FailedMount", "MountVolume.WaitForAttach failed for volume pvc-0b7c: timed out waiting for the condition", name="y")])
        self.assertEqual([entries(r) for r in rows], [{(19, ur.MEDIUM)}, {(19, ur.MEDIUM)}])
        self.assertEqual(rows[0]["classifications"][0]["evidence"], f'{ur.TEXT_SOURCE_EVENT} reads "FailedAttachVolume AttachVolume.Attach failed for volume pv-1"')
        self.assertEqual(rows[0]["classifications"][0]["detail"], ur.FROM_EVENT_TEXT_DETAIL.format(source=ur.TEXT_SOURCE_EVENT))
        # The pod the event names sitting Pending is the API server agreeing with the event text; the
        # row's own grade is medium either way, and the detail names what agreed.
        stuck = pod("y", phase="Pending", statuses=[waiting("ContainerCreating")])
        stuck["status"]["startTime"] = "2026-10-08T10:00:00Z"
        [row] = [s for s in self.classify(events=[event("FailedMount", "MountVolume.WaitForAttach failed for volume pvc-0b7c: timed out waiting for the condition", name="y")], pods=[stuck]) if s["category"] == "event"]
        self.assertEqual(entries(row), {(19, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["detail"], "from event text; pod y phase Pending agrees")

    def test_configmap_mount_failure_is_not_entry_19(self):
        [row] = self.classify(events=[event("FailedMount", 'MountVolume.SetUp failed for volume "config" : object "gke-gmp-system"/"collector" not registered')])
        self.assertEqual(entries(row), {(None, ur.MEDIUM)})
        # The kubelet's generic mount-timeout sentence names ConfigMaps too.
        [row] = self.classify(events=[event("FailedMount", "Unable to attach or mount volumes: unmounted volumes=[config], unattached volumes=[config kube-api-access-x]: timed out waiting for the condition")])
        self.assertEqual(entries(row), {(None, ur.MEDIUM)})

    def test_kernel_oomkilling_event_is_charged_to_the_pod_on_that_node(self):
        seeded = symptoms_of("seeded-a")
        self.assertEqual([s for s in seeded if s["reason"] == ur.OOM_NODE_EVENT_REASON], [])
        without_payments = {**READS["seeded-a"], "pods": [p for p in READS["seeded-a"]["pods"] if "payments-api" not in p["metadata"]["name"]]}
        rows = [s for s in symptoms_of("seeded-a", reads=without_payments) if s["reason"] == ur.OOM_NODE_EVENT_REASON]
        self.assertEqual([r["object"] for r in rows], ["Node/gke-seeded-a-default-pool-62ac8ee0-d595"])

    def test_pod_events_implied_by_the_pod_row_are_collapsed(self):
        seeded = symptoms_of("seeded-a")
        self.assertEqual([s for s in seeded if s["category"] == "event" and s["reason"] in ur.EVENT_REASONS_IMPLIED_BY_POD], [])
        self.assertEqual(len(by_object(seeded, INFERENCE)), 1)

    def test_entry_20_image_pull_names_the_host(self):
        failing = pod("legacy-pull", statuses=[waiting("ImagePullBackOff", 'Back-off pulling image "k8s.gcr.io/pause:3.9"')], images=["k8s.gcr.io/pause:3.9"])
        ops = [o for o in ops_for("seeded-a") if "idle-batch-pool" not in o["targetLink"]]
        # Alone: medium, nothing shows the image still pulls on an untouched node.
        [row] = self.classify(pods=[failing], ops=ops)
        self.assertEqual(entries(row), {(20, ur.MEDIUM)})
        self.assertIn("no pod with this image running on an untouched pool", row["classifications"][0]["detail"])
        # The same image Running on the untouched idle-batch-pool: the mechanism, high.
        twin = pod("legacy-pull-old", images=["k8s.gcr.io/pause:3.9"], node="gke-seeded-a-idle-batch-pool-a5fd3288-q4ts")
        twin["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
        [row] = self.classify(pods=[failing, twin], ops=ops)
        self.assertEqual(entries(row), {(20, ur.HIGH)})
        self.assertIn("image host k8s.gcr.io; same image running on an untouched pool", row["classifications"][0]["detail"])
        [row] = self.classify(pods=[pod("hub-pull", statuses=[waiting("ErrImagePull")])])
        self.assertIn(f"image host {ur.DEFAULT_IMAGE_HOST}", row["classifications"][0]["detail"])

    def test_reason_hit_on_a_sidecar_quotes_the_sidecar(self):
        # The main container crash-loops, the sidecar cannot pull: entry 20 on the sidecar's reason,
        # evidenced by the sidecar's own text, not the headline container's.
        statuses = [
            {"name": "main", "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 5m0s restarting failed container=main"}}, "restartCount": 7},
            {"name": "sidecar", "state": {"waiting": {"reason": "ImagePullBackOff", "message": 'Back-off pulling image "k8s.gcr.io/pause:3.9"'}}},
        ]
        [row] = self.classify(pods=[pod("api", containers=2, statuses=statuses, images=["example.com/api:1", "k8s.gcr.io/pause:3.9"])])
        self.assertEqual({c["entry"] for c in row["classifications"]}, {20})
        self.assertIn('ImagePullBackOff Back-off pulling image "k8s.gcr.io/pause:3.9"', row["classifications"][0]["evidence"])
        self.assertNotIn("CrashLoopBackOff", row["classifications"][0]["evidence"])

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

    def test_event_only_scheduling_failure_needs_pool_evidence(self):
        gpu_pool_only = [o for o in ops_for("seeded-a") if "pinned-inference-pool" in o["targetLink"]]
        # The dead pods' events resolve to a Deployment whose template pins default-pool: untouched, medium.
        workloads = copy.deepcopy(READS["seeded-a"]["workloads"])
        payments = next(w for w in workloads if w["metadata"]["name"] == "payments-api")
        payments["spec"]["template"]["spec"]["nodeSelector"] = {"cloud.google.com/gke-nodepool": "default-pool"}
        pods = [p for p in READS["seeded-a"]["pods"] if "payments-api" not in p["metadata"]["name"]]
        events = [event("FailedScheduling", "0/4 nodes are available: 4 Insufficient cpu.", name="payments-api-79b77b8c67-gone1", namespace="seeded-debug")]
        reads = {**READS["seeded-a"], "pods": pods, "workloads": workloads, "events": events}
        [row] = by_object(symptoms_of("seeded-a", reads=reads, ops=gpu_pool_only), PAYMENTS)
        self.assertEqual(entries(row), {(2, ur.MEDIUM)})
        self.assertIn(ur.GATE_CLOSED_TEXT, row["classifications"][0]["detail"])
        # Pinned to the touched pool the gate opens, but the pods are gone and the event is the only
        # text: medium, marked as event text alone.
        payments["spec"]["template"]["spec"]["nodeSelector"] = {"cloud.google.com/gke-nodepool": "pinned-inference-pool"}
        [row] = by_object(symptoms_of("seeded-a", reads=reads, ops=gpu_pool_only), PAYMENTS)
        self.assertEqual(entries(row), {(2, ur.MEDIUM)})
        self.assertNotIn(ur.GATE_CLOSED_TEXT, row["classifications"][0]["detail"])
        self.assertIn(ur.FROM_EVENT_TEXT_DETAIL.format(source=ur.TEXT_SOURCE_EVENT), row["classifications"][0]["detail"])
        self.assertEqual(row["classifications"][0]["evidence_core"], f'{ur.TEXT_SOURCE_EVENT} reads "Insufficient cpu"')
        # No pool evidence at all on an event row: closed.
        payments["spec"]["template"]["spec"].pop("nodeSelector")
        [row] = by_object(symptoms_of("seeded-a", reads=reads, ops=gpu_pool_only), PAYMENTS)
        self.assertEqual(entries(row), {(2, ur.MEDIUM)})
        self.assertIn(ur.GATE_CLOSED_TEXT, row["classifications"][0]["detail"])

    def test_pending_pod_pinned_to_an_untouched_pool_is_medium(self):
        # Friday's rollout touched gpu-pool only; a pod pinned to default-pool that cannot schedule is not its doing.
        gpu_pool_only = copy.deepcopy([o for o in ops_for("seeded-a") if "pinned-inference-pool" in o["targetLink"]])
        pinned = pod("web", scheduled_message="0/4 nodes are available: 4 Insufficient cpu.", node_selector={"cloud.google.com/gke-nodepool": "default-pool"})
        [row] = self.classify(pods=[pinned], ops=gpu_pool_only)
        self.assertEqual(entries(row), {(2, ur.MEDIUM)})
        self.assertIn(ur.GATE_CLOSED_TEXT, row["classifications"][0]["detail"])
        pinned["spec"]["nodeSelector"] = {"cloud.google.com/gke-nodepool": "pinned-inference-pool"}
        [row] = self.classify(pods=[pinned], ops=gpu_pool_only)
        self.assertEqual(entries(row), {(2, ur.HIGH)})
        free = pod("web", scheduled_message="0/4 nodes are available: 4 Insufficient cpu.")
        [row] = self.classify(pods=[free], ops=gpu_pool_only)
        self.assertEqual(entries(row), {(2, ur.HIGH)})

    def test_entry_6_no_matches_for_kind(self):
        rejection = event("FailedCreate", 'error: unable to recognize "manifest.yaml": no matches for kind "FlowSchema" in version "flowcontrol.apiserver.k8s.io/v1beta3"', kind="CronJob")
        [row] = self.classify(events=[rejection])
        self.assertEqual(entries(row), {(6, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["detail"], ur.FROM_EVENT_TEXT_DETAIL.format(source=ur.TEXT_SOURCE_EVENT))
        # The CronJob's Job carries a Failed condition its controller set: the API server agrees.
        job = {"kind": "Job", "metadata": {"name": "x-29300000", "namespace": "apps", "ownerReferences": [{"kind": "CronJob", "name": "x"}]}, "status": {"conditions": [{"type": "Failed", "status": "True"}]}}
        [row] = self.classify(events=[rejection], owners=[job])
        self.assertEqual(entries(row), {(6, ur.HIGH)})
        self.assertEqual(row["classifications"][0]["detail"], "from event text; Job condition Failed=True agrees")

    def test_entry_6_needs_a_job_owner_and_an_error(self):
        [row] = self.classify(pods=[pod("legacy-flowcontrol-caller", phase="Failed", statuses=[{"name": "c0", "state": {"terminated": {"reason": "Error", "exitCode": 1}}}])])
        self.assertEqual(entries(row), {(None, ur.MEDIUM)})

    def test_env_values_never_reach_the_symptom_row(self):
        secret_pod = pod("legacy-flowcontrol-sync-abcde", phase="Failed", owner="Job", statuses=[{"name": "c0", "state": {"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": "2026-10-08T12:00:00Z"}}}])
        secret_pod["spec"]["containers"][0]["env"] = [{"name": "DB_PASSWORD", "value": "hunter2-not-for-the-report"}, {"name": "API", "value": "flowcontrol.apiserver.k8s.io/v1beta3"}]
        [row] = self.classify(pods=[secret_pod])
        self.assertEqual(entries(row), {(6, ur.MEDIUM)})
        self.assertEqual(row["api_markers"], ["flowcontrol", "v1beta"])
        self.assertIn("name and spec mentions flowcontrol, v1beta", row["classifications"][0]["evidence"])
        self.assertNotIn("hunter2", json.dumps(row))
        self.assertNotIn("spec_text", row)

    def test_series_event_is_dated_by_its_last_observation(self):
        stale_first = {"type": "Warning", "reason": "FailedScheduling", "message": "0/2 nodes are available: 1 Insufficient cpu.", "involvedObject": {"kind": "Pod", "name": "x", "namespace": "apps"}, "eventTime": "2026-09-01T00:00:00Z", "series": {"count": 596, "lastObservedTime": "2026-10-08T12:00:00Z"}, "metadata": {"creationTimestamp": "2026-09-01T00:00:00Z"}}
        [row] = self.classify(events=[stale_first])
        self.assertEqual((row["count"], row["last_seen"]), (596, "2026-10-08T12:00:00Z"))
        kept = symptoms_of("seeded-a", reads={**READS["seeded-a"], "events": [stale_first]}, window=datetime(2026, 10, 7, tzinfo=timezone.utc))
        self.assertIn("apps/Pod/x", {s["object"] for s in kept if s["category"] == "event"})

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
        # policy/v1: an empty selector matches every pod in the namespace.
        self.assertTrue(ur._selector_matches({}, labels))
        pdbs = copy.deepcopy(READS["seeded-a"]["pdbs"])
        pdbs[0]["spec"]["selector"] = {"matchExpressions": [{"key": "app", "operator": "In", "values": ["inference-server"]}]}
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "pdbs": pdbs}) if s["category"] == "pdb"]
        self.assertEqual(entries(rows[0]), {(1, ur.HIGH)})

    def test_empty_budget_selector_covers_the_namespace_and_an_absent_one_covers_nothing(self):
        # A namespace-wide "evict nothing" budget: `selector: {}` covers every pod in seeded-capacity,
        # so the drained pool's replicas link it to the hold; a budget with no selector covers none.
        pdbs = copy.deepcopy(READS["seeded-a"]["pdbs"])
        pdbs[0]["spec"]["selector"] = {}
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "pdbs": pdbs}) if s["category"] == "pdb"]
        self.assertEqual([(r["upgraded_pools"], r["message"].split(" pods=")[1].split(" ")[0]) for r in rows], [(["pinned-inference-pool"], str(len([p for p in READS["seeded-a"]["pods"] if p["metadata"]["namespace"] == "seeded-capacity"])))])
        pdbs[0]["spec"].pop("selector")
        self.assertEqual([s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "pdbs": pdbs}) if s["category"] == "pdb"], [])
        # The risk shape reads the same two spellings: an empty selector covers the workloads, an
        # absent one none, so a spec that forbids disruption counts its replicas only with the former.
        pdbs[0]["spec"]["selector"], pdbs[0]["spec"]["maxUnavailable"] = {}, 0
        [shape] = [s for s in ur.budget_shapes(pdbs, READS["seeded-a"]["workloads"]) if s["object"].endswith("PodDisruptionBudget/inference-server")]
        self.assertEqual(shape["confidence"], ur.HIGH)
        self.assertIn(" over ", shape["evidence"])
        pdbs[0]["spec"].pop("selector")
        [shape] = [s for s in ur.budget_shapes(pdbs, READS["seeded-a"]["workloads"]) if s["object"].endswith("PodDisruptionBudget/inference-server")]
        self.assertNotIn(" over ", shape["evidence"])

    def test_entry_1_is_high_on_a_drained_pool_held_or_not(self):
        def metric(op, name, value):
            for m in op["progress"]["metrics"]:
                if m["name"] == name:
                    m["intValue"] = str(value)

        ops = copy.deepcopy(ops_for("seeded-a"))
        quick = next(o for o in ops if "pinned-inference-pool" in o["targetLink"])
        quick["endTime"] = "2026-10-08T04:30:00Z"
        metric(quick, ur.OPERATION_METRIC_PDB_DELAY, 0)
        rows = [s for s in symptoms_of("seeded-a", ops=ops) if s["category"] == "pdb"]
        self.assertEqual(entries(rows[0]), {(1, ur.HIGH)})
        self.assertNotIn("drain held", rows[0]["classifications"][0]["detail"])
        # The captured operation carries GKE's own measure of the hold: read, and named as measured.
        # The budget's spec (maxUnavailable 1) allows disruption; it sits at zero because three of its
        # four replicas are Pending, so the detail and the mitigation point at the pods' own incident.
        downstream = 'budget inference-server: spec {"maxUnavailable": 1} allows disruption; at zero because 3 of 4 covered pod(s) are not ready, the covered pods\' own incident'
        held = [s for s in symptoms_of("seeded-a") if s["category"] == "pdb"]
        self.assertEqual(held[0]["classifications"][0]["detail"], f"{downstream}; drain held by the budget on pinned-inference-pool (NODE_PDB_DELAY_SECONDS 3598 s, measured by GKE)")
        self.assertIn("took 63 min over 1 node(s); NODE_PDB_DELAY_SECONDS 3598 s", held[0]["classifications"][0]["evidence"])
        self.assertEqual((held[0]["budget_downstream"], held[0]["spec_forbids"], held[0]["not_ready"], held[0]["expected_pods"]), (True, False, 3, 4))
        lines = ur.mitigation_lines(held[0], held[0]["classifications"][0])
        self.assertEqual((lines["mitigate_before"], lines["mitigate_after"]), (ur.BUDGET_DOWNSTREAM_MITIGATE_BEFORE, ur.BUDGET_DOWNSTREAM_MITIGATE_AFTER))
        self.assertTrue(lines["before_signal"].endswith(ur.BUDGET_DOWNSTREAM_BEFORE))
        # A spec that forbids disruption keeps the catalogue's row: the budget is the thing to fix.
        strict = copy.deepcopy(READS["seeded-a"]["pdbs"])
        strict[0]["spec"]["maxUnavailable"] = 0
        [row] = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "pdbs": strict}) if s["category"] == "pdb"]
        self.assertEqual((row["budget_downstream"], entries(row)), (False, {(1, ur.HIGH)}))
        self.assertTrue(row["classifications"][0]["detail"].startswith("budget inference-server; drain held by the budget on pinned-inference-pool"))
        self.assertEqual(ur.mitigation_lines(row, row["classifications"][0])["mitigate_before"], ur.MITIGATIONS[1]["mitigate_before"])
        # Over the hour with the metric at zero (a stockout, a slow boot): the budget held nothing.
        stockout = copy.deepcopy(ops_for("seeded-a"))
        metric(next(o for o in stockout if "pinned-inference-pool" in o["targetLink"]), ur.OPERATION_METRIC_PDB_DELAY, 0)
        rows = [s for s in symptoms_of("seeded-a", ops=stockout) if s["category"] == "pdb"]
        self.assertNotIn("drain held", rows[0]["classifications"][0]["detail"])
        # Without metrics the hour-per-node estimate speaks, and says it is an estimate.
        bare = copy.deepcopy(ops_for("seeded-a"))
        next(o for o in bare if "pinned-inference-pool" in o["targetLink"]).pop("progress")
        rows = [s for s in symptoms_of("seeded-a", ops=bare) if s["category"] == "pdb"]
        self.assertEqual(rows[0]["classifications"][0]["detail"], f"{downstream}; drain held by the budget on pinned-inference-pool (estimated from the operation's duration, 63 min over 1 node(s); no progress metrics)")
        self.assertTrue(rows[0]["classifications"][0]["evidence"].endswith(f"over 1 node(s); {ur.NO_PROGRESS_METRICS_TEXT}"))
        # The pool autoscaled since: three nodes now, one upgraded. The metrics count the one.
        nodes = copy.deepcopy(READS["seeded-a"]["nodes"])
        pinned = next(n for n in nodes if n["metadata"]["labels"]["cloud.google.com/gke-nodepool"] == "pinned-inference-pool")
        for suffix in ("new1", "new2"):
            extra = copy.deepcopy(pinned)
            extra["metadata"]["name"] = f"{pinned['metadata']['name']}-{suffix}"
            nodes.append(extra)
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "nodes": nodes}) if s["category"] == "pdb"]
        self.assertIn("took 63 min over 1 node(s); NODE_PDB_DELAY_SECONDS 3598 s", rows[0]["classifications"][0]["evidence"])
        self.assertIn("measured by GKE", rows[0]["classifications"][0]["detail"])
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "nodes": nodes}, ops=bare) if s["category"] == "pdb"]
        self.assertNotIn("drain held", rows[0]["classifications"][0]["detail"])  # the estimate over three nodes does not cross

    def test_entry_14_reads_gate_and_cgroup_mode_from_the_same_replica(self):
        # A sibling OOM-killed on a touched cgroup v1 pool must not lend its drain to the replica on an
        # untouched cgroup v2 pool: the pool that supplies the mode supplies the gate.
        cluster = cluster_doc("seeded-a")
        cluster["nodePools"][0]["config"]["effectiveCgroupMode"] = ur.CGROUP_V1_MODE  # default-pool on v1, touched
        ops = [o for o in ops_for("seeded-a") if "idle-batch-pool" not in o["targetLink"]]  # idle-batch-pool (v2) untouched
        on_v1_touched = pod("jvm-aaaaa", images=["eclipse-temurin:8u302-jre"], statuses=[oom()], node="gke-seeded-a-default-pool-62ac8ee0-d595")
        on_v2_untouched = pod("jvm-zzzzz", images=["eclipse-temurin:8u302-jre"], statuses=[oom()], node="gke-seeded-a-idle-batch-pool-a5fd3288-q4ts")
        for p in (on_v1_touched, on_v2_untouched):
            p["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "jvm-rs"}]
        for first, second in ((on_v1_touched, on_v2_untouched), (on_v2_untouched, on_v1_touched)):
            reads = {"pods": [first, second], "nodes": READS["seeded-a"]["nodes"], "events": [], "pdbs": [], "owners": []}
            [row] = ur.collect_symptoms(cluster, reads, ops, SINCE)
            self.assertEqual(entries(row), {(14, ur.MEDIUM)}, f"order {first['metadata']['name']}, {second['metadata']['name']}")
            self.assertIn("14 or 15 on cgroup v2", row["classifications"][0]["detail"])

    def test_merged_row_reads_every_replicas_containers(self):
        # One replica carries the CUDA driver error, the other a plain exit: entry 18 whatever the order.
        cuda = pod("gpu-aaaaa", statuses=[{"name": "c0", "state": {"terminated": {"reason": "Error", "exitCode": 1, "message": "CUDA Error 803: system has unsupported display driver / cuda driver combination"}}}])
        plain = pod("gpu-zzzzz", statuses=[{"name": "c0", "state": {"terminated": {"reason": "Error", "exitCode": 1, "message": "exit status 1"}}}])
        for p in (cuda, plain):
            p["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "gpu-rs"}]
        for first, second in ((cuda, plain), (plain, cuda)):
            [row] = self.classify(pods=[first, second])
            self.assertEqual(entries(row), {(18, ur.HIGH)}, f"order {first['metadata']['name']}, {second['metadata']['name']}")
            self.assertEqual(len(row["containers"]), 2)
        # And an OOM kill on the second-listed replica is still an OOM row.
        healthy_exit = pod("mem-aaaaa", statuses=[{"name": "c0", "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"reason": "Error", "exitCode": 1}}}])
        killed = pod("mem-zzzzz", images=["eclipse-temurin:8u302-jre"], statuses=[oom()])
        for p in (healthy_exit, killed):
            p["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "mem-rs"}]
        for first, second in ((healthy_exit, killed), (killed, healthy_exit)):
            [row] = self.classify(pods=[first, second])
            self.assertEqual(entries(row), {(14, ur.HIGH)}, f"order {first['metadata']['name']}, {second['metadata']['name']}")

    def test_merged_event_row_dates_from_the_earliest_event(self):
        early = event("Unhealthy", "probe failed", name="web-aaaaa", namespace="apps", last="2026-10-08T12:00:00Z")
        early["firstTimestamp"] = "2026-10-07T06:00:00Z"
        late = event("Unhealthy", "probe failed", name="web-zzzzz", namespace="apps", last="2026-10-08T13:00:00Z")
        late["firstTimestamp"] = "2026-10-08T12:30:00Z"
        rs = {"kind": "ReplicaSet", "metadata": {"name": "web-rs", "namespace": "apps", "ownerReferences": [{"kind": "Deployment", "name": "web"}]}}
        pods = [pod("web-aaaaa", namespace="apps"), pod("web-zzzzz", namespace="apps")]
        for p in pods:
            p["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "web-rs"}]
            p["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
            p["status"]["phase"] = "Running"
        for first, second in ((early, late), (late, early)):
            reads = {"pods": pods, "nodes": READS["seeded-a"]["nodes"], "events": [first, second], "pdbs": [], "owners": [rs]}
            [row] = ur.collect_symptoms(cluster_doc("seeded-a"), reads, ops_for("seeded-a"), SINCE)
            self.assertEqual((row["object"], row["first_seen"], row["onset"], row["last_seen"]), ("apps/Deployment/web", "2026-10-07T06:00:00Z", "2026-10-07T06:00:00Z", "2026-10-08T13:00:00Z"))

    def test_pod_caught_mid_start_is_not_a_symptom(self):
        young = pod("fresh", statuses=[{"name": "c0", "state": {"waiting": {"reason": "ContainerCreating"}}, "restartCount": 0}])
        young["status"]["startTime"] = "2026-10-08T17:55:00Z"
        young["status"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": "2026-10-08T17:55:00Z"}]
        starting: list[str] = []
        rows = ur.collect_symptoms(cluster_doc("seeded-a"), {"pods": [young], "nodes": READS["seeded-a"]["nodes"], "events": [], "pdbs": [], "owners": []}, ops_for("seeded-a"), SINCE, now=NOW, starting=starting)
        self.assertEqual((rows, starting), ([], ["apps/fresh"]))
        # Older than the grace, or restarted, it is a symptom.
        young["status"]["startTime"] = "2026-10-08T17:00:00Z"
        rows = ur.collect_symptoms(cluster_doc("seeded-a"), {"pods": [young], "nodes": READS["seeded-a"]["nodes"], "events": [], "pdbs": [], "owners": []}, ops_for("seeded-a"), SINCE, now=NOW)
        self.assertEqual([r["reason"] for r in rows], ["ContainerCreating"])
        young["status"]["startTime"] = "2026-10-08T17:55:00Z"
        young["status"]["containerStatuses"][0]["restartCount"] = 2
        rows = ur.collect_symptoms(cluster_doc("seeded-a"), {"pods": [young], "nodes": READS["seeded-a"]["nodes"], "events": [], "pdbs": [], "owners": []}, ops_for("seeded-a"), SINCE, now=NOW)
        self.assertEqual(len(rows), 1)
        # Bound, with no container status yet (the kubelet has not reported): starting inside the
        # grace, a bare `Pending` symptom past it.
        bound = pod("bound", phase="Pending", statuses=[])
        bound["metadata"]["creationTimestamp"] = "2026-10-08T17:55:00Z"
        bound["status"]["conditions"] = [{"type": "PodScheduled", "status": "True"}]
        starting = []
        rows = ur.collect_symptoms(cluster_doc("seeded-a"), {"pods": [bound], "nodes": READS["seeded-a"]["nodes"], "events": [], "pdbs": [], "owners": []}, ops_for("seeded-a"), SINCE, now=NOW, starting=starting)
        self.assertEqual((rows, starting), ([], ["apps/bound"]))
        bound["metadata"]["creationTimestamp"] = "2026-10-08T17:00:00Z"
        rows = ur.collect_symptoms(cluster_doc("seeded-a"), {"pods": [bound], "nodes": READS["seeded-a"]["nodes"], "events": [], "pdbs": [], "owners": []}, ops_for("seeded-a"), SINCE, now=NOW)
        self.assertEqual([r["reason"] for r in rows], ["Pending"])
        # A pod held by scheduling gates (a queue admission controller's) waits for admission at any
        # age: no row, no guard, listed as gated; nor does it stand as a failing pod for event text.
        queued = pod("queued", phase="Pending", statuses=[])
        queued["metadata"]["creationTimestamp"] = "2026-10-01T00:00:00Z"
        queued["spec"]["schedulingGates"] = [{"name": "kueue.x-k8s.io/admission"}]
        queued["status"]["conditions"] = [{"type": "PodScheduled", "status": "False", "reason": "SchedulingGated", "message": "Scheduling is blocked due to non-empty scheduling gates"}]
        gated = []
        rows = ur.collect_symptoms(cluster_doc("seeded-a"), {"pods": [queued], "nodes": READS["seeded-a"]["nodes"], "events": [], "pdbs": [], "owners": []}, ops_for("seeded-a"), SINCE, now=NOW, gated=gated)
        self.assertEqual((rows, gated), ([], ["apps/queued"]))
        resolver = ur.Resolver([queued], [], [])
        self.assertEqual(ur._api_set_failures([queued], [], resolver), ({}, {}))

    def test_pending_pod_preference_on_a_custom_label_gates_on_that_pool(self):
        # inference-server pins `seeded-role: pinned-inference`, a label only pinned-inference-pool's node carries.
        pools_only_default = [o for o in ops_for("seeded-a") if "/nodePools/default-pool" in o["targetLink"]]
        rows = by_object(symptoms_of("seeded-a", ops=pools_only_default), INFERENCE, "pending")
        self.assertEqual({(c["entry"], c["confidence"]) for c in rows[0]["classifications"]}, {(2, ur.MEDIUM)})
        pinned_only = [o for o in ops_for("seeded-a") if "/nodePools/pinned-inference-pool" in o["targetLink"]]
        rows = by_object(symptoms_of("seeded-a", ops=pinned_only), INFERENCE, "pending")
        self.assertIn((2, ur.HIGH), {(c["entry"], c["confidence"]) for c in rows[0]["classifications"]})
        labels = ur.pool_labels(cluster_doc("seeded-a"), READS["seeded-a"]["nodes"])
        self.assertEqual(labels[("seeded-role", "pinned-inference")], {"pinned-inference-pool"})
        self.assertEqual(ur._selector_pools({"seeded-role": "pinned-inference"}, labels), {"pinned-inference-pool"})
        self.assertEqual(ur._selector_pools({"no-such-label": "x"}, labels), set())
        # A label every node carries pins nothing, also beside a labelled pool at zero nodes.
        self.assertEqual(ur._selector_pools({"kubernetes.io/os": "linux"}, labels), set())
        idle = cluster_doc("seeded-a")
        idle["nodePools"].append({"name": "burst-pool", "version": "1.35.8-gke.1380001", "config": {"labels": {"workload": "burst"}}})
        with_idle = ur.pool_labels(idle, READS["seeded-a"]["nodes"])
        self.assertEqual(with_idle[("workload", "burst")], {"burst-pool"})
        self.assertEqual(ur._selector_pools({"kubernetes.io/os": "linux"}, with_idle), set())
        self.assertEqual(ur._selector_pools({"workload": "burst"}, with_idle), {"burst-pool"})
        # One pool: a pin by the pool label itself counts (it is a preference by definition), a
        # label every node carries still pins nothing.
        one = {("cloud.google.com/gke-nodepool", "default-pool"): {"default-pool"}, ("kubernetes.io/os", "linux"): {"default-pool"}, ("seeded-role", "web"): {"default-pool"}}
        self.assertEqual(ur._pinned_pools([{"cloud.google.com/gke-nodepool": ["default-pool"]}], one), {"default-pool"})
        self.assertEqual(ur._pinned_pools([{"kubernetes.io/os": ["linux"]}], one), set())
        self.assertEqual(ur._pinned_pools([{"seeded-role": ["web"]}], one), set())
        nodes = READS["seeded-a"]["nodes"]
        # The same pin spelled as a required nodeAffinity term, on the pool label or on the custom
        # label: read like the selector, so only the pinned pool's operation opens the gate.
        for key, value in (("cloud.google.com/gke-nodepool", "pinned-inference-pool"), ("seeded-role", "pinned-inference")):
            gpu = pod("trainer", scheduled_message="0/4 nodes are available: 1 Insufficient nvidia.com/gpu, 3 node(s) didn't match Pod's node affinity/selector.")
            gpu["spec"]["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": key, "operator": "In", "values": [value]}]}]}}}
            self.assertEqual(ur.pod_pins(gpu["spec"]), [{key: [value]}])
            [row] = symptoms_of("seeded-a", reads={"pods": [gpu], "nodes": nodes, "events": [], "pdbs": []}, ops=pools_only_default)
            self.assertEqual(entries(row), {(18, ur.MEDIUM)}, key)
            self.assertIn(ur.GATE_CLOSED_TEXT, row["classifications"][0]["detail"])
            [row] = symptoms_of("seeded-a", reads={"pods": [gpu], "nodes": nodes, "events": [], "pdbs": []}, ops=pinned_only)
            self.assertEqual(entries(row), {(18, ur.HIGH)}, key)
        # Several terms are alternatives; a term's expressions and the nodeSelector are conjoined.
        spec = {"nodeSelector": {"seeded-role": "pinned-inference"}, "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "cloud.google.com/gke-nodepool", "operator": "In", "values": ["default-pool", "idle-batch-pool"]}]}, {"matchExpressions": [{"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64"]}, {"key": "x", "operator": "Exists"}]}]}}}}
        self.assertEqual(ur.pod_pins(spec), [{"cloud.google.com/gke-nodepool": ["default-pool", "idle-batch-pool"], "seeded-role": ["pinned-inference"]}, {"kubernetes.io/arch": ["amd64"], "seeded-role": ["pinned-inference"]}])
        self.assertEqual(ur._pinned_pools(ur.pod_pins(spec), labels), {"pinned-inference-pool"})
        # A pod pinned only by a label every node carries counts any upgraded pool, as an unpinned one.
        plain = pod("web", scheduled_message="0/4 nodes are available: 4 Insufficient cpu.", node_selector={"kubernetes.io/os": "linux"})
        [row] = symptoms_of("seeded-a", reads={"pods": [plain], "nodes": nodes, "events": [], "pdbs": []}, ops=pools_only_default)
        self.assertEqual(entries(row), {(2, ur.HIGH)})
        # An event charged to an owner whose template pins through affinity reads the owner's pin.
        workloads = copy.deepcopy(READS["seeded-a"]["workloads"])
        payments = next(w for w in workloads if w["metadata"]["name"] == "payments-api")
        payments["spec"]["template"]["spec"]["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "cloud.google.com/gke-nodepool", "operator": "In", "values": ["pinned-inference-pool"]}]}]}}}
        pods = [p for p in READS["seeded-a"]["pods"] if "payments-api" not in p["metadata"]["name"]]
        events = [event("FailedScheduling", "0/4 nodes are available: 4 Insufficient cpu.", name="payments-api-79b77b8c67-gone1", namespace="seeded-debug")]
        [row] = by_object(symptoms_of("seeded-a", reads={**READS["seeded-a"], "pods": pods, "workloads": workloads, "events": events}, ops=pools_only_default), PAYMENTS)
        self.assertIn(ur.GATE_CLOSED_TEXT, row["classifications"][0]["detail"])
        self.assertEqual((entries(row), row["node_pins"]), ({(2, ur.MEDIUM)}, [{"cloud.google.com/gke-nodepool": ["pinned-inference-pool"]}]))

    def test_single_pool_cluster_keeps_an_explicit_pool_pin(self):
        # seeded-a reduced to default-pool: nodes, operations and pods pinned by the pool label.
        nodes = [n for n in READS["seeded-a"]["nodes"] if n["metadata"]["labels"]["cloud.google.com/gke-nodepool"] == "default-pool"]
        cluster = cluster_doc("seeded-a")
        cluster["nodePools"] = [p for p in cluster["nodePools"] if p["name"] == "default-pool"]
        ops = [o for o in ops_for("seeded-a") if "/nodePools/default-pool" in o["targetLink"] or o["operationType"] == "UPGRADE_MASTER"]
        labels = ur.pool_labels(cluster, nodes)
        self.assertEqual(ur._pinned_pools([{"cloud.google.com/gke-nodepool": ["default-pool"]}], labels), {"default-pool"})
        # (a) A zero-allowance budget whose covered pods are node-less replicas created after the
        # drain ended, pinned by the pool label: linked to the pool, the hold filed.
        pods = []
        for i in range(2):
            replica = pod(f"web-6d5f8b7c9-r{i}", namespace="seeded-capacity", scheduled_message="0/2 nodes are available: 2 Insufficient cpu.", node_selector={"cloud.google.com/gke-nodepool": "default-pool"})
            replica["metadata"]["labels"] = {"app": "inference-server"}
            replica["metadata"]["creationTimestamp"] = "2026-10-07T05:00:00Z"
            pods.append(replica)
        reads = {"pods": pods, "nodes": nodes, "events": [], "pdbs": READS["seeded-a"]["pdbs"], "owners": [], "workloads": []}
        rows = [s for s in ur.collect_symptoms(cluster, reads, ops, SINCE) if s["category"] == "pdb"]
        self.assertEqual([(r["pools"], r["upgraded_pools"]) for r in rows], [(["default-pool"], ["default-pool"])])
        # (b) An event row carrying its owner's template pin by the pool label: the gate opens when
        # that pool was upgraded, and stays closed when only the control plane was.
        workloads = copy.deepcopy([w for w in READS["seeded-a"]["workloads"] if w["metadata"]["name"] == "payments-api"])
        workloads[0]["spec"]["template"]["spec"]["nodeSelector"] = {"cloud.google.com/gke-nodepool": "default-pool"}
        events = [event("FailedScheduling", "0/2 nodes are available: 2 Insufficient cpu.", name="payments-api-79b77b8c67-gone1", namespace="seeded-debug")]
        reads = {"pods": [], "nodes": nodes, "events": events, "pdbs": [], "owners": READS["seeded-a"]["owners"], "workloads": workloads}
        [row] = by_object(ur.collect_symptoms(cluster, reads, ops, SINCE), PAYMENTS)
        self.assertNotIn(ur.GATE_CLOSED_TEXT, row["classifications"][0]["detail"])
        [row] = by_object(ur.collect_symptoms(cluster, reads, [o for o in ops if o["operationType"] == "UPGRADE_MASTER"], SINCE), PAYMENTS)
        self.assertIn(ur.GATE_CLOSED_TEXT, row["classifications"][0]["detail"])
        # (c) An injected dropped-label entry grades against the pinned pool's minor, not the control
        # plane's: the pool at 1.23 has not dropped a label lost at 1.24.
        old = copy.deepcopy(cluster)
        old["nodePools"][0]["version"] = "1.23.17-gke.1700"
        reads = {"pods": [pod("on-master", scheduled_message="0/2 nodes are available: 2 node(s) didn't match Pod's node affinity/selector.", node_selector={"node-role.kubernetes.io/master": "", "cloud.google.com/gke-nodepool": "default-pool"})], "nodes": nodes, "events": [], "pdbs": [], "owners": []}
        with mock.patch.dict(ur.DROPPED_NODE_LABELS, {"node-role.kubernetes.io/master": (1, 24)}):
            [row] = ur.collect_symptoms(old, reads, ops, SINCE)
        self.assertEqual(entries(row), {(12, ur.MEDIUM)})
        self.assertEqual(row["classifications"][0]["detail"], f"no node carries cloud.google.com/gke-nodepool=default-pool,node-role.kubernetes.io/master=; {ur.LABEL_NOT_DROPPED_TEXT}")

    def test_cluster_universal_selector_does_not_link_a_budget_to_every_pool(self):
        # Every covered replica unplaced and pinned only by `kubernetes.io/os: linux`, the template too:
        # the label names every pool, so it names none, and the drain that was running when the
        # displaced replicas were created is the budget's pool evidence.
        pods = copy.deepcopy(READS["seeded-a"]["pods"])
        for p in pods:
            if p["metadata"]["name"].startswith("inference-server"):
                p["spec"]["nodeSelector"], p["spec"]["nodeName"] = {"kubernetes.io/os": "linux"}, None
        workloads = copy.deepcopy(READS["seeded-a"]["workloads"])
        next(w for w in workloads if w["metadata"]["name"] == "inference-server")["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/os": "linux"}
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "pods": pods, "workloads": workloads}) if s["category"] == "pdb"]
        self.assertEqual([(r["pools"], r["upgraded_pools"]) for r in rows], [(["pinned-inference-pool"], ["pinned-inference-pool"])])
        self.assertNotIn("default-pool", rows[0]["classifications"][0]["evidence"])
        # The same with a labelled pool at zero nodes in the cluster record.
        idle = cluster_doc("seeded-a")
        idle["nodePools"].append({"name": "burst-pool", "version": "1.35.8-gke.1380001", "config": {"labels": {"workload": "burst"}}})
        rows = [s for s in ur.collect_symptoms(idle, {**READS["seeded-a"], "pods": pods, "workloads": workloads}, ops_for("seeded-a"), SINCE) if s["category"] == "pdb"]
        self.assertEqual([r["upgraded_pools"] for r in rows], [["pinned-inference-pool"]])


    def test_pre_existing_sibling_proves_an_ambiguous_rows_age(self):
        # One replica crash-looping on an untouched pool since before the window, one recreated by the
        # default-pool drain on a touched cgroup v2 node: the row predates the upgrade, whatever the order.
        ops = [o for o in ops_for("seeded-a") if "idle-batch-pool" not in o["targetLink"]]
        old = pod("jvm-aaaaa", images=["eclipse-temurin:8u302-jre"], statuses=[{"name": "c0", "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-10-08T17:00:00Z"}}, "restartCount": 40}], node="gke-seeded-a-idle-batch-pool-a5fd3288-q4ts")
        old["status"]["startTime"] = "2026-09-20T00:00:00Z"
        old["metadata"]["creationTimestamp"] = "2026-09-20T00:00:00Z"
        old["status"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": "2026-10-08T17:00:01Z"}]
        new = pod("jvm-zzzzz", images=["eclipse-temurin:8u302-jre"], statuses=[{"name": "c0", "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-10-08T17:30:00Z"}}, "restartCount": 12}], node="gke-seeded-a-default-pool-62ac8ee0-d595")
        new["status"]["startTime"] = "2026-10-07T04:05:00Z"
        new["metadata"]["creationTimestamp"] = "2026-10-07T04:05:00Z"
        new["status"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": "2026-10-08T17:30:01Z"}]
        for p in (old, new):
            p["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "jvm-rs"}]
        first_op = datetime(2026, 10, 6, 4, 22, 12, tzinfo=timezone.utc)
        for first, second in ((old, new), (new, old)):
            reads = {"pods": [first, second], "nodes": READS["seeded-a"]["nodes"], "events": [], "pdbs": [], "owners": []}
            [row] = ur.collect_symptoms(cluster_doc("seeded-a"), reads, ops, SINCE)
            for baseline in (None, []):
                ur.mark_since([row], baseline, first_op)
                self.assertEqual((row["new_pods"], row["pre_existing_pods"], row["predates_upgrade"], row.get("recreated_only")), (["jvm-zzzzz"], ["jvm-aaaaa"], True, None), f"order {first['metadata']['name']} first, baseline {baseline!r}")
                self.assertIn("; 1 new since the operation (e.g. jvm-zzzzz)", row["classifications"][0]["evidence"])
                self.assertIn("; 1 pre-existing since 2026-09-20T00:00:00Z (e.g. jvm-aaaaa)", row["classifications"][0]["evidence"])

    def test_entry_3_every_replica_unavailable_during_one_pool_operation(self):
        # inventory-api's one replica went unavailable inside the default-pool drain (04:02 to 04:12 on
        # 10-07) and sits on a default-pool node: entry 3, high, an Error, keyed by the operation.
        workloads = copy.deepcopy(READS["seeded-a"]["workloads"])
        inventory = next(w for w in workloads if w["metadata"]["name"] == "inventory-api")
        for cond in inventory["status"]["conditions"]:
            if cond["type"] == "Available":
                cond["lastTransitionTime"], cond["message"] = "2026-10-07T04:08:00Z", "Deployment does not have minimum availability."
        pods = copy.deepcopy(READS["seeded-a"]["pods"])
        replica = next(p for p in pods if p["metadata"]["name"].startswith("inventory-api-"))
        replica["spec"]["nodeName"] = "gke-seeded-a-default-pool-62ac8ee0-d595"
        reads = {**READS["seeded-a"], "workloads": workloads, "pods": pods}
        # The catalogue's floor: a workload of one replica is never entry 3 (its displaced replica
        # is entry 2, or its budget entry 1); the fixture Deployment has one, so the test gives it two.
        inventory["spec"]["replicas"] = 1
        self.assertEqual([s for s in symptoms_of("seeded-a", reads=reads) if s["category"] == ur.CATEGORY_UNAVAILABLE], [])
        # Nor is it entry 3 when the drain's evicted pod is still listed beside its Pending replacement
        # (Terminating under its grace period, or Failed and not yet collected): the floor is the spec's
        # count, never the pod list's length.
        leftover = copy.deepcopy(replica)
        leftover["metadata"]["name"] = replica["metadata"]["name"] + "-old"
        leftover["metadata"]["deletionTimestamp"] = "2026-10-07T04:05:00Z"
        leftover["status"]["phase"] = "Failed"
        pods.append(leftover)
        self.assertEqual([s for s in symptoms_of("seeded-a", reads=reads) if s["category"] == ur.CATEGORY_UNAVAILABLE], [])
        pods.remove(leftover)
        inventory["spec"]["replicas"] = 2
        [row] = [s for s in symptoms_of("seeded-a", reads=reads) if s["category"] == ur.CATEGORY_UNAVAILABLE]
        self.assertEqual((row["object"], row["pool"], row["all_in_pool"], row["pods"]), ("seeded-stall/Deployment/inventory-api", "default-pool", True, [replica["metadata"]["name"]]))
        self.assertEqual(entries(row), {(3, ur.HIGH)})
        self.assertEqual(row["classifications"][0]["evidence"], "Available=False since 2026-10-07T04:08:00Z during UPGRADE_NODES operation-1791345747420-340e78e6-3868-4267-a04b-9db624275fa3 on default-pool (2026-10-07T04:02:27Z to 2026-10-07T04:12:09Z); all 1 pod(s) on pool default-pool")
        self.assertEqual(row["classifications"][0]["detail"], ur.SPREAD_POOL_DETAIL)
        self.assertIn("|op=operation-1791345747420", ur.symptom_key(row))
        self.assertEqual(ur._symptom_severity(row), ur.SEVERITY_ERROR)
        # On the untouched idle-batch-pool node in the same zone: the drained zone held every
        # replica, the pool did not; medium.
        replica["spec"]["nodeName"] = "gke-seeded-a-idle-batch-pool-a5fd3288-q4ts"
        [row] = [s for s in symptoms_of("seeded-a", reads=reads) if s["category"] == ur.CATEGORY_UNAVAILABLE]
        self.assertEqual(entries(row), {(3, ur.MEDIUM)})
        self.assertTrue(row["classifications"][0]["evidence"].endswith("; all 1 pod(s) in zone us-central1-a (pools idle-batch-pool)"))
        # In another zone: not this operation's doing.
        nodes = copy.deepcopy(READS["seeded-a"]["nodes"])
        elsewhere = copy.deepcopy(nodes[0])
        elsewhere["metadata"]["name"] = "gke-seeded-a-far-pool-00000000-zzzz"
        elsewhere["metadata"]["labels"]["cloud.google.com/gke-nodepool"] = "far-pool"
        elsewhere["metadata"]["labels"]["topology.kubernetes.io/zone"] = "us-central1-b"
        replica["spec"]["nodeName"] = elsewhere["metadata"]["name"]
        self.assertEqual([s for s in symptoms_of("seeded-a", reads={**reads, "nodes": nodes + [elsewhere]}) if s["category"] == ur.CATEGORY_UNAVAILABLE], [])
        # A transition outside every pool operation's span is not entry 3.
        replica["spec"]["nodeName"] = "gke-seeded-a-default-pool-62ac8ee0-d595"
        for cond in inventory["status"]["conditions"]:
            if cond["type"] == "Available":
                cond["lastTransitionTime"] = "2026-10-07T18:27:18Z"
        self.assertEqual([s for s in symptoms_of("seeded-a", reads=reads) if s["category"] == ur.CATEGORY_UNAVAILABLE], [])
        # A StatefulSet carries no condition: no ready replica, every pod recreated inside the span.
        ledger_db = {"kind": "StatefulSet", "metadata": {"name": "ledger-db", "namespace": "seeded-stall"}, "spec": {"template": {"spec": {"containers": [{"name": "db", "image": "postgres:16"}]}}}, "status": {"replicas": 2, "readyReplicas": 0}}
        members = []
        for ordinal in (0, 1):
            member = pod(f"ledger-db-{ordinal}", namespace="seeded-stall", statuses=[waiting("CrashLoopBackOff")])
            member["metadata"]["ownerReferences"] = [{"kind": "StatefulSet", "name": "ledger-db"}]
            member["metadata"]["creationTimestamp"] = member["status"]["startTime"] = "2026-10-07T04:05:00Z"
            members.append(member)
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "workloads": [ledger_db], "pods": members}) if s["category"] == ur.CATEGORY_UNAVAILABLE]
        self.assertEqual([(r["object"], entries(r), r["onset"]) for r in rows], [("seeded-stall/StatefulSet/ledger-db", {(3, ur.HIGH)}, "2026-10-07T04:05:00Z")])
        # One member evicted and still Pending with no node: the drain running when it was created
        # places it on the pool, so every replica still counts as on the drained pool.
        members[1]["spec"]["nodeName"] = None
        members[1]["status"] = {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "0/4 nodes are available: 4 Insufficient cpu."}], "containerStatuses": []}
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "workloads": [ledger_db], "pods": members}) if s["category"] == ur.CATEGORY_UNAVAILABLE]
        self.assertEqual([(entries(r), r["all_in_pool"]) for r in rows], [({(3, ur.HIGH)}, True)])
        # The same displaced replica for the Deployment leg.
        for cond in inventory["status"]["conditions"]:
            if cond["type"] == "Available":
                cond["lastTransitionTime"] = "2026-10-07T04:08:00Z"
        replica["spec"]["nodeName"] = None
        replica["metadata"]["creationTimestamp"] = "2026-10-07T04:06:00Z"
        replica["status"] = {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "0/4 nodes are available: 4 Insufficient cpu."}], "containerStatuses": []}
        rows = [s for s in symptoms_of("seeded-a", reads=reads) if s["category"] == ur.CATEGORY_UNAVAILABLE]
        self.assertEqual([(r["object"], entries(r)) for r in rows], [("seeded-stall/Deployment/inventory-api", {(3, ur.HIGH)})])

    def test_entry_5_a_pool_two_minors_behind_for_seven_days(self):
        # Row 5 is graded on the skew's own age, from the run that first recorded it, with no window
        # arithmetic: a daily window grades as a weekly one does; inside seven days, under an open
        # window or an active exclusion, it is Info; a cluster with no policy is never graded; one
        # pool on two kubelet versions is no row (an open operation holds the cluster, a finished one
        # leaves one version).
        gemma = cluster_doc("gemma-gpu-upgraded")
        gemma["nodePools"][0]["version"] = "1.30.5-gke.1000000"  # default-pool two minors behind the control plane
        reads = READS["gemma-gpu-upgraded"]
        skew_text = "pool default-pool at 1.30.5-gke.1000000, control plane at 1.32.13-gke.2427000 (2 minors apart)"
        key = "NodePool/default-pool|version-skew|version skew"
        old = {key: "2026-09-20T18:00:00Z"}
        # The exclusion is active at NOW: held, whatever the age.
        reading = ur.version_skew(gemma, NOW, old)
        self.assertEqual((reading["skews"], reading["state"], reading["standing"][key], reading["overdue"]), ([("default-pool", skew_text)], "exclusion hold-gpu-minor (NO_MINOR_UPGRADES) is active until 2026-10-21T00:00:00Z", True, False))
        self.assertEqual([s for s in ur.collect_symptoms(gemma, reads, [], SINCE, now=NOW, skew_first_seen=old) if s["category"] == ur.CATEGORY_VERSION_SKEW], [])
        # The exclusion over and the skew recorded 32 days before: a row, medium, with its age.
        later = datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)
        [row] = [s for s in ur.collect_symptoms(gemma, reads, [], SINCE, now=later, skew_first_seen=old) if s["category"] == ur.CATEGORY_VERSION_SKEW]
        self.assertEqual(entries(row), {(5, ur.MEDIUM)})
        self.assertEqual((row["object"], row["system"], row["classifications"][0]["evidence"], row["classifications"][0]["detail"]), ("NodePool/default-pool", True, skew_text, ur.SKEW_AGE_FORMAT.format(days=32, since="2026-09-20T18:00:00Z")))
        self.assertEqual(ur.symptom_key(row), key)
        # Inside the open daily window (03:00-07:00) it is held even when old.
        at_open = datetime(2026, 10, 22, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(ur.version_skew(gemma, at_open, old)["state"], ur.WINDOW_OPEN_STATE)
        self.assertEqual([s for s in ur.collect_symptoms(gemma, reads, [], SINCE, now=at_open, skew_first_seen=old) if s["category"] == ur.CATEGORY_VERSION_SKEW], [])
        # First recorded this run, or six days ago: Info, not graded; at seven days: graded.
        for first_seen, graded in ((None, False), ({key: "2026-10-16T18:00:00Z"}, False), ({key: "2026-10-15T18:00:00Z"}, True)):
            with self.subTest(first_seen=first_seen):
                reading = ur.version_skew(gemma, later, first_seen)
                self.assertEqual((reading["standing"][key], reading["overdue"]), (graded, graded))
                self.assertEqual(len([s for s in ur.collect_symptoms(gemma, reads, [], SINCE, now=later, skew_first_seen=first_seen) if s["category"] == ur.CATEGORY_VERSION_SKEW]), int(graded))
        self.assertEqual(ur.version_skew(gemma, later)["ages"][key], ur.SKEW_AGE_FIRST_FORMAT)
        self.assertEqual(ur.skew_keys(gemma, later), [key])
        # Two kubelet versions within one pool is no row: the describe shows one pool version.
        seeded = cluster_doc("seeded-a")
        nodes = copy.deepcopy(READS["seeded-a"]["nodes"])
        nodes[0]["status"]["nodeInfo"]["kubeletVersion"] = "v1.34.1-gke.1000000"
        self.assertEqual([s for s in ur.collect_symptoms(seeded, {**READS["seeded-a"], "nodes": nodes}, ops_for("seeded-a"), SINCE, now=NOW) if s["category"] == ur.CATEGORY_VERSION_SKEW], [])
        self.assertEqual(ur.version_skew(seeded, NOW)["skews"], [])
        # No maintenance policy: never row 5, however old; the stored set still carries the key.
        bare = cluster_doc("gemma-gpu-upgraded")
        bare.pop("maintenancePolicy")
        bare["nodePools"][0]["version"] = "1.30.5-gke.1000000"
        reading = ur.version_skew(bare, later, old)
        self.assertEqual((reading["overdue"], reading["no_policy"], reading["keys"]), (False, True, [key]))
        self.assertEqual([s for s in ur.collect_symptoms(bare, reads, [], SINCE, now=later, skew_first_seen=old) if s["category"] == ur.CATEGORY_VERSION_SKEW], [])

    def test_entry_14_gates_on_the_oom_pods_own_pool(self):
        # idle-batch-pool (cgroup v2) was not touched this week; default-pool was. The OOM pod on the untouched pool is medium.
        ops = [o for o in ops_for("seeded-a") if "idle-batch-pool" not in o["targetLink"]]
        untouched = pod("jvm", images=["eclipse-temurin:8u302-jre"], statuses=[oom()], node="gke-seeded-a-idle-batch-pool-a5fd3288-q4ts")
        [row] = self.classify(pods=[untouched], ops=ops)
        self.assertEqual(entries(row), {(14, ur.MEDIUM)})
        touched = pod("jvm", images=["eclipse-temurin:8u302-jre"], statuses=[oom()], node="gke-seeded-a-default-pool-62ac8ee0-d595")
        [row] = self.classify(pods=[touched], ops=ops)
        self.assertEqual(entries(row), {(14, ur.HIGH)})
        nodeless = pod("jvm", images=["eclipse-temurin:8u302-jre"], statuses=[oom()], node="unknown-node")
        [row] = self.classify(pods=[nodeless], ops=ops)
        self.assertEqual(entries(row), {(14, ur.MEDIUM)})  # cgroup mode unknown: 14 or 15, medium

    def test_kernel_oomkilling_collapses_against_every_replica_node(self):
        second_node = "gke-seeded-a-default-pool-62ac8ee0-mbzp"
        twin = copy.deepcopy(next(p for p in READS["seeded-a"]["pods"] if p["metadata"]["name"] == "payments-api-79b77b8c67-vfrh9"))
        twin["metadata"]["name"] = "payments-api-79b77b8c67-zzzzz"
        twin["spec"]["nodeName"] = second_node
        kill = copy.deepcopy(next(e for e in READS["seeded-a"]["events"] if e["reason"] == "OOMKilling"))
        kill["involvedObject"]["name"] = second_node
        kill["metadata"]["name"] = "second-node-kill"
        reads = {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [twin], "events": READS["seeded-a"]["events"] + [kill]}
        symptoms = symptoms_of("seeded-a", reads=reads)
        self.assertEqual([s for s in symptoms if s["reason"] == ur.OOM_NODE_EVENT_REASON], [])
        row = next(s for s in symptoms if s["object"] == PAYMENTS)
        self.assertEqual(set(row["oom_nodes"].values()), {"gke-seeded-a-default-pool-62ac8ee0-d595", second_node})

    def test_entry_1_names_the_held_pool_among_several_touched(self):
        # Spread the budget's pods over default-pool (drained in 9 min) and pinned-inference-pool (held 63 min).
        pods = copy.deepcopy(READS["seeded-a"]["pods"])
        running = next(p for p in pods if p["metadata"]["name"] == "inference-server-778b78fdb8-txlv7")
        extra = copy.deepcopy(running)
        extra["metadata"]["name"] = "inference-server-778b78fdb8-extra"
        extra["spec"]["nodeName"] = "gke-seeded-a-default-pool-62ac8ee0-d595"
        rows = [s for s in symptoms_of("seeded-a", reads={**READS["seeded-a"], "pods": pods + [extra]}) if s["category"] == "pdb"]
        self.assertEqual(rows[0]["upgraded_pools"], ["default-pool", "pinned-inference-pool"])
        [c] = rows[0]["classifications"]
        self.assertEqual((c["entry"], c["confidence"]), (1, ur.HIGH))
        self.assertIn("pods on default-pool, pinned-inference-pool; UPGRADE_NODES operation-1791433235916-c1860f09-e11c-4fe1-b75b-40cf815a7e81 on pinned-inference-pool took 63 min", c["evidence"])
        self.assertIn("drain held by the budget on pinned-inference-pool (NODE_PDB_DELAY_SECONDS 3598 s, measured by GKE)", c["detail"])

    def test_timestamps_survive_under_their_own_keys(self):
        nodes = copy.deepcopy(READS["seeded-a"]["nodes"])
        broken = next(n for n in nodes if n["metadata"]["labels"]["cloud.google.com/gke-nodepool"] == "idle-batch-pool")
        for cond in broken["status"]["conditions"]:
            if cond["type"] == "Ready":
                cond["status"], cond["lastTransitionTime"] = "False", "2026-10-08T03:50:00Z"
        reads = {**READS["seeded-a"], "nodes": nodes}
        symptoms = symptoms_of("seeded-a", reads=reads)
        ur.mark_since(symptoms, None)
        node = next(s for s in symptoms if s["category"] == "node")
        self.assertEqual((node["onset"], node["since"]), ("2026-10-08T03:50:00Z", ur.SINCE_FIRST_SEEN))
        self.assertIn("NotReady since 2026-10-08T03:50:00Z after UPGRADE_NODES on idle-batch-pool", node["classifications"][0]["evidence"])
        payments = next(s for s in symptoms if s["object"] == PAYMENTS)
        self.assertTrue(payments["started"])
        self.assertEqual(payments["since"], ur.SINCE_FIRST_SEEN)

    def test_merged_row_is_graded_by_its_strongest_replica_whatever_the_order(self):
        cluster = cluster_doc("seeded-a")
        cluster["nodePools"][1]["config"]["effectiveCgroupMode"] = ur.CGROUP_V1_MODE  # idle-batch-pool on v1
        ops = [o for o in ops_for("seeded-a") if "idle-batch-pool" not in o["targetLink"]]  # untouched
        on_v1 = pod("jvm-aaaaa", images=["eclipse-temurin:8u302-jre"], statuses=[oom()], node="gke-seeded-a-idle-batch-pool-a5fd3288-q4ts")
        on_v2 = pod("jvm-zzzzz", images=["eclipse-temurin:8u302-jre"], statuses=[oom()], node="gke-seeded-a-default-pool-62ac8ee0-d595")
        for p in (on_v1, on_v2):
            p["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "jvm-rs"}]
        for first, second in ((on_v1, on_v2), (on_v2, on_v1)):
            reads = {"pods": [first, second], "nodes": READS["seeded-a"]["nodes"], "events": [], "pdbs": [], "owners": []}
            [row] = ur.collect_symptoms(cluster, reads, ops, SINCE)
            self.assertEqual(entries(row), {(14, ur.HIGH)}, f"order {first['metadata']['name']}, {second['metadata']['name']}")
        # Entry 2/20 gate: any replica on a touched pool opens it.
        failing_a = pod("pull-aaaaa", statuses=[waiting("ImagePullBackOff")], images=["k8s.gcr.io/pause:3.9"], node="gke-seeded-a-idle-batch-pool-a5fd3288-q4ts")
        failing_b = pod("pull-zzzzz", statuses=[waiting("ImagePullBackOff")], images=["k8s.gcr.io/pause:3.9"], node="gke-seeded-a-default-pool-62ac8ee0-d595")
        for p in (failing_a, failing_b):
            p["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "pull-rs"}]
        for first, second in ((failing_a, failing_b), (failing_b, failing_a)):
            [row] = self.classify(pods=[first, second], ops=ops)
            self.assertNotIn(ur.GATE_CLOSED_TEXT, row["classifications"][0]["detail"])

    def test_entry_14_high_needs_a_runtime_image_on_a_touched_v2_pool(self):
        jvm = pod("jvm", images=["eclipse-temurin:8u302-jre"], statuses=[oom()])
        [row] = self.classify(pods=[jvm])
        self.assertEqual(entries(row), {(14, ur.HIGH)})
        self.assertIn("runtime image eclipse-temurin:8u302-jre (JDK 8u302 < 8u372)", row["classifications"][0]["evidence"])
        [row] = self.classify(pods=[jvm], ops=[])
        self.assertEqual(entries(row), {(14, ur.MEDIUM)})
        [row] = self.classify(pods=[pod("plain", statuses=[oom()])])
        self.assertEqual(entries(row), {(14, ur.MEDIUM)})

    def test_control_plane_entries_gate_on_a_master_upgrade(self):
        master_only = [o for o in ops_for("seeded-a") if o["operationType"] == "UPGRADE_MASTER"]
        failing = {"kind": "ReplicaSet", "metadata": {"name": "x", "namespace": "apps"}, "status": {"conditions": [{"type": "ReplicaFailure", "status": "True"}]}}
        [row] = self.classify(events=[event("FailedCreate", 'failed calling webhook "gate.example.io"', kind="ReplicaSet")], ops=master_only, owners=[failing])
        self.assertEqual(entries(row), {(7, ur.HIGH)})
        [row] = self.classify(events=[event("FailedCreate", 'failed calling webhook "gate.example.io"', kind="ReplicaSet")], ops=[], owners=[failing])
        self.assertEqual(entries(row), {(7, ur.MEDIUM)})
        self.assertIn(ur.GATE_CLOSED_TEXT, row["classifications"][0]["detail"])
        # A pool-only window does not open the control-plane gate.
        pools_only = [o for o in ops_for("seeded-a") if o["operationType"] == "UPGRADE_NODES"]
        [row] = self.classify(events=[event("FailedCreate", 'failed calling webhook "gate.example.io"', kind="ReplicaSet")], ops=pools_only, owners=[failing])
        self.assertEqual(entries(row), {(7, ur.MEDIUM)})
        [row] = self.classify(events=[event("FailedCreate", 'error: unable to recognize: no matches for kind "FlowSchema"', kind="CronJob")], ops=pools_only)
        self.assertEqual(entries(row), {(6, ur.MEDIUM)})

    def test_symptom_onset_is_read_from_the_object(self):
        pending = pod("waiting", scheduled_message="0/4 nodes are available: 4 Insufficient cpu.")
        pending["status"]["conditions"][0]["lastTransitionTime"] = "2026-10-01T00:00:00Z"
        crashed = pod("crash", statuses=[{"name": "c0", "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": "2026-10-08T06:00:00Z"}}}])
        crashed["status"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": "2026-10-05T00:00:00Z"}]
        crashed["status"]["startTime"] = "2026-10-04T00:00:00Z"
        rows = {s["name"]: s for s in self.classify(pods=[pending, crashed], events=[event("Unhealthy", "probe failed", name="other", last="2026-10-08T12:00:00Z")])}
        self.assertEqual(rows["waiting"]["onset"], "2026-10-01T00:00:00Z")
        # A restarted pod: the earlier of its start and the Ready transition, never the latest crash.
        self.assertEqual(rows["crash"]["onset"], "2026-10-04T00:00:00Z")
        # Not restarted: the Ready transition dates the loss of readiness.
        probe_fail = pod("probe", statuses=[{"name": "c0", "state": {"running": {}}, "restartCount": 0}])
        probe_fail["status"]["startTime"] = "2026-10-04T00:00:00Z"
        probe_fail["status"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": "2026-10-05T00:00:00Z"}]
        self.assertEqual(self.classify(pods=[probe_fail])[0]["onset"], "2026-10-05T00:00:00Z")
        crashed["status"]["conditions"] = []
        no_condition = {s["name"]: s for s in self.classify(pods=[crashed])}
        self.assertEqual(no_condition["crash"]["onset"], "2026-10-04T00:00:00Z")
        self.assertEqual(rows["other"]["onset"], "2026-10-08T12:00:00Z")


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
        # maxUnavailable 1: zero now because the pods are not ready, so a lesser risk.
        shape = self.shape("seeded-capacity/PodDisruptionBudget/inference-server", 1)
        self.assertEqual(shape["confidence"], ur.MEDIUM)
        self.assertIn("allows disruption once its pods are ready", shape["evidence"])
        gemma = {(s["object"], s["entry"]): s for s in ur.collect_shapes(cluster_doc("gemma-gpu-upgraded"), READS["gemma-gpu-upgraded"])}
        budget = gemma[("kubeagents-system/PodDisruptionBudget/gemma-server", 1)]
        self.assertEqual(budget["confidence"], ur.HIGH)
        self.assertIn('disruptionsAllowed=0 by spec {"maxUnavailable": 0} over 1 replica(s)', budget["evidence"])
        pdb = {"kind": "PodDisruptionBudget", "metadata": {"name": "all", "namespace": "seeded-capacity"}, "spec": {"minAvailable": 4, "selector": {"matchLabels": {"app": "inference-server"}}}, "status": {"disruptionsAllowed": 0}}
        [shape] = [s for s in ur.budget_shapes([pdb], READS["seeded-a"]["workloads"]) if s["kind"] == "PodDisruptionBudget"]
        self.assertEqual(shape["confidence"], ur.HIGH)
        pdb["spec"]["minAvailable"] = 3
        [shape] = [s for s in ur.budget_shapes([pdb], READS["seeded-a"]["workloads"]) if s["kind"] == "PodDisruptionBudget"]
        self.assertEqual(shape["confidence"], ur.MEDIUM)
        pdb["spec"] = {"minAvailable": "100%", "selector": {"matchLabels": {"app": "inference-server"}}}
        [shape] = [s for s in ur.budget_shapes([pdb], READS["seeded-a"]["workloads"]) if s["kind"] == "PodDisruptionBudget"]
        self.assertEqual(shape["confidence"], ur.HIGH)

    def test_entry_1_single_replica_behind_a_budget(self):
        pdb = {"kind": "PodDisruptionBudget", "metadata": {"name": "payments", "namespace": "seeded-debug"}, "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app": "payments-api"}}}, "status": {"disruptionsAllowed": 1}}
        shapes = ur.budget_shapes([pdb], READS["seeded-a"]["workloads"])
        self.assertEqual([(s["object"], s["entry"], s["confidence"]) for s in shapes], [("seeded-debug/Deployment/payments-api", 1, ur.HIGH)])
        self.assertIn("one replica behind budget payments", shapes[0]["evidence"])
        # A budget that permits the eviction is no entry-1 risk, whatever the replica count: a chart's
        # `maxUnavailable: 1`, or `minAvailable: 0`. A percentage above zero demands the one pod.
        selector = pdb["spec"]["selector"]
        for permissive in ({"maxUnavailable": 1}, {"minAvailable": 0}):
            self.assertEqual(ur.budget_shapes([{**pdb, "spec": {**permissive, "selector": selector}}], READS["seeded-a"]["workloads"]), [], permissive)
        percent = {**pdb, "spec": {"minAvailable": "50%", "selector": selector}}
        self.assertEqual([(s["object"], s["entry"], s["confidence"]) for s in ur.budget_shapes([percent], READS["seeded-a"]["workloads"])], [("seeded-debug/Deployment/payments-api", 1, ur.HIGH)])
        # A budget whose selector spans several workloads counts their pods together, as its controller
        # does: one that still allows an eviction across the set does not demand the singleton's only
        # pod; one that allows none does, by count or by a percentage rounded as the controller rounds
        # it (minAvailable up, maxUnavailable down).
        workloads = copy.deepcopy(READS["seeded-a"]["workloads"])
        payments = next(w for w in workloads if w["metadata"]["name"] == "payments-api")
        payments["spec"]["template"]["metadata"].setdefault("labels", {})["tier"] = "payments"
        sibling = copy.deepcopy(payments)
        sibling["metadata"]["name"] = "payments-worker"
        sibling["spec"]["replicas"] = 3
        workloads.append(sibling)
        shared = {"matchLabels": {"tier": "payments"}}

        def singleton(spec):
            return [(s["object"], s["confidence"]) for s in ur.budget_shapes([{**pdb, "spec": {**spec, "selector": shared}}], workloads) if s["object"].startswith("seeded-debug/Deployment/")]

        for allows_one in ({"minAvailable": 1}, {"minAvailable": 3}, {"minAvailable": "50%"}, {"minAvailable": "75%"}, {"maxUnavailable": 1}, {"maxUnavailable": "25%"}):
            self.assertEqual(singleton(allows_one), [], allows_one)
        for allows_none in ({"minAvailable": 4}, {"minAvailable": "100%"}, {"minAvailable": "80%"}, {"maxUnavailable": 0}, {"maxUnavailable": "0%"}, {"maxUnavailable": "10%"}):
            self.assertEqual(singleton(allows_none), [("seeded-debug/Deployment/payments-api", ur.HIGH)], allows_none)
        [evidence] = [s["evidence"] for s in ur.budget_shapes([{**pdb, "spec": {"maxUnavailable": 0, "selector": shared}}], workloads) if s["object"] == "seeded-debug/Deployment/payments-api"]
        self.assertIn("covering 4 pod(s)", evidence)

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
        pod_rule = [{"apiGroups": [""], "apiVersions": ["v1"], "resources": ["pods"], "operations": ["CREATE"]}]
        config = {"kind": "ValidatingWebhookConfiguration", "metadata": {"name": "gate"}, "webhooks": [{"name": "gate.example.io", "failurePolicy": "Fail", "rules": pod_rule, "clientConfig": {"service": {"namespace": "apps", "name": "gate"}}}]}
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
        # A fail-closed hook reaching pods whose Service has a ready endpoint (gmp-operator's): no shape.
        backed = copy.deepcopy(config)
        backed["webhooks"][0]["clientConfig"]["service"] = {"namespace": "gmp-system", "name": "gmp-operator"}
        self.assertEqual(ur.webhook_shapes([backed], READS["seeded-a"]["endpointslices"]), [])
        # The captured gmp-operator hooks reach monitoring resources, not pods: screened out by their rules.
        self.assertEqual(ur.webhook_shapes(READS["seeded-a"]["webhooks"], READS["seeded-a"]["endpointslices"]), [])
        # Without the endpoint read the check cannot run.
        self.assertEqual(ur.webhook_shapes([config], None), [])
        # Rules decide whether the hook can stop a drain at all: the fleet's planted
        # seeded-fail-closed-gate (configmaps CREATE) matches no pod and is not entry 7.
        gate = copy.deepcopy(config)
        gate["webhooks"][0]["rules"] = [{"apiGroups": [""], "apiVersions": ["v1"], "resources": ["configmaps"], "operations": ["CREATE"]}]
        self.assertEqual(ur.webhook_shapes([gate], READS["seeded-a"]["endpointslices"]), [])
        gate["webhooks"][0].pop("rules")
        self.assertEqual(ur.webhook_shapes([gate], READS["seeded-a"]["endpointslices"]), [])
        gate["webhooks"][0]["rules"] = [{"apiGroups": ["*"], "apiVersions": ["*"], "resources": ["*"], "operations": ["*"]}]
        self.assertEqual(len(ur.webhook_shapes([gate], READS["seeded-a"]["endpointslices"])), 1)
        self.assertTrue(all(w.get("rules") for i in READS["seeded-a"]["webhooks"] for w in i["webhooks"]), "the fixture carries the real rules")

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

    def test_daily_with_byday_opens_and_closes_on_the_named_days_in_both_parsers(self):
        # RFC 5545 lets FREQ=DAILY carry BYDAY, and one parse serves the next opening and the last
        # closing: a Monday-to-Friday 03:00-07:00Z window read on a Friday evening next opens on the
        # Monday, and last closed that Friday at 07:00Z, in both.
        window = {"recurringWindow": {"recurrence": "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR", "window": {"startTime": "2026-01-05T03:00:00Z", "endTime": "2026-01-05T07:00:00Z"}}}
        friday_evening = datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc)
        text, opens = ur._next_opening(window, friday_evening)
        self.assertEqual((text, opens), ("DAILY on MO,TU,WE,TH,FR at 03:00 UTC for 4h", datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)))
        self.assertEqual(ur._last_closing(window, friday_evening), (False, datetime(2026, 10, 9, 7, 0, tzinfo=timezone.utc)))
        rule, freq, start, length, days = ur.recurring_window_rule(window["recurringWindow"])
        self.assertEqual((rule["FREQ"], freq, length, days), ("DAILY", ur.RRULE_FREQ_DAILY, timedelta(hours=4), [0, 1, 2, 3, 4]))

    def test_server_config_is_fetched_once_per_location(self):
        fleet = FakeFleet()
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: home, ur.STORE_HOME_ENV: home}), redirect_stderr(io.StringIO()):
            ur.collect(args(), run=fleet, now=NOW)
        self.assertEqual(sum(1 for c in fleet.calls if "get-server-config" in c), 1)


# Another process holding the lock: it takes the flock, says so, and releases it on a line of stdin.
HOLD_LOCK_SCRIPT = """
import fcntl, os, sys
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
sys.stdin.readline()
fcntl.flock(fd, fcntl.LOCK_UN)
"""


class LedgerAndGuardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: self.tmp.name, ur.STORE_HOME_ENV: self.tmp.name})
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
        result, _ = self.collect(dry_run=True, output=str(self.home / "out.json"), manifest_file=str(self.home / "manifest.json"))
        self.assertTrue(result["dry_run"])
        self.assertIn(ur.SECTION_ERRORS, result["report"])
        # None of the five artefacts: the report and its link, the JSON, the manifest, the guards,
        # the ledger.
        self.assertFalse((self.home / ur.REPORTS_SUBDIR).exists())
        self.assertFalse((self.home / "out.json").exists())
        self.assertFalse((self.home / "manifest.json").exists())
        self.assertFalse((self.home / ur.GUARDS_FILENAME).exists())
        self.assertFalse((self.home / ur.LEDGER_FILENAME).exists())
        # The kubeconfigs the reads fetch are working files under HERMES_HOME, not store artefacts.
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [".kubeconfigs"])

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
        self.assertEqual({g["entry"] for g in seeded["guards"] if g["kind"] == ur.GUARD_KIND_FAILURE}, {None, 1, 2, 14})
        self.assertEqual([g["check"] for g in seeded["guards"] if g["entry"] is None], [ur.CHECK_SYMPTOM_UNCLASSIFIED])
        self.assertEqual({g["entry"] for g in seeded["guards"] if g["kind"] == ur.GUARD_KIND_RISK}, {1, 4, 12, 13, 14, 17, 18, 19, 20})
        self.assertEqual(doc["guards"], result["guards"])
        self.assertEqual(seeded["baseline"]["shapes"], 9)
        self.assertEqual(seeded["next_upgrade"]["target"], "1.35.8-gke.1225000")
        sections = doc["sections"]
        self.assertEqual([(i["cluster"], i["object"], i["entries"]) for i in sections["errors"]], [(SEEDED, INFERENCE, "2"), (SEEDED, "seeded-capacity/PodDisruptionBudget/inference-server", "1")])
        self.assertEqual([i["object"] for i in sections["warnings"]], [KUBE_DNS, TUNER, PAYMENTS, "seeded-stall/Deployment/inventory-api"])
        inference_row = next(s for r in doc["reviews"] for s in r["what_failed"] if s["object"] == INFERENCE)
        self.assertEqual((inference_row["onset_source"], inference_row["new_pods"], inference_row["pre_existing_pods"]), (ur.ONSET_FROM_POD, ["inference-server-778b78fdb8-ld26r", "inference-server-778b78fdb8-zxlkz"], ["inference-server-778b78fdb8-cp2pf"]))
        self.assertEqual([(c["cluster"], c["incidents"]) for c in sections["info"]["clean"]], [(GEMMA, 2), (SEEDED, 4)])
        self.assertEqual((sections["info"]["unchanged"], sections["info"]["failed_reads"]), ([], []))

    def test_guards_file_merges_new_seen_again_and_gone(self):
        seen = "2026-10-08T18:00:00Z"
        fresh = ur.guards_for(SEEDED, symptoms_of("seeded-a"), seen)
        first = ur.merge_guards(ur.empty_guards(), fresh, {SEEDED}, seen)
        ids = {g["id"] for g in first["guards"]}
        self.assertEqual(ids, {ur.guard_id(SEEDED, 14, PAYMENTS), ur.guard_id(SEEDED, 2, INFERENCE), ur.guard_id(SEEDED, 1, "seeded-capacity/PodDisruptionBudget/inference-server"), ur.guard_id(SEEDED, None, "seeded-stall/Deployment/inventory-api")})
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
        # Every guard is kept; the partial review's event rows (no pod list to collapse them into)
        # add event-only unclassified guards of their own.
        self.assertTrue(before <= after)
        self.assertTrue(all(g["entry"] is None and g["source"] == ur.GUARD_SOURCE_EVENT for g in second["guards"] if g["cluster"] == SEEDED and g["id"] not in before))
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        self.assertEqual(ledger["clusters"][SEEDED]["last_run"], "2026-10-08T18:00:00Z")
        self.assertEqual(ledger["clusters"][SEEDED]["partial_read"], "2026-10-15T18:00:00Z")
        review = second["reviews"][0]
        self.assertFalse(review["reviewed"])
        self.assertEqual(review["partial"], ["pods"])
        self.assertIn(ur.PARTIAL_READ_TEXT.format(failed="pods"), ur.render_report(second))
        self.assertNotIn("the guards were kept", ur.render_report(second))
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
                rc = ur.main(["--full", "--scope-projects", PROJECT, "--no-report"])
        self.assertEqual(rc, ur.EXIT_USAGE)
        self.assertIn("crash record, not a re-baseline", err.getvalue())
        self.assertIn(ur.EXIT_PROJECTS_FORMAT.format(projects=PROJECT, note=""), err.getvalue())
        self.assertFalse((self.home / ur.LEDGER_FILENAME).exists())
        # The crash record blocks a fresh start until it is archived.
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--no-report"]), ur.EXIT_USAGE)
                # The reset archives the ledger's record and the live (broken) guards file: the next
                # run is a baseline, both files written together.
                self.assertEqual(ur.main(["--reset-ledger"]), 0)
                self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--no-report"]), 0)
                self.assertTrue((self.home / ur.LEDGER_FILENAME).exists() and (self.home / ur.GUARDS_FILENAME).exists())
                # A broken guards file beside the live ledger is met next: set aside, and its record
                # blocks like the ledger's.
                (self.home / ur.GUARDS_FILENAME).write_text("{not json")
                self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--no-report"]), ur.EXIT_USAGE)
                self.assertTrue(list(self.home.glob(ur.GUARDS_RECORD_GLOB)))
                with redirect_stderr(io.StringIO()) as err2:
                    self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--no-report"]), ur.EXIT_USAGE)
                self.assertIn("guards.json.unreadable-", err2.getvalue())
                self.assertIn(f"Restore it as {ur.GUARDS_FILENAME}", err2.getvalue())
                self.assertEqual(ur.main(["--reset-ledger"]), 0)
                rc = ur.main(["--full", "--scope-projects", PROJECT, "--no-report"])
        self.assertEqual(rc, 0)
        # Both files' records sit in the archive, and neither beside the live files.
        self.assertEqual(sorted({p.name.split(".unreadable-")[0] for p in (self.home / ur.ARCHIVE_SUBDIR).rglob("*.unreadable-*")}), sorted({ur.GUARDS_FILENAME, ur.LEDGER_FILENAME}))
        self.assertEqual([p.name for p in self.home.glob("*.unreadable-*")], [])
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["version"], ur.LEDGER_VERSION)

    def test_lock_is_waited_for_ten_minutes_polling(self):
        lock_path = self.home / ur.LOCK_FILENAME
        held = ur.acquire_lock(lock_path)
        self.assertIsNotNone(held)
        try:
            # A fake clock advanced by each sleep: the budget is two minutes in fifteen-second polls
            # (the SOP runs the collector in the foreground under a 600-second budget), then None.
            now = [0.0]
            slept = []

            def sleep(seconds):
                slept.append(seconds)
                now[0] += seconds

            self.assertIsNone(ur.acquire_lock(lock_path, sleep=sleep, clock=lambda: now[0]))
            self.assertEqual((sum(slept), len(slept), ur.LOCK_WAIT, ur.LOCK_POLL_INTERVAL), (120.0, 8, timedelta(minutes=2), timedelta(seconds=15)))
            # Released during the wait: the lock is taken and the run proceeds.
            released = []

            def release_then_sleep(seconds):
                slept.append(seconds)
                if len(released) == 2:
                    held.close()
                released.append(seconds)

            taken = ur.acquire_lock(lock_path, sleep=release_then_sleep, clock=lambda: 0.0)
            self.assertIsNotNone(taken)
            taken.close()
        finally:
            with contextlib_suppress(ValueError, OSError):
                held.close()
        # Through main: a second run waits the (patched) budget, prints the one line with the wait, writes nothing.
        held = ur.acquire_lock(lock_path)
        try:
            with mock.patch.object(ur, "LOCK_WAIT", timedelta(seconds=0.05)), mock.patch.object(ur, "LOCK_POLL_INTERVAL", timedelta(seconds=0.01)):
                with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
                    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                        rc = ur.main(["--full", "--scope-projects", PROJECT])
                self.assertEqual(rc, 0)
                line = out.getvalue().strip()
                self.assertTrue(line.startswith(ur.lock_held_line(lock_path, timedelta(seconds=0.05))), line)
                self.assertTrue(line.endswith(ur.EXIT_PROJECTS_FORMAT.format(projects=PROJECT, note="")), line)
                self.assertIn("waited 0 min for it; nothing written", out.getvalue())
                self.assertFalse((self.home / ur.LEDGER_FILENAME).exists())
                # Released while the second run waits: it takes the lock and runs.
                threading.Timer(0.02, held.close).start()
                with mock.patch.object(ur, "LOCK_WAIT", timedelta(seconds=5)):
                    with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
                        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                            rc = ur.main(["--full", "--scope-projects", PROJECT, "--no-report"])
                self.assertEqual(rc, 0)
                self.assertIn(ur.SECTION_ERRORS, out.getvalue())
                self.assertTrue((self.home / ur.LEDGER_FILENAME).exists())
        finally:
            with contextlib_suppress(ValueError, OSError):
                held.close()

    def test_lock_is_read_only_world_readable_and_only_contention_is_retried(self):
        store = self.home / "nested" / "store"
        lock_path = store / ur.LOCK_FILENAME
        # Under a hardened umask the lock is still 0644 and the store's directories 0755, made the
        # report store's way; a root hand run hands what it creates to the store's owner (below).
        mask = os.umask(0o077)
        try:
            held = ur.acquire_lock(lock_path)
        finally:
            os.umask(mask)
        self.assertIsNotNone(held)
        self.assertFalse(held.writable())
        # The store's directory and the parents it had to make are the store's, at its mode.
        self.assertEqual((stat.S_IMODE(lock_path.stat().st_mode), stat.S_IMODE(store.stat().st_mode), stat.S_IMODE((self.home / "nested").stat().st_mode)), (ur.LOCK_FILE_MODE, ur.STORE_DIR_MODE, ur.STORE_DIR_MODE))
        self.assertEqual((ur.STORE_DIR_MODE, ur.STORE_FILE_MODE), (0o755, 0o644))
        held.close()
        # A lock file left read-only by another uid still opens: flock(2) needs no writable descriptor.
        lock_path.chmod(0o444)
        again = ur.acquire_lock(lock_path)
        self.assertIsNotNone(again)
        again.close()
        # Only contention is retried: a filesystem that refuses flock raises at once, without a wait,
        # rather than spending the budget and blaming a run that does not exist.
        slept = []
        with mock.patch.object(ur.fcntl, "flock", side_effect=OSError(errno.ENOLCK, "No locks available")):
            with self.assertRaises(OSError) as caught:
                ur.acquire_lock(lock_path, sleep=slept.append, clock=lambda: 0.0)
        self.assertEqual((caught.exception.errno, slept), (errno.ENOLCK, []))
        # The store's files follow the same rule: 0644 whatever the writer's umask, for the JSON
        # (ledger, guards, manifest) and the report alike, with their directories 0777.
        mask = os.umask(0o077)
        try:
            ur.write_json_atomically(store / "deep" / "doc.json", {"a": 1})
            ur.write_report(store / "reports" / "r.md", "# r\n")
        finally:
            os.umask(mask)
        self.assertEqual(stat.S_IMODE((store / "deep").stat().st_mode), ur.STORE_DIR_MODE)
        self.assertEqual(stat.S_IMODE((store / "deep" / "doc.json").stat().st_mode), ur.STORE_FILE_MODE)
        self.assertEqual(stat.S_IMODE((store / "reports" / "r.md").stat().st_mode), ur.STORE_FILE_MODE)
        self.assertEqual(ur.STORE_FILE_MODE, 0o644)
        # A run as root hands every directory and file it creates, and the lock, to the owner of the
        # nearest directory that already existed, as the report store's `_adopt_owner` does, so the
        # uid-1000 tick writes after a root hand run without a world-writable directory.
        chowns = []
        fresh = self.home / "root-run" / "store"
        with mock.patch.object(os, "geteuid", return_value=ur.ROOT_UID), mock.patch.object(os, "fchown", side_effect=lambda fd, uid, gid: chowns.append((os.fstat(fd).st_ino, uid, gid))):
            ur.make_store_dir(fresh)
            ur.write_json_atomically(fresh / "doc.json", {"a": 1})
            ur.write_report(fresh / "r.md", "# r\n", link=False)
            held = ur.acquire_lock(fresh / ur.LOCK_FILENAME)
            held.close()
        owner = self.home.stat()
        self.assertTrue(chowns and all((uid, gid) == (owner.st_uid, owner.st_gid) for _, uid, gid in chowns), chowns)
        # Through the descriptor, never the path: the inodes handed over are the two directories, the
        # two files (staged under random names and renamed into place) and the lock, nothing else.
        self.assertEqual({ino for ino, _, _ in chowns}, {p.stat().st_ino for p in (self.home / "root-run", fresh, fresh / "doc.json", fresh / "r.md", fresh / ur.LOCK_FILENAME)})

    def test_an_existing_store_directory_is_left_as_found_and_a_denied_store_is_one_line(self):
        # The report store's rule touches only the directories it makes. A store directory that
        # already exists root-owned at 0777 (a run under the earlier world-writable rule, or an
        # operator's chmod to let the tick in) keeps its mode and its owner under a root run, since
        # tightening it to 0755 without handing it over would lock the uid-1000 tick out of a store
        # it could write before; a child the root run makes under it is 0755 and that directory's
        # owner's.
        opened = self.home / "opened"
        opened.mkdir()
        opened.chmod(0o777)
        chowns = []
        with mock.patch.object(os, "geteuid", return_value=ur.ROOT_UID), mock.patch.object(os, "fchown", side_effect=lambda fd, uid, gid: chowns.append((os.fstat(fd).st_ino, uid, gid))):
            ur.make_store_dir(opened)
            self.assertEqual((stat.S_IMODE(opened.stat().st_mode), chowns), (0o777, []))
            ur.make_store_dir(opened / "reports")
        owner = opened.stat()
        self.assertEqual((stat.S_IMODE(opened.stat().st_mode), stat.S_IMODE((opened / "reports").stat().st_mode)), (0o777, ur.STORE_DIR_MODE))
        self.assertEqual(chowns, [((opened / "reports").stat().st_ino, owner.st_uid, owner.st_gid)])
        # A store path this uid cannot write is one stderr line, exit 2, nothing on stdout and no
        # traceback, the owner named when it is another uid's and the run's projects at the end for
        # the recovery `finish`; the lock, when it was taken, is released on the way out.
        denied = PermissionError(errno.EACCES, "Permission denied", str(self.home / "reports" / ".r.md.tmp"))
        (self.home / "reports").mkdir()
        uid = self.home.stat().st_uid
        self.assertEqual(ur.store_denied_line(denied), ur.STORE_DENIED_FORMAT.format(path=denied.filename, error="Permission denied", note=""))
        with mock.patch.object(os, "geteuid", return_value=uid + 1):
            self.assertEqual(ur.store_denied_line(denied), ur.STORE_DENIED_FORMAT.format(path=denied.filename, error="Permission denied", note=ur.STORE_OWNER_NOTE_FORMAT.format(where=self.home / "reports", owner=uid, uid=uid + 1)))
        tail = ur.EXIT_PROJECTS_FORMAT.format(projects=PROJECT, note="")
        full = ["--full", "--scope-projects", PROJECT, "--no-report"]
        for target, flags, ending in (("acquire_lock", full, tail), ("collect", full, tail), ("probe_lock", ["--probe-lock"], ""), ("reset_ledger", ["--reset-ledger"], "")):
            with self.subTest(target=target):
                with mock.patch.object(ur, target, side_effect=denied), mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
                    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                        rc = ur.main(flags)
                self.assertEqual((rc, out.getvalue(), err.getvalue().splitlines()[-1]), (ur.EXIT_USAGE, "", ur.store_denied_line(denied) + ending))
                self.assertNotIn("Traceback", err.getvalue())
        held = ur.acquire_lock(self.home / ur.LOCK_FILENAME, wait=timedelta(0))
        self.assertIsNotNone(held)
        held.close()

    def test_a_root_run_follows_no_symlink_planted_in_the_store(self):
        # The store's directories are the uid-1000 agent's, so the agent's own account can plant a
        # symlink under a name a root hand run would write: the report's staging name, the lock, a
        # store directory. The report is staged under a random name made with O_EXCL with its mode and
        # owner set through the descriptor; the lock is opened with O_NOFOLLOW and refused as a symlink;
        # a directory is set through a descriptor opened without following. The targets stay untouched.
        target = self.home / "target"
        target.write_text("root's secret")
        target.chmod(0o600)
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        elsewhere.chmod(0o700)
        store = self.home / "store"
        ur.make_store_dir(store / ur.REPORTS_SUBDIR)
        (store / ur.REPORTS_SUBDIR / "r.md.tmp").symlink_to(target)
        (store / ur.LOCK_FILENAME).symlink_to(target)
        (store / ur.ARCHIVE_SUBDIR).symlink_to(elsewhere)
        chowned = []
        with mock.patch.object(os, "geteuid", return_value=ur.ROOT_UID), mock.patch.object(os, "fchown", side_effect=lambda fd, uid, gid: chowned.append(os.fstat(fd).st_ino)):
            ur.write_report(store / ur.REPORTS_SUBDIR / "r.md", "# r\n", link=False)
            with self.assertRaises(ur.StateUnreadable) as caught:
                ur.acquire_lock(store / ur.LOCK_FILENAME)
            ur.make_store_dir(store / ur.ARCHIVE_SUBDIR)
        self.assertEqual((target.read_text(), stat.S_IMODE(target.stat().st_mode), (store / ur.REPORTS_SUBDIR / "r.md.tmp").is_symlink(), (store / ur.REPORTS_SUBDIR / "r.md").read_text()), ("root's secret", 0o600, True, "# r\n"))
        self.assertEqual((str(caught.exception), (store / ur.LOCK_FILENAME).is_symlink()), (ur.LOCK_IS_SYMLINK_TEXT.format(path=store / ur.LOCK_FILENAME), True))
        self.assertEqual((stat.S_IMODE(elsewhere.stat().st_mode), (store / ur.ARCHIVE_SUBDIR).is_symlink()), (0o700, True))
        self.assertTrue({target.stat().st_ino, elsewhere.stat().st_ino}.isdisjoint(chowned))
        self.assertEqual(ur.probe_lock(store / ur.LOCK_FILENAME), (ur.EXIT_USAGE, ur.LOCK_IS_SYMLINK_TEXT.format(path=store / ur.LOCK_FILENAME)))

    def test_api_disabled_project_is_empty_for_operations_too(self):
        ops, error = ur.list_operations("no-gke-project", SINCE, run=lambda argv, **kw: run_of(1, "", API_OFF_STDERR.format(project="no-gke-project", api="operations")))
        self.assertEqual((ops, error), ([], None))
        result, _ = self.collect(FakeFleet(api_off=["no-gke-project"]), scope_projects=f"{PROJECT},no-gke-project")
        self.assertEqual((result["failed_reads"], result["operations_unread"], result["projects"]), ([], [], [PROJECT, "no-gke-project"]))
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})[ur.LEDGER_PROJECTS_KEY], [PROJECT, "no-gke-project"])
        self.assertNotIn(ur.INFO_FAILED_READS, ur.render_report(result))

    def test_after_upgrade_run_that_waited_prints_nothing_when_already_reviewed(self):
        # The Sunday run recorded every operation; an after-upgrade wake that lands in the same minute
        # waits for the lock, then finds its operations reviewed and prints nothing.
        self.collect()
        lock_path = self.home / ur.LOCK_FILENAME
        held = ur.acquire_lock(lock_path)
        threading.Timer(0.02, held.close).start()
        with mock.patch.object(ur, "LOCK_POLL_INTERVAL", timedelta(seconds=0.01)), mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                rc = ur.main(["--after-upgrade", "--project", PROJECT])
        self.assertEqual((rc, out.getvalue()), (0, ""))
        self.assertIn(ur.AFTER_UPGRADE_QUIET_LOG, err.getvalue())
        self.assertEqual(sorted(p.name for p in (self.home / ur.REPORTS_SUBDIR).iterdir()), ["20261008T180000Z.json", "20261008T180000Z.md", ur.LATEST_REPORT_LINK])

    def test_probe_lock_reports_free_or_held_and_touches_nothing(self):
        # `--probe-lock` tests the lock without taking it for any time and without writing: a store
        # with no lock file is free and stays untouched; a lock another process's flock holds is
        # reported with the holder line and exit 1; once released it is free again. It takes no
        # other mode flag.
        lock_path = self.home / ur.LOCK_FILENAME
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ur.main(["--probe-lock"]), 0)
        self.assertEqual(out.getvalue().strip(), ur.LOCK_PROBE_FREE_TEXT.format(path=lock_path))
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])
        holder = subprocess.Popen([sys.executable, "-c", HOLD_LOCK_SCRIPT, str(lock_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ur.main(["--probe-lock"]), ur.EXIT_LOCK_HELD)
            self.assertEqual(out.getvalue().strip(), ur.lock_probe_held_line(lock_path))
            self.assertTrue(out.getvalue().startswith("another retrospective run holds"))
            self.assertEqual(sorted(p.name for p in self.home.iterdir()), [ur.LOCK_FILENAME])
        finally:
            holder.stdin.write("release\n")
            holder.stdin.flush()
            holder.wait(timeout=30)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ur.main(["--probe-lock"]), 0)
        self.assertEqual(out.getvalue().strip(), ur.LOCK_PROBE_FREE_TEXT.format(path=lock_path))
        # A pending reset is named on the line, free or held: the probe is what the SOP runs before
        # `start`, and the baseline runs without one.
        marker = self.home / ur.RESET_MARKER_FILENAME
        marker.write_text("{}")
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ur.main(["--probe-lock"]), 0)
        self.assertEqual(out.getvalue().strip(), ur.LOCK_PROBE_RESET_PENDING_FORMAT.format(line=ur.LOCK_PROBE_FREE_TEXT.format(path=lock_path), marker=marker))
        held_by_us = ur.acquire_lock(lock_path)
        try:
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ur.main(["--probe-lock"]), ur.EXIT_LOCK_HELD)
            self.assertEqual(out.getvalue().strip(), ur.LOCK_PROBE_RESET_PENDING_FORMAT.format(line=ur.lock_probe_held_line(lock_path), marker=marker))
        finally:
            held_by_us.close()
        marker.unlink()
        for argv in (["--probe-lock", "--full", "--scope-projects", PROJECT], ["--probe-lock", "--after-upgrade"], ["--probe-lock", "--dry-run"], ["--probe-lock", "--reset-ledger"], ["--probe-lock", "--cluster", SEEDED]):
            with self.subTest(argv=argv):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(ur.main(argv), ur.EXIT_USAGE)
                self.assertIn(ur.PROBE_LOCK_WITH_OTHERS_TEXT, err.getvalue())

    def test_lock_makes_a_second_run_exit_quietly(self):
        lock_path = self.home / ur.LOCK_FILENAME
        held = ur.acquire_lock(lock_path)
        self.assertIsNotNone(held)
        try:
            with mock.patch.object(ur, "LOCK_WAIT", timedelta(0)), mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
                with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                    rc = ur.main(["--full", "--scope-projects", PROJECT])
            self.assertEqual(rc, 0)
            line = out.getvalue().strip()
            # The line ends with the projects the run resolved, which the SOP's recovery `finish` lists.
            self.assertTrue(line.startswith(ur.lock_held_line(lock_path, timedelta(0))), line)
            self.assertTrue(line.endswith(ur.EXIT_PROJECTS_FORMAT.format(projects=PROJECT, note="")), line)
            self.assertIn("another retrospective run holds", out.getvalue())
            self.assertFalse((self.home / ur.LEDGER_FILENAME).exists())
            self.assertFalse((self.home / ur.REPORTS_SUBDIR).exists())
            # The same exit on every route: the after-upgrade wake and the forced review print the
            # lock line, exit 0 and write nothing.
            for argv in (["--after-upgrade", "--project", PROJECT], ["--cluster", SEEDED, "--project", PROJECT]):
                with mock.patch.object(ur, "LOCK_WAIT", timedelta(0)), mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
                    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                        self.assertEqual(ur.main(argv), 0, argv)
                line = out.getvalue().strip()
                self.assertTrue(line.startswith(ur.lock_held_line(lock_path, timedelta(0))), (argv, line))
                self.assertTrue(line.endswith(ur.EXIT_PROJECTS_FORMAT.format(projects=PROJECT, note="")), (argv, line))
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
                self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--no-report"]), 0)
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

    def test_an_upgrading_cluster_keeps_its_guards_and_raises_no_stale_warning(self):
        # Run 1 writes failure guards on seeded-a; run 2 finds its pool upgrade still running.
        first, _ = self.collect()
        self.assertTrue(any(g["cluster"] == SEEDED and g["kind"] == ur.GUARD_KIND_FAILURE for g in first["guards"]))
        ops = copy.deepcopy(OPERATIONS)
        running = next(o for o in ops if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/pinned-inference-pool" in o["targetLink"])
        running["status"], running["endTime"] = "RUNNING", None
        second, _ = self.collect(FakeFleet(operations=ops))
        self.assertEqual([u["cluster"] for u in second["upgrading"]], [SEEDED])
        stale = [w for w in second["sections"]["warnings"] if w["kind"] == ur.INCIDENT_STALE_GUARD]
        self.assertEqual([w["cluster"] for w in stale], [])
        self.assertTrue(any(g["cluster"] == SEEDED and g["kind"] == ur.GUARD_KIND_FAILURE for g in second["guards"]))
        self.assertNotIn(SEEDED, ur.render_report(second).split(ur.SECTION_INFO)[0])

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
        # A listing that failed is the project's gap: the run stays full and drops nothing.
        self.collect()
        kept, _ = self.collect(FakeFleet(list_rc=1), now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(kept["removed_clusters"], [])
        self.assertFalse(kept["scoped"])
        self.assertIn(GEMMA, {g["cluster"] for g in kept["guards"]})
        self.assertIn(f"{GEMMA}: its project's listing failed (clusters list rc=1: PERMISSION_DENIED); ledger entry and guards kept unchanged", kept["failed_reads"])

    def test_review_boundary_survives_a_raise_in_its_own_fallback_inputs(self):
        def boom(*args, **kwargs):
            raise RuntimeError("planted")

        with mock.patch.object(ur, "collect_symptoms", boom), mock.patch.object(ur, "what_happened", boom):
            result, _ = self.collect()
        failed = [r for r in result["reviews"] if not r["reviewed"]]
        self.assertEqual(len(failed), 2)
        self.assertTrue(all(r["what_happened"]["status"] in (ur.STATUS_NEW, ur.STATUS_UPGRADED, ur.STATUS_FORCED) and r["what_happened"]["operations"] == [] for r in failed))
        self.assertTrue(all(any(e.startswith("review failed: RuntimeError('planted')") for e in r["read_errors"]) for r in failed))
        self.assertIn(ur.SECTION_INFO, ur.render_report(result))
        with mock.patch.object(ur, "collect_symptoms", boom), mock.patch.object(ur, "next_upgrade", boom):
            result, _ = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        failed = [r for r in result["reviews"] if not r["reviewed"]]
        self.assertTrue(failed)
        self.assertTrue(all(r["next_upgrade"]["window"] == ur.NO_WINDOW_TEXT and r["next_upgrade"]["target"] is None for r in failed))
        ur.render_report(result)
        # A window the parser rejects on a cluster that is otherwise read in full loses the
        # next-upgrade line, not the review: reviewed, with the error under its read errors.
        with mock.patch.object(ur, "next_upgrade", boom):
            reviewed, _ = self.collect(now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        self.assertTrue(all(r["reviewed"] for r in reviewed["reviews"]))
        self.assertTrue(all(r["read_errors"] == [f"{ur.NEXT_UPGRADE_ERROR_PREFIX}RuntimeError('planted')"] and r["next_upgrade"]["window"] == ur.NO_WINDOW_TEXT for r in reviewed["reviews"]))
        # And on an unchanged cluster, where no review boundary stands: the run still writes, the
        # row carries the bare line and Info names the failure.
        with mock.patch.object(ur, "next_upgrade", boom):
            later, _ = self.collect(now=datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc))
        self.assertEqual([u["cluster"] for u in later["unchanged"]], [GEMMA, SEEDED])
        self.assertTrue(all(u["next_upgrade"] == ur._bare_next_upgrade() for u in later["unchanged"]))
        self.assertIn(f"{SEEDED}: {ur.NEXT_UPGRADE_ERROR_PREFIX}RuntimeError('planted')", later["failed_reads"])
        self.assertIn(ur.INFO_UNCHANGED, ur.render_report(later))
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["updated_at"], "2026-10-17T18:00:00Z")

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
        # The operator fixes payments-api, inventory-api (the unclassified guard) and the registry
        # image: those guards clear, the rest stay.
        fixed_pods = [p for p in READS["seeded-a"]["pods"] if "payments-api" not in p["metadata"]["name"] and "inventory-api" not in p["metadata"]["name"]]
        fixed_workloads = copy.deepcopy(READS["seeded-a"]["workloads"])
        for w in fixed_workloads:
            if w["metadata"]["name"] == "legacy-registry-pull":
                w["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.k8s.io/pause:3.9"
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "pods": fixed_pods, "workloads": fixed_workloads}}):
            third, _ = self.collect(now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in third["rechecks"] if r["cluster"] == SEEDED]
        self.assertEqual(sorted(recheck["cleared"]), sorted([ur.guard_id(SEEDED, 14, PAYMENTS), ur.guard_id(SEEDED, None, "seeded-stall/Deployment/inventory-api"), ur.guard_id(SEEDED, 20, "seeded-shapes/Deployment/legacy-registry-pull", ur.GUARD_KIND_RISK)]))
        self.assertIn(f"- {SEEDED}: re-checked for {len(seeded_guards)} guard(s): 3 cleared", ur.render_report(third))
        remaining = {g["id"] for g in third["guards"] if g["cluster"] == SEEDED}
        self.assertEqual(len(remaining), len(seeded_guards) - 3)
        self.assertIn(ur.guard_id(SEEDED, 1, "seeded-capacity/PodDisruptionBudget/inference-server"), remaining)
        # A re-check whose read fails clears nothing, holds every guard that needed the read, and
        # says so; the held guards keep their review count.
        fourth, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods")]), now=datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in fourth["rechecks"] if r["cluster"] == SEEDED]
        self.assertEqual((recheck["cleared"], recheck["failed_reads"]), ([], ["pods"]))
        self.assertTrue(recheck["errors"])
        self.assertEqual(recheck["held"].get(ur.guard_id(SEEDED, 2, INFERENCE)), ["pods"])
        # The budget's keep rule reads pdbs, which answered: still at zero, refreshed.
        self.assertIn(ur.guard_id(SEEDED, 1, "seeded-capacity/PodDisruptionBudget/inference-server"), recheck["refreshed"])
        self.assertEqual(next(g for g in fourth["guards"] if g["id"] == ur.guard_id(SEEDED, 2, INFERENCE))["reviews"], 3)
        self.assertIn(ur.RECHECK_HELD_FORMAT.format(count=len(recheck["held"])), ur.render_report(fourth))

    def test_event_only_guard_is_rechecked_through_events(self):
        # An attach failure on a pod of the healthy checkout-gateway: an event-only guard. The
        # re-check adds the events read: refreshed while an event still names the object, held when
        # that read fails, cleared when the events read is empty and the pods read shows it healthy.
        checkout = "seeded-reliability/Deployment/checkout-gateway"
        attach = event("FailedAttachVolume", "AttachVolume.Attach failed for volume pv-1", name="checkout-gateway-568bd768b4-zzzzz", namespace="seeded-reliability")
        with_event = {**READS["seeded-a"], "events": READS["seeded-a"]["events"] + [attach]}
        with mock.patch.dict(READS, {"seeded-a": with_event}):
            first, _ = self.collect()
        gid = ur.guard_id(SEEDED, 19, checkout)
        guard = next(g for g in first["guards"] if g["id"] == gid)
        self.assertEqual((guard["source"], guard["check"], guard["reviews"]), (ur.GUARD_SOURCE_EVENT, ur.CHECK_SYMPTOM_TENTATIVE, 1))
        # A guard a pod also showed is not event-only.
        self.assertEqual(next(g for g in first["guards"] if g["id"] == ur.guard_id(SEEDED, 2, INFERENCE))["source"], ur.CATEGORY_PENDING)
        # Unchanged, the event still in the API server's hour: the re-check reads events and refreshes.
        with mock.patch.dict(READS, {"seeded-a": with_event}):
            second, fleet = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in second["rechecks"] if r["cluster"] == SEEDED]
        self.assertIn("events", {c[2] for c in fleet.calls if c[0] == "kubectl"})
        self.assertIn(gid, recheck["refreshed"])
        self.assertEqual((recheck["cleared"], recheck["held"]), ([], {}))
        self.assertEqual(next(g for g in second["guards"] if g["id"] == gid)["reviews"], 2)
        self.assertEqual([i for i in second["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD], [])
        # The events read fails: held as it stands, no review counted, the read a limitation.
        with mock.patch.dict(READS, {"seeded-a": with_event}):
            third, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "events")]), now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in third["rechecks"] if r["cluster"] == SEEDED]
        self.assertEqual((recheck["held"].get(gid), recheck["failed_reads"]), (["events"], ["events"]))
        self.assertNotIn(gid, recheck["cleared"] + recheck["refreshed"])
        self.assertEqual(next(g for g in third["guards"] if g["id"] == gid)["reviews"], 2)
        self.assertIn(f"- {SEEDED}: re-checked for {recheck['guards']} guard(s): 0 cleared; {ur.RECHECK_HELD_FORMAT.format(count=1)}", ur.render_report(third))
        self.assertIn("cluster re-checked this run but the read failed (events:", ur.render_report(third))
        # The event gone and the pods healthy: both reads answer, neither names it, cleared.
        fourth, _ = self.collect(now=datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in fourth["rechecks"] if r["cluster"] == SEEDED]
        self.assertIn(gid, recheck["cleared"])
        self.assertNotIn(gid, {g["id"] for g in fourth["guards"]})

    def test_stale_guard_on_a_partially_read_cluster_says_so(self):
        self.collect()
        later = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        second, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods")]), now=later, cluster=[SEEDED])
        stale = [i for i in second["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD and i["cluster"] == SEEDED]
        self.assertTrue(stale)
        self.assertTrue(all(i["partial"] == ["pods"] for i in stale))
        report = ur.render_report(second)
        self.assertEqual(report.count(ur.STALE_PARTIAL_TEXT.format(failed="pods")), len(stale))
        # gemma is outside this scoped run: no stale incident for it at all.
        self.assertEqual([i for i in second["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD and i["cluster"] == GEMMA], [])

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

    def test_recheck_refresh_skips_pods_still_starting(self):
        self.collect()
        young = pod("fresh", namespace="seeded-debug", statuses=[{"name": "c0", "state": {"waiting": {"reason": "ContainerCreating"}}, "restartCount": 0}])
        young["status"]["startTime"] = "2026-10-15T17:55:00Z"
        young["metadata"]["creationTimestamp"] = "2026-10-15T17:55:00Z"
        young["status"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": "2026-10-15T17:55:00Z"}]
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [young]}}):
            second, _ = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in second["rechecks"] if r["cluster"] == SEEDED]
        self.assertFalse(any("fresh" in key for key in recheck["symptom_baseline"]), recheck["symptom_baseline"])
        self.assertFalse(any("fresh" in key for key in ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED]["symptoms"]))

    def test_gated_pods_are_listed_under_info_not_graded(self):
        queued = pod("queued", namespace="seeded-debug", phase="Pending", statuses=[])
        queued["metadata"]["creationTimestamp"] = "2026-10-01T00:00:00Z"
        queued["status"]["conditions"] = [{"type": "PodScheduled", "status": "False", "reason": "SchedulingGated", "message": "Scheduling is blocked due to non-empty scheduling gates"}]
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [queued]}}):
            result, _ = self.collect()
        review = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertEqual(review["gated"], ["seeded-debug/queued"])
        self.assertNotIn("seeded-debug/queued", json.dumps([s.get("pods") for s in review["what_failed"]]))
        self.assertFalse(any(g["object"].endswith("/queued") for g in result["guards"]))
        block = ur.render_report(result).split(ur.SECTION_INFO)[1].split(f"### {SEEDED}")[1].split("### ")[0]
        self.assertIn(ur.GATED_TEXT.format(count=1, pods="seeded-debug/queued"), block)

    def test_recheck_for_risk_guards_reads_nodes(self):
        # An entry-14 risk guard's pool exposure comes from the node list: the guard's recorded reads
        # carry `nodes`, a scoped re-check (refreshing nothing) reads them, and the guard is refreshed.
        self.assertIn("nodes", ur.SHAPE_READS_BY_ENTRY[ur.ENTRY_CGROUP_V2])
        self.collect()
        jvm = "seeded-shapes/Deployment/cgroup-blind-jvm"
        [guard] = [g for g in ur.load_json(self.home / ur.GUARDS_FILENAME, {})["guards"] if g["id"] == ur.guard_id(SEEDED, 14, jvm, ur.GUARD_KIND_RISK)]
        self.assertIn("nodes", guard["reads"])
        later = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        ur.write_json_atomically(self.home / ur.GUARDS_FILENAME, {"version": ur.GUARDS_VERSION, "updated_at": "2026-10-08T18:00:00Z", "guards": [guard]})
        second, fleet = self.collect(now=later, since="30")
        self.assertEqual(second["reviews"], [])
        [recheck] = [r for r in second["rechecks"] if r["cluster"] == SEEDED]
        self.assertEqual((recheck["refreshed"], recheck["cleared"]), ([guard["id"]], []))
        self.assertIn("nodes", {c[2] for c in fleet.calls if c[0] == "kubectl"})
        # A guard whose recorded reads omit nodes is re-checked without them: the regression this
        # test exists for is the recorded reads, which come from SHAPE_READS_BY_ENTRY.
        without = {**guard, "reads": [r for r in guard["reads"] if r != "nodes"]}
        ur.write_json_atomically(self.home / ur.GUARDS_FILENAME, {"version": ur.GUARDS_VERSION, "updated_at": "2026-10-08T18:00:00Z", "guards": [without]})
        third, fleet = self.collect(now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc), since="30")
        self.assertNotIn("nodes", {c[2] for c in fleet.calls if c[0] == "kubectl"})

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
        # The event is gone but inference-server's pods still fail: the event guard is refreshed
        # through its object, so nothing is stale on a re-checked cluster whose reads answered.
        second, _ = self.collect(now=later)
        report = ur.render_report(second)
        self.assertEqual([i for i in second["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD], [])
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

    def test_symptom_baseline_marks_since_and_demotes_pre_existing_symptoms(self):
        first, _ = self.collect()
        seeded = next(r for r in first["reviews"] if r["cluster"] == SEEDED)
        self.assertEqual({s["since"] for s in seeded["what_failed"]}, {ur.SINCE_FIRST_SEEN})
        self.assertIn("Symptom baseline recorded: 4 symptom(s), 4 first seen.", ur.render_report(first))
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        self.assertIn(f"{INFERENCE}|pending|Unschedulable", ledger["clusters"][SEEDED]["symptoms"])
        self.assertFalse(any("Insufficient" in k for k in ledger["clusters"][SEEDED]["symptoms"]))
        # The cluster upgrades; the same symptoms are still there, plus a new one.
        bumped = cluster_doc("seeded-a")
        bumped["currentMasterVersion"] = "1.36.4-gke.1247000"
        # A new symptom with no pool gate: a webhook rejection on payments-api's ReplicaSet.
        reads = {**READS["seeded-a"], "events": READS["seeded-a"]["events"] + [event("FailedCreate", 'failed calling webhook "gate.example.io"', kind="ReplicaSet", name="payments-api-79b77b8c67", namespace="seeded-debug", last="2026-10-15T12:00:00Z")]}
        master = copy.deepcopy(next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_MASTER" and "/clusters/seeded-a" in o["targetLink"]))
        master.update(name="operation-second-master", startTime="2026-10-15T09:00:00Z", endTime="2026-10-15T09:10:00Z")
        with mock.patch.dict(READS, {"seeded-a": reads}):
            second, _ = self.collect(FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], operations=OPERATIONS + [master]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        review = next(r for r in second["reviews"] if r["cluster"] == SEEDED)
        self.assertEqual(review["what_happened"]["status"], ur.STATUS_UPGRADED)
        since = {s["object"]: s["since"] for s in review["what_failed"]}
        self.assertEqual(since[INFERENCE], ur.SINCE_BEFORE)
        self.assertEqual(since[PAYMENTS], ur.SINCE_NEW)
        inference = next(i for i in second["sections"]["warnings"] if i["object"] == INFERENCE)
        self.assertTrue(inference["predates_upgrade"])
        self.assertNotIn(INFERENCE, [i["object"] for i in second["sections"]["errors"]])
        self.assertIn(PAYMENTS, [i["object"] for i in second["sections"]["errors"]])
        report = ur.render_report(second)
        self.assertIn(f"| Unschedulable | {ur.SINCE_BEFORE} | 2. No spare capacity", report)
        self.assertIn(ur.PREDATES_UPGRADE_TEXT, report)

    def test_full_run_records_and_changes_the_fleet_set(self):
        first, _ = self.collect()
        self.assertFalse(first["scoped"])
        self.assertEqual(first["fleet"], [PROJECT])
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})[ur.LEDGER_PROJECTS_KEY], [PROJECT])
        # A project joins: the fleet grows; its listing is empty here.
        grown, _ = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc), scope_projects=f"{PROJECT},other-project")
        self.assertEqual(grown["fleet"], [PROJECT, "other-project"])
        # A project leaves: its clusters and guards go with it.
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        ledger["clusters"]["other-project/us-central1-a/elsewhere"] = {"control_plane": "1.0.0", "node_pools": {}, "channel": "", "first_seen": "2026-10-01T00:00:00Z", "last_run": "2026-10-01T00:00:00Z"}
        ur.write_json_atomically(self.home / ur.LEDGER_FILENAME, ledger)
        shrunk, _ = self.collect(now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(shrunk["fleet"], [PROJECT])
        self.assertEqual(shrunk["removed_clusters"], ["other-project/us-central1-a/elsewhere"])
        self.assertNotIn("other-project/us-central1-a/elsewhere", ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"])

    def test_scoped_run_never_adds_to_the_fleet(self):
        # No --full (discovery, no --project): scoped, nothing recorded, every cluster outside the (empty) fleet.
        result, _ = self.collect(project=None, full=False)
        self.assertTrue(result["scoped"])
        self.assertIn(ur.NOT_FULL_TEXT, result["scope_reason"])
        self.assertEqual(result["outside_fleet"], [GEMMA, SEEDED])
        self.assertEqual(result["guards"], [])
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        # Outside the fleet: no versions, no symptom set, no guards; only the operations reviewed, so
        # the after-upgrade route does not meet the same operation as new on every wake.
        self.assertEqual(ledger[ur.LEDGER_PROJECTS_KEY], [])
        self.assertEqual({k: set(v) for k, v in ledger["clusters"].items()}, {GEMMA: {ur.LEDGER_OPERATIONS_KEY}, SEEDED: {ur.LEDGER_OPERATIONS_KEY}})
        self.assertEqual([o["route"] for o in ledger["clusters"][SEEDED][ur.LEDGER_OPERATIONS_KEY]], [ur.ROUTE_SCOPED] * 4)
        self.assertIn(f"### {SEEDED} — 4 incident(s) above; {ur.OUTSIDE_FLEET_TEXT}", ur.render_report(result))
        self.assertTrue(Path(result["report_path"]).name.endswith("-scoped.md"))
        self.assertFalse((self.home / ur.REPORTS_SUBDIR / ur.LATEST_REPORT_LINK).exists())
        # After a full run of one project, a scoped --cluster run on a cluster of another project records nothing for it.
        self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        twin = cluster_doc("seeded-a")
        twin["project"] = "other-project"
        other_key = f"other-project/{LOCATION}/seeded-a"
        scoped, _ = self.collect(FakeFleet(clusters=[twin]), now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc), scope_projects=None, project="other-project", cluster=[other_key])
        self.assertEqual(scoped["outside_fleet"], [other_key])
        self.assertNotIn(other_key, {g["cluster"] for g in scoped["guards"]})
        self.assertEqual(set(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][other_key]), {ur.LEDGER_OPERATIONS_KEY})
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})[ur.LEDGER_PROJECTS_KEY], [PROJECT])
        # The next full run prunes the entry with its project.
        self.collect(now=datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc))
        self.assertNotIn(other_key, ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"])

    def test_full_run_refreshes_every_fleet_cluster_symptom_set(self):
        self.collect()
        healthy = {**READS["seeded-a"], "pods": [p for p in READS["seeded-a"]["pods"] if "payments-api" not in p["metadata"]["name"]]}
        with mock.patch.dict(READS, {"seeded-a": healthy}):
            second, fleet = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(second["reviews"], [])
        [recheck] = [r for r in second["rechecks"] if r["cluster"] == SEEDED]
        self.assertIsNotNone(recheck["symptom_baseline"])
        self.assertNotIn(f"{PAYMENTS}|not-ready|CrashLoopBackOff", recheck["symptom_baseline"])
        entry = ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED]
        self.assertEqual((entry["symptoms_refreshed"], entry["last_run"]), ("2026-10-15T18:00:00Z", "2026-10-08T18:00:00Z"))
        self.assertNotIn(f"{PAYMENTS}|not-ready|CrashLoopBackOff", entry["symptoms"])
        self.assertIn("symptom set refreshed", ur.render_report(second))
        kinds = {c[2] for c in fleet.calls if c[0] == "kubectl"}
        self.assertTrue({"pods", "nodes", "replicasets,jobs"} <= kinds)
        self.assertNotIn("events", kinds)
        # A scoped run refreshes nothing.
        third, _ = self.collect(now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc), since="30")
        self.assertTrue(all(r["symptom_baseline"] is None for r in third["rechecks"]))

    def test_onset_before_the_first_operation_is_a_warning_on_a_first_run(self):
        reads = copy.deepcopy(READS["seeded-a"])
        for p in reads["pods"]:
            if p["metadata"]["name"].startswith("inference-server") and p["status"]["phase"] == "Pending":
                for cond in p["status"]["conditions"]:
                    if cond["type"] == "PodScheduled":
                        cond["lastTransitionTime"] = "2026-10-01T00:00:00Z"
        with mock.patch.dict(READS, {"seeded-a": reads}):
            result, _ = self.collect()
        inference = next(i for i in result["sections"]["warnings"] if i["object"] == INFERENCE)
        self.assertTrue(inference["predates_upgrade"])
        self.assertIn(ur.FIRST_RUN_GRADING_TEXT, ur.render_report(result))

    def test_since_is_a_hand_run_that_widens_and_advances_nothing(self):
        self.collect()
        before = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        result, _ = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc), since="30", cluster=[SEEDED])
        self.assertTrue(result["scoped"])
        self.assertIn("--since widens the window by hand", result["scope_reason"])
        review = result["reviews"][0]
        self.assertEqual(review["what_happened"]["window_start"], "2026-09-15T18:00:00Z")
        self.assertEqual(len(review["what_happened"]["operations"]), 4)
        after = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        self.assertEqual(after["clusters"][SEEDED]["last_run"], before["clusters"][SEEDED]["last_run"])
        self.assertEqual(after["clusters"][SEEDED]["symptoms"], before["clusters"][SEEDED]["symptoms"])

    def test_after_upgrade_reviews_a_settled_unreviewed_operation_once(self):
        self.collect()  # Sunday: the full run records the operations it reviewed
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        recorded = ledger["clusters"][SEEDED][ur.LEDGER_OPERATIONS_KEY]
        self.assertEqual([(o["route"], o["reviewed_at"]) for o in recorded], [(ur.ROUTE_FULL, "2026-10-08T18:00:00Z")] * 4)
        self.assertEqual({o["id"] for o in recorded}, {o["name"] for o in ops_for("seeded-a")})
        later = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        # Nothing new since: the after-upgrade run reads no cluster, prints nothing, writes nothing.
        quiet, fleet = self.collect(now=later, full=False, after_upgrade=True)
        self.assertEqual((quiet["quiet"], quiet["report"], quiet["reviews"], quiet["scoped"]), (True, "", [], True))
        self.assertFalse(any("get-credentials" in " ".join(c) or c[0] == "kubectl" for c in fleet.calls))
        self.assertEqual(sorted(p.name for p in (self.home / ur.REPORTS_SUBDIR).iterdir()), ["20261008T180000Z.json", "20261008T180000Z.md", ur.LATEST_REPORT_LINK])
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["updated_at"], "2026-10-08T18:00:00Z")
        # A pool upgrade on seeded-a that ended thirty minutes ago: selected by the operation alone,
        # reviewed as a scoped run; gemma, with nothing new, is neither read nor listed.
        fresh = copy.deepcopy(next(o for o in ops_for("seeded-a") if "pinned-inference-pool" in o["targetLink"]))
        fresh.update(name="operation-after-1", startTime="2026-10-15T17:00:00Z", endTime="2026-10-15T17:30:00Z")
        result, fleet = self.collect(FakeFleet(operations=OPERATIONS + [fresh]), now=later, full=False, after_upgrade=True)
        self.assertEqual([(r["cluster"], r["what_happened"]["status"]) for r in result["reviews"]], [(SEEDED, "upgraded")])
        self.assertEqual(result["reviews"][0]["what_happened"]["reasons"], [ur.AFTER_UPGRADE_REASON_FORMAT.format(count=1, floor="2026-10-15T17:45:00Z")])
        self.assertEqual([op["name"] for op in result["reviews"][0]["what_happened"]["operations"]], ["operation-after-1"])
        self.assertEqual((result["scoped"], result["after_upgrade"], result["scope_reason"], result["unchanged"]), (True, True, ur.AFTER_UPGRADE_SCOPE_TEXT, []))
        self.assertFalse(any("gemma" in " ".join(c) for c in fleet.calls if "get-credentials" in c))
        self.assertTrue(Path(result["report_path"]).name.endswith("-scoped.md"))
        self.assertEqual(os.readlink(self.home / ur.REPORTS_SUBDIR / ur.LATEST_REPORT_LINK), "20261008T180000Z.md")
        self.assertIn(ur.INFO_SCOPED.format(reason=ur.AFTER_UPGRADE_SCOPE_TEXT), ur.render_report(result))
        # The ledger: the operation reviewed, by the after-upgrade route; versions, last_run and the
        # symptom set stay the full run's.
        after = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        self.assertEqual({k: after["clusters"][SEEDED][k] for k in ("control_plane", "node_pools", "last_run", "symptoms")}, {k: ledger["clusters"][SEEDED][k] for k in ("control_plane", "node_pools", "last_run", "symptoms")})
        self.assertEqual([(o["id"], o["route"], o["reviewed_at"], o["end"]) for o in after["clusters"][SEEDED][ur.LEDGER_OPERATIONS_KEY] if o["route"] != ur.ROUTE_FULL], [("operation-after-1", ur.ROUTE_AFTER_UPGRADE, "2026-10-15T18:00:00Z", "2026-10-15T17:30:00Z")])
        self.assertEqual(after["clusters"][GEMMA], ledger["clusters"][GEMMA])
        # A second wake for the same operation: nothing new, nothing printed.
        again, _ = self.collect(FakeFleet(operations=OPERATIONS + [fresh]), now=later + timedelta(minutes=15), full=False, after_upgrade=True)
        self.assertTrue(again["quiet"])
        # The next full run still selects the cluster on the operation's end time, refreshes its
        # baseline, and reports the operation as already reviewed by the after-upgrade route.
        full, _ = self.collect(FakeFleet(operations=OPERATIONS + [fresh]), now=datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc))
        seeded = next(r for r in full["reviews"] if r["cluster"] == SEEDED)
        [op] = seeded["what_happened"]["operations"]
        self.assertEqual(op["already_reviewed"], {"route": ur.ROUTE_AFTER_UPGRADE, "at": "2026-10-15T18:00:00Z"})
        self.assertIn("| DONE; " + ur.ALREADY_REVIEWED_FORMAT.format(route=ur.ROUTE_AFTER_UPGRADE, at="2026-10-15T18:00:00Z") + " |", ur.render_report(full))
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED]["last_run"], "2026-10-18T18:00:00Z")

    def test_after_upgrade_without_a_full_run_says_no_baseline_and_writes_nothing(self):
        # No full run has recorded the fleet: the route has nothing to diff against. One line, exit
        # 0, nothing read, nothing written.
        result, fleet = self.collect(full=False, after_upgrade=True)
        self.assertEqual((result["quiet"], result["no_baseline"], result["report"]), (True, True, ur.NO_BASELINE_TEXT + "\n"))
        self.assertEqual(fleet.calls, [])
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                rc = ur.main(["--after-upgrade", "--project", PROJECT])
        self.assertEqual((rc, out.getvalue()), (0, ur.NO_BASELINE_TEXT + "\n"))
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [ur.LOCK_FILENAME])

    def test_after_upgrade_wake_outside_the_fleet_reviews_an_operation_once(self):
        # The fleet records one project; a wake over another (a project that joined between two
        # Sunday runs) reviews the settled operations once, records them for the cluster, and the
        # next wake is quiet.
        self.collect()
        twin = cluster_doc("seeded-a")
        twin["project"] = "other-project"
        other_key = f"other-project/{LOCATION}/seeded-a"
        first, _ = self.collect(FakeFleet(clusters=[twin]), now=NOW + timedelta(hours=1), full=False, after_upgrade=True, scope_projects="other-project")
        self.assertEqual(([r["cluster"] for r in first["reviews"]], first["outside_fleet"]), ([other_key], [other_key]))
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        self.assertEqual((ledger[ur.LEDGER_PROJECTS_KEY], set(ledger["clusters"][other_key])), ([PROJECT], {ur.LEDGER_OPERATIONS_KEY}))
        self.assertEqual([o["route"] for o in ledger["clusters"][other_key][ur.LEDGER_OPERATIONS_KEY]], [ur.ROUTE_AFTER_UPGRADE] * 4)
        second, fleet = self.collect(FakeFleet(clusters=[twin]), now=NOW + timedelta(hours=2), full=False, after_upgrade=True, scope_projects="other-project")
        self.assertTrue(second["quiet"])
        self.assertFalse(any("get-credentials" in " ".join(c) for c in fleet.calls))
        # The sweep wrote the outside cluster no guard, and its ledger entry holds the operations
        # reviewed alone; the next full run that names the project adds it to the fleet set and
        # reviews the cluster in full, its versions recorded and its operations already reviewed.
        self.assertEqual([g for g in first["guards"] if g["cluster"] == other_key], [])
        third, _ = self.collect(FakeFleet(clusters=[cluster_doc("seeded-a"), cluster_doc("gemma-gpu-upgraded"), twin]), now=NOW + timedelta(days=7), scope_projects=f"{PROJECT},other-project")
        self.assertIn(other_key, [r["cluster"] for r in third["reviews"]])
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        self.assertEqual(ledger[ur.LEDGER_PROJECTS_KEY], [PROJECT, "other-project"])
        entry = ledger["clusters"][other_key]
        self.assertEqual((entry["control_plane"], entry["last_run"]), (twin["currentMasterVersion"], ur.fmt_ts(NOW + timedelta(days=7))))
        review = next(r for r in third["reviews"] if r["cluster"] == other_key)
        self.assertTrue(review["reviewed"])
        self.assertEqual({op.get("already_reviewed", {}).get("route") for op in review["what_happened"]["operations"]}, {ur.ROUTE_AFTER_UPGRADE})
        self.assertTrue(any(g["cluster"] == other_key for g in third["guards"]))

    def test_after_upgrade_holds_an_operation_under_fifteen_minutes_old(self):
        self.collect()
        later = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        just_ended = copy.deepcopy(next(o for o in ops_for("seeded-a") if "pinned-inference-pool" in o["targetLink"]))
        just_ended.update(name="operation-fresh", startTime="2026-10-15T17:40:00Z", endTime="2026-10-15T17:50:00Z")
        # Alone, the fresh operation holds its cluster back and the run is quiet.
        quiet, fleet = self.collect(FakeFleet(operations=OPERATIONS + [just_ended]), now=later, full=False, after_upgrade=True)
        self.assertTrue(quiet["quiet"])
        self.assertEqual([(u["cluster"], u["operation"]["name"], u.get("settling")) for u in quiet["upgrading"]], [(SEEDED, "operation-fresh", True)])
        self.assertFalse(any("get-credentials" in " ".join(c) for c in fleet.calls))
        # Beside a settled operation on another cluster, the held cluster is listed with its reason and
        # not reviewed; the next wake, fifteen minutes on, reviews it.
        gemma_op = copy.deepcopy(just_ended)
        gemma_op.update(name="operation-gemma", targetLink=just_ended["targetLink"].replace("/clusters/seeded-a/nodePools/pinned-inference-pool", "/clusters/gemma-gpu-upgraded/nodePools/gpu-pool"), startTime="2026-10-15T17:00:00Z", endTime="2026-10-15T17:20:00Z")
        result, _ = self.collect(FakeFleet(operations=OPERATIONS + [just_ended, gemma_op]), now=later, full=False, after_upgrade=True)
        self.assertEqual([r["cluster"] for r in result["reviews"]], [GEMMA])
        self.assertIn("- " + ur.SETTLING_LINE.format(cluster=SEEDED, operation="UPGRADE_NODES", target="pinned-inference-pool", end="2026-10-15T17:50:00Z", minutes=15), ur.render_report(result))
        self.assertNotIn("operation-fresh", json.dumps(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED][ur.LEDGER_OPERATIONS_KEY]))
        # Fifteen minutes on, a settled and a fresher operation on the one cluster: the floor applies
        # per cluster, so it waits whole and nothing is written (a review of the settled half would
        # grade the fresher pool's displaced pods against a window that does not name their
        # operation); the held line counts the settled operation waiting with it.
        fresher = copy.deepcopy(just_ended)
        fresher.update(name="operation-fresher", startTime="2026-10-15T18:05:00Z", endTime="2026-10-15T18:10:00Z")
        waiting, _ = self.collect(FakeFleet(operations=OPERATIONS + [just_ended, gemma_op, fresher]), now=later + timedelta(minutes=15), full=False, after_upgrade=True)
        self.assertTrue(waiting["quiet"])
        self.assertEqual(waiting["reviews"], [])
        self.assertEqual([(u["cluster"], u["operation"]["name"], u.get("settling"), u.get("waiting")) for u in waiting["upgrading"]], [(SEEDED, "operation-fresher", True, 1)])
        self.assertEqual(ur._upgrading_line(waiting["upgrading"][0], str), ur.SETTLING_LINE.format(cluster=SEEDED, operation="UPGRADE_NODES", target="pinned-inference-pool", end="2026-10-15T18:10:00Z", minutes=15) + ur.SETTLED_WAITING_FORMAT.format(count=1))
        recorded = ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED][ur.LEDGER_OPERATIONS_KEY]
        self.assertEqual([o["id"] for o in recorded if o["route"] == ur.ROUTE_AFTER_UPGRADE], [])
        # Once both have settled, one review covers both pools and records both operations.
        last, _ = self.collect(FakeFleet(operations=OPERATIONS + [just_ended, gemma_op, fresher]), now=later + timedelta(minutes=30), full=False, after_upgrade=True)
        self.assertEqual([r["cluster"] for r in last["reviews"]], [SEEDED])
        self.assertEqual([op["name"] for op in last["reviews"][0]["what_happened"]["operations"]], ["operation-fresh", "operation-fresher"])
        self.assertEqual([op.get("already_reviewed") or None for op in last["reviews"][0]["what_happened"]["operations"]], [None, None])
        self.assertIn(ur.AFTER_UPGRADE_REASON_FORMAT.format(count=2, floor=ur.fmt_ts(later + timedelta(minutes=30) - ur.AFTER_UPGRADE_SETTLE)), last["reviews"][0]["what_happened"]["reasons"])
        recorded = ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED][ur.LEDGER_OPERATIONS_KEY]
        self.assertEqual([o["id"] for o in recorded if o["route"] == ur.ROUTE_AFTER_UPGRADE], ["operation-fresh", "operation-fresher"])

    def test_usage_errors_are_refused_before_the_lock_and_the_state_files(self):
        # A refused flag set neither waits on another run's lock nor reads a state file: the
        # operator is told the flags are wrong at once, and a corrupt ledger is not set aside by a
        # run that was never going to proceed.
        (self.home / ur.LEDGER_FILENAME).write_text("{not json")
        refused = (
            (["--full", "--scope-projects", PROJECT, "--cluster", SEEDED], ur.FULL_WITH_CLUSTER_TEXT),
            (["--full", "--project", PROJECT], ur.FULL_WITH_PROJECT_TEXT),
            (["--full", "--scope-projects", PROJECT, "--since", "7"], ur.FULL_WITH_SINCE_TEXT),
            (["--after-upgrade", "--full", "--scope-projects", PROJECT], ur.AFTER_UPGRADE_WITH_FULL_TEXT),
            (["--after-upgrade", "--cluster", SEEDED], ur.AFTER_UPGRADE_WITH_CLUSTER_TEXT),
            (["--project", PROJECT, "--report", str(self.home / ur.LATEST_REPORT_LINK)], ur.REPORT_IS_LINK_TEXT.format(name=ur.LATEST_REPORT_LINK)),
            (["--project", PROJECT, "--since", "yesterday"], "--since takes"),
        )
        with mock.patch.object(ur, "acquire_lock", side_effect=AssertionError("the lock must not be taken")), mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            for argv, text in refused:
                with self.subTest(argv=argv):
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                        self.assertEqual(ur.main(argv), ur.EXIT_USAGE)
                    self.assertIn(text, err.getvalue())
        self.assertEqual((self.home / ur.LEDGER_FILENAME).read_text(), "{not json")
        self.assertEqual([p.name for p in self.home.glob("*unreadable*")], [])

    def test_kubeconfig_directory_and_a_stale_kubeconfig_survive_a_root_hand_run(self):
        # The shared `.kubeconfigs` is made usable by `collect.prepare_kubeconfig` before gcloud runs,
        # in either order of uids: fresh under any umask it is sticky and world-writable; one this uid
        # made at another mode is widened; another uid's (a root hand run's) is moved aside and remade;
        # a stale kubeconfig this uid cannot write is removed (root can write any file and keeps it);
        # a directory that cannot be made usable is the read's error.
        import collect as fleet_collect  # noqa: PLC0415 -- the shared rule under test

        seeded = {"project": PROJECT, "name": "seeded-a", "location": LOCATION}
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: home}):
            kc = ur.kubeconfig_path(PROJECT, "seeded-a", LOCATION)
            self.assertEqual(kc, Path(home) / ur.KUBECONFIG_SUBDIR / f"kubeconfig_{PROJECT}_seeded-a_{LOCATION}.yaml")
            mask = os.umask(0o077)
            try:
                self.assertEqual(ur.fetch_credentials(seeded, run=FakeFleet()), (kc, None))
            finally:
                os.umask(mask)
            self.assertEqual(stat.S_IMODE(kc.parent.stat().st_mode), fleet_collect.KUBECONFIG_DIR_MODE)
            kc.parent.chmod(0o755)
            kc.write_text("stale")
            kc.chmod(0o400)
            self.assertEqual(ur.fetch_credentials(seeded, run=FakeFleet()), (kc, None))
            self.assertEqual((stat.S_IMODE(kc.parent.stat().st_mode), kc.exists()), (fleet_collect.KUBECONFIG_DIR_MODE, os.geteuid() == 0))
            # Another uid's directory: moved aside in the parent, which is this uid's, and remade.
            kc.write_text("root's")
            with mock.patch.object(os, "geteuid", return_value=kc.parent.stat().st_uid + 1), redirect_stderr(io.StringIO()):
                self.assertEqual(ur.fetch_credentials(seeded, run=FakeFleet()), (kc, None))
            self.assertFalse(kc.exists())
            self.assertEqual(len([p for p in Path(home).iterdir() if p.name.startswith(ur.KUBECONFIG_SUBDIR + ".unusable-")]), 1)
            self.assertEqual(stat.S_IMODE(kc.parent.stat().st_mode), fleet_collect.KUBECONFIG_DIR_MODE)
            with mock.patch.object(fleet_collect, "prepare_kubeconfig", return_value="refused"):
                self.assertEqual(ur.fetch_credentials(seeded, run=FakeFleet()), (kc, "get-credentials: refused"))
            # A root hand run after the tick: from root's side the directory is another uid's, and root
            # keeps it rather than moving the tick's files aside (the `os.access` keep of a stale file
            # root can write is the one branch that needs a real root).
            kc.write_text("the tick's")
            with mock.patch.object(os, "geteuid", return_value=fleet_collect.ROOT_UID):
                self.assertEqual(ur.fetch_credentials(seeded, run=FakeFleet()), (kc, None))
            self.assertEqual((kc.exists(), stat.S_IMODE(kc.parent.stat().st_mode), len([p for p in Path(home).iterdir() if p.name.startswith(ur.KUBECONFIG_SUBDIR + ".unusable-")])), (True, fleet_collect.KUBECONFIG_DIR_MODE, 1))
            # The rename race in the worker pool: the thread whose rename finds the directory already
            # moved (the winner remade it between this thread's stat and rename) reads what stands there
            # and reports no refusal, so its cluster is read this run.
            for earlier in [p for p in Path(home).iterdir() if p.name.startswith(ur.KUBECONFIG_SUBDIR + ".unusable-")]:
                earlier.rename(earlier.with_name("earlier-" + earlier.name))
            real_rename = Path.rename

            def winner_first(path, target):
                real_rename(path, target)
                path.mkdir(0o755)
                raise FileNotFoundError(errno.ENOENT, "No such file or directory")

            with mock.patch.object(os, "geteuid", return_value=kc.parent.stat().st_uid + 1), mock.patch.object(Path, "rename", winner_first), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(ur.fetch_credentials(seeded, run=FakeFleet()), (kc, None))
            self.assertNotIn("could not be moved aside", err.getvalue())
            self.assertEqual((len([p for p in Path(home).iterdir() if p.name.startswith(ur.KUBECONFIG_SUBDIR + ".unusable-")]), stat.S_IMODE(kc.parent.stat().st_mode)), (1, fleet_collect.KUBECONFIG_DIR_MODE))

    def test_the_shared_kubeconfig_directory_is_prepared_once_before_the_pools_open(self):
        # One preparation before any credentials are fetched, so the first wave's threads never race one
        # another's move-aside of a directory another uid left; the per-cluster call then finds the
        # directory this run's. With one worker the order is exact: the pre-pool preparation, then each
        # cluster's own before its `get-credentials`.
        import collect as fleet_collect  # noqa: PLC0415 -- the shared rule under test

        sequence = []
        fleet = FakeFleet()
        real = fleet_collect.usable_kubeconfig_dir

        def prepared(directory):
            sequence.append("prepare")
            return real(directory)

        def run(argv, **kwargs):
            if argv[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
                sequence.append("credentials")
            return fleet(argv, **kwargs)

        with mock.patch.object(fleet_collect, "usable_kubeconfig_dir", prepared), redirect_stderr(io.StringIO()):
            ur.collect(args(), run=run, now=NOW, max_workers=1)
        self.assertEqual((sequence[:3], sequence.count("credentials")), (["prepare", "prepare", "credentials"], 2))

    def test_a_quiet_after_upgrade_wake_names_what_it_holds(self):
        # A wake that writes nothing says why: nothing settled is reviewable yet, and each held cluster's
        # line follows; with nothing held at all it says no settled operation awaits review.
        self.collect()
        just_ended = copy.deepcopy(next(o for o in ops_for("seeded-a") if "pinned-inference-pool" in o["targetLink"]))
        just_ended.update(name="operation-fresh", startTime="2026-10-15T17:40:00Z", endTime="2026-10-15T17:50:00Z")
        later = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        with redirect_stderr(io.StringIO()) as err:
            quiet = ur.collect(args(full=False, after_upgrade=True), run=FakeFleet(operations=OPERATIONS + [just_ended]), now=later)
        self.assertTrue(quiet["quiet"])
        logged = err.getvalue()
        self.assertIn(ur.AFTER_UPGRADE_HELD_LOG, logged)
        self.assertIn(ur.SETTLING_LINE.format(cluster=SEEDED, operation="UPGRADE_NODES", target="pinned-inference-pool", end="2026-10-15T17:50:00Z", minutes=15), logged)
        self.assertNotIn(ur.AFTER_UPGRADE_QUIET_LOG, logged)
        with redirect_stderr(io.StringIO()) as err:
            nothing = ur.collect(args(full=False, after_upgrade=True), run=FakeFleet(), now=later)
        self.assertTrue(nothing["quiet"])
        self.assertIn(ur.AFTER_UPGRADE_QUIET_LOG, err.getvalue())

    def test_an_operation_is_in_the_window_by_its_end_whatever_its_start(self):
        # A long operation (a blue-green soak) starts before the window it ends in, and one still in
        # flight has no end: the listing is bounded by end time, so both reach the rules downstream.
        pinned = next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_NODES" and "/nodePools/pinned-inference-pool" in o["targetLink"])
        long_op = {**copy.deepcopy(pinned), "name": "operation-long", "startTime": "2026-09-20T04:00:00Z", "endTime": "2026-10-07T12:00:00Z"}
        ops, error = ur.list_operations(PROJECT, SINCE, run=FakeFleet(operations=OPERATIONS + [long_op]))
        self.assertIsNone(error)
        self.assertIn("operation-long", [o["name"] for o in ops])
        result, _ = self.collect(FakeFleet(operations=OPERATIONS + [long_op]))
        review = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertIn("operation-long", [op["name"] for op in review["what_happened"]["operations"]])
        running = {**copy.deepcopy(pinned), "name": "operation-still-running", "startTime": "2026-09-01T04:00:00Z", "status": "RUNNING"}
        running.pop("endTime", None)
        held, _ = self.collect(FakeFleet(operations=OPERATIONS + [running]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual([(u["cluster"], u["operation"]["name"]) for u in held["upgrading"]], [(SEEDED, "operation-still-running")])

    def test_a_recorded_skew_keeps_its_check_whatever_the_weeks_operations(self):
        # A version skew is graded by persistence, never by onset: once recorded it files under
        # `upgrade-symptom-tentative` on a week with an operation in the window as on one without, so
        # the finding's identity does not flip and its guard keeps its check.
        gemma = cluster_doc("gemma-gpu-upgraded")
        gemma["nodePools"][0]["version"] = "1.30.5-gke.1000000"
        gemma["maintenancePolicy"]["window"].pop("maintenanceExclusions")
        master = copy.deepcopy(next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_MASTER" and "/clusters/seeded-a" in o["targetLink"]))
        master.update(name="operation-gemma-master", targetLink=master["targetLink"].replace("/clusters/seeded-a", "/clusters/gemma-gpu-upgraded"), startTime="2026-10-14T04:00:00Z", endTime="2026-10-14T04:20:00Z")
        guard = ur.guard_id(GEMMA, 5, "NodePool/default-pool")
        self.collect(FakeFleet(clusters=[cluster_doc("seeded-a"), gemma]))
        with_op, _ = self.collect(FakeFleet(clusters=[cluster_doc("seeded-a"), gemma], operations=OPERATIONS + [master]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        [review] = [r for r in with_op["reviews"] if r["cluster"] == GEMMA]
        self.assertEqual(review["what_happened"]["status"], ur.STATUS_UPGRADED)
        [incident] = [i for i in with_op["sections"]["warnings"] if i["cluster"] == GEMMA and i["object"] == "NodePool/default-pool"]
        self.assertEqual((incident["check"], incident["predates_upgrade"]), (ur.CHECK_SYMPTOM_TENTATIVE, False))
        self.assertNotIn(ur.PREDATES_UPGRADE_TEXT, ur.render_report(with_op).split("`NodePool/default-pool`")[1].split("### ")[0])
        self.assertEqual([g["check"] for g in with_op["guards"] if g["id"] == guard], [ur.CHECK_SYMPTOM_TENTATIVE])
        without_op, _ = self.collect(FakeFleet(clusters=[cluster_doc("seeded-a"), gemma], operations=OPERATIONS + [master]), now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc))
        [review] = [r for r in without_op["reviews"] if r["cluster"] == GEMMA]
        self.assertEqual(review["what_happened"]["status"], ur.STATUS_REGRADED)
        [incident] = [i for i in without_op["sections"]["warnings"] if i["cluster"] == GEMMA and i["object"] == "NodePool/default-pool"]
        self.assertEqual(incident["check"], ur.CHECK_SYMPTOM_TENTATIVE)
        self.assertEqual([(g["check"], g["reviews"]) for g in without_op["guards"] if g["id"] == guard], [(ur.CHECK_SYMPTOM_TENTATIVE, 2)])

    def test_the_linked_report_is_never_pruned(self):
        # `--full --no-report` writes a JSON stamp and no Markdown, and moves no link: after fourteen such
        # runs the newest stamps are JSON-only and the report the link names would age out of the kept
        # set; the link's stamp is kept whatever its age.
        self.collect()
        reports = self.home / ur.REPORTS_SUBDIR
        link = reports / ur.LATEST_REPORT_LINK
        target = os.readlink(link)
        for day in range(1, ur.REPORTS_KEPT + 2):
            self.collect(no_report=True, now=NOW + timedelta(days=day))
        self.assertEqual(os.readlink(link), target)
        self.assertTrue((reports / target).exists())
        self.assertTrue(link.resolve().exists())
        self.assertEqual(len(list(reports.glob("*Z.json"))), ur.REPORTS_KEPT + 1)

    def test_caller_named_output_parents_keep_the_callers_umask_and_their_files_are_the_stores_mode(self):
        # `make_store_dir` clears the umask for the store's own directories alone: a parent a caller-named
        # `--output`, `--manifest-file` or `--report` creates outside the store is made under the
        # caller's umask, never world-writable, while `reports/` under the store stays 0777. The files
        # inside are `STORE_FILE_MODE` (0644) as every file the collector writes: `mkstemp` would make
        # them 0600 under any umask, and a root hand run's manifest must stay readable by the uid-1000
        # `finish` that reads it.
        with tempfile.TemporaryDirectory() as outside:
            out = Path(outside) / "retro" / "out.json"
            manifest = Path(outside) / "mf" / "m.json"
            report = Path(outside) / "rep" / "r.md"
            mask = os.umask(0o077)
            try:
                self.collect(output=str(out), manifest_file=str(manifest))
                self.collect(report=str(report), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
            finally:
                os.umask(mask)
            for created in (out.parent, manifest.parent, report.parent):
                self.assertEqual(stat.S_IMODE(created.stat().st_mode), ur.STORE_DIR_MODE & ~0o077, created)
            for written in (out, manifest, report):
                self.assertEqual(stat.S_IMODE(written.stat().st_mode), ur.STORE_FILE_MODE, written)
            self.assertEqual(stat.S_IMODE((self.home / ur.REPORTS_SUBDIR).stat().st_mode), ur.STORE_DIR_MODE)

    def test_the_declared_scope_is_swept_exactly_and_nothing_is_listed(self):
        # The fleet is the fleet_scope tool's resolved set: swept as handed, no listing, recorded as
        # the fleet; a project the next scope no longer names is pruned with its clusters and guards.
        result, fleet = self.collect(scope_projects=f"{PROJECT},other-project", manifest_file=str(self.home / "manifest.json"))
        self.assertEqual((result["projects"], result["fleet"]), ([PROJECT, "other-project"], [PROJECT, "other-project"]))
        self.assertFalse(any("projects list" in " ".join(c) or "config get-value" in " ".join(c) for c in fleet.calls))
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        self.assertNotIn("limitations", manifest)
        self.assertNotIn(ur.UNENUMERATED_PROJECTS_TARGET, [c["name"] for c in manifest["clusters"]])
        self.assertFalse(manifest["partial"])
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        other = "other-project/us-central1-a/elsewhere"
        ledger["clusters"][other] = {"control_plane": "1.0.0", "node_pools": {}, "channel": "", "first_seen": "2026-10-01T00:00:00Z", "last_run": "2026-10-01T00:00:00Z"}
        ur.write_json_atomically(self.home / ur.LEDGER_FILENAME, ledger)
        narrowed, _ = self.collect(scope_projects=PROJECT, now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual((narrowed["fleet"], narrowed["removed_clusters"]), ([PROJECT], [other]))
        self.assertNotIn(other, ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"])

    def test_a_project_override_is_free_undeclared_and_must_be_inside_a_declared_scope(self):
        # Undeclared: `--project` is a hand run's override and the run is scoped. Declared: an
        # override outside the readable scope is refused before the lock, one inside it runs.
        free, fleet = self.collect(scope_projects=None, project=PROJECT, full=False)
        self.assertEqual((free["projects"], free["scoped"]), ([PROJECT], True))
        self.assertFalse(any("projects list" in " ".join(c) for c in fleet.calls))
        with self.assertRaises(argparse.ArgumentTypeError) as caught:
            self.collect(scope_projects=f"{PROJECT},other-project", project="acme-only", full=False)
        self.assertIn("declared scope does not list", str(caught.exception))
        with self.assertRaises(argparse.ArgumentTypeError) as caught:
            self.collect(scope_projects=PROJECT, scope_unread="payments-staging=denied", project="payments-staging", full=False)
        self.assertIn("could not read", str(caught.exception))
        inside, _ = self.collect(scope_projects=f"{PROJECT},other-project", project=PROJECT, full=False)
        self.assertEqual(inside["projects"], [PROJECT])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ur.main(["--scope-projects", PROJECT, "--project", "acme-only"]), ur.EXIT_USAGE)
        self.assertIn("declared scope does not list", err.getvalue())

    def test_exit_lines_name_the_runs_projects(self):
        # The tail every exit after `start` without a manifest carries: the resolved projects,
        # `--cluster`'s included, the listing's note when it was incomplete, `none` when nothing resolved.
        self.assertEqual(ur.exit_projects_text((["b", "a"], [], None), set()), "; this run's projects: a, b")
        self.assertEqual(ur.exit_projects_text((["a"], [], "why"), {"c/us-central1-a/x"}), "; this run's projects: a, c (why)")
        self.assertEqual(ur.exit_projects_text(([], ["no project"], None), set()), "; this run's projects: none")
        with self.assertRaises(argparse.ArgumentTypeError) as caught:
            self.collect(after_upgrade=True, since="30d", full=False)
        self.assertEqual(str(caught.exception), ur.AFTER_UPGRADE_WITH_SINCE_TEXT)

    def test_a_held_guard_on_a_cluster_not_read_names_the_reason_not_the_five_reads(self):
        # A review that never reached its reads holds its guards with the review's own error, not
        # the five read names: the credentials failed, or the review raised after the reads
        # answered. A partial read still names the reads that failed.
        self.collect()
        unread, _ = self.collect(FakeFleet(broken=["seeded-a"]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc), cluster=[SEEDED])
        stale = [w for w in unread["sections"]["warnings"] if w["kind"] == ur.INCIDENT_STALE_GUARD and w["cluster"] == SEEDED]
        self.assertTrue(stale)
        report = ur.render_report(unread)
        self.assertEqual(report.count(ur.STALE_UNREAD_TEXT.format(errors="get-credentials rc=1: ERROR: cluster seeded-a not found")), len(stale))
        self.assertNotIn("read partially this run", report)

        def boom(*args, **kwargs):
            raise RuntimeError("planted")

        with mock.patch.object(ur, "collect_symptoms", boom):
            raised, _ = self.collect(now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc), cluster=[SEEDED])
        stale = [w for w in raised["sections"]["warnings"] if w["kind"] == ur.INCIDENT_STALE_GUARD and w["cluster"] == SEEDED]
        self.assertTrue(stale)
        report = ur.render_report(raised)
        self.assertEqual(report.count(ur.STALE_UNREAD_TEXT.format(errors="review failed: RuntimeError('planted')")), len(stale))
        partial, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods")]), now=datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc), cluster=[SEEDED])
        report = ur.render_report(partial)
        self.assertIn(ur.STALE_PARTIAL_TEXT.format(failed="pods"), report)
        self.assertNotIn("cluster not read this run", report)

    def test_a_skew_on_a_cluster_with_no_maintenance_policy_is_an_info_line_and_never_row_5(self):
        # Row 5 needs a window; a cluster with no maintenance policy whose pool sits two minors behind
        # gets the Info line naming the readiness watch's pool-skew rule, no entry-5 symptom on the
        # first run or the next, and is not re-selected for the skew.
        bare = cluster_doc("gemma-gpu-upgraded")
        bare.pop("maintenancePolicy")
        bare["currentMasterVersion"] = "1.32.13-gke.2427000"
        bare["nodePools"][0]["version"] = "1.30.5-gke.1000000"
        first, _ = self.collect(FakeFleet(clusters=[bare]))
        [review] = first["reviews"]
        skews_text = "pool default-pool at 1.30.5-gke.1000000, control plane at 1.32.13-gke.2427000 (2 minors apart)"
        self.assertEqual(review["skew_notes"], [ur.SKEW_NO_POLICY_TEXT.format(skews=skews_text)])
        self.assertEqual([s for s in review["what_failed"] if s["category"] == ur.CATEGORY_VERSION_SKEW], [])
        self.assertTrue(any(ur.CATEGORY_VERSION_SKEW in key for key in ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][GEMMA]["symptoms"]))
        second, _ = self.collect(FakeFleet(clusters=[bare]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(([r["cluster"] for r in second["reviews"]], [u["cluster"] for u in second["unchanged"]]), ([], [GEMMA]))
        self.assertEqual([i for i in second["sections"]["warnings"] if i["object"].startswith("NodePool/")], [])
        self.assertIn(ur.SKEW_NO_POLICY_TEXT.format(skews=skews_text), ur.render_report(first))

    def test_a_forced_cluster_reads_its_own_project_alone(self):
        # `--cluster` without `--project` reads the cluster's own project and nothing else: no
        # `projects list` on an undeclared install, no other declared project's listings, and the
        # exit line names that project alone.
        only, fleet = self.collect(FakeFleet(projects_listed=[PROJECT, "other-project"]), scope_projects=None, project=None, cluster=[SEEDED], full=False)
        self.assertEqual((only["projects"], [r["cluster"] for r in only["reviews"]]), ([PROJECT], [SEEDED]))
        self.assertFalse(any("projects list" in " ".join(c) or "config get-value" in " ".join(c) or "other-project" in " ".join(c) for c in fleet.calls))
        declared, fleet = self.collect(scope_projects=f"{PROJECT},other-project", cluster=[SEEDED], full=False, now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(declared["projects"], [PROJECT])
        self.assertFalse(any("other-project" in " ".join(c) for c in fleet.calls))
        listing = FakeFleet(projects_listed=[PROJECT, "other-project"])
        with mock.patch.object(ur, "default_run", listing), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ur.main(["--cluster", SEEDED, "--no-report"]), 0)
        self.assertFalse(any("projects list" in " ".join(c) or "other-project" in " ".join(c) for c in listing.calls))

    def test_a_malformed_cluster_key_is_a_usage_error(self):
        # `--cluster seeded-a` is a bare name, not <project>/<location>/<name>: refused before the
        # lock rather than swept as a project id; an empty part is refused the same way.
        for bad in ("seeded-a", f"{PROJECT}//seeded-a", f"{PROJECT}/{LOCATION}/seeded-a/extra"):
            with self.assertRaises(argparse.ArgumentTypeError) as caught:
                self.collect(scope_projects=None, project=None, cluster=[bad], full=False)
            self.assertEqual(str(caught.exception), ur.CLUSTER_SHAPE_TEXT.format(cluster=bad))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ur.main(["--cluster", "seeded-a"]), ur.EXIT_USAGE)
        self.assertIn(ur.CLUSTER_SHAPE_TEXT.format(cluster="seeded-a"), err.getvalue())
        self.assertFalse((self.home / ur.REPORTS_SUBDIR).exists())

    def test_a_forced_cluster_names_its_project_under_the_overrides_rule(self):
        # `--cluster` names a project as `--project` does: free on an undeclared install, and on a
        # declared one admitted only inside the readable scope; a cluster of a project outside it, or
        # of an unread one, is refused before the lock with the override's reason.
        with self.assertRaises(argparse.ArgumentTypeError) as caught:
            self.collect(scope_projects=PROJECT, cluster=["acme-only/us-central1-a/x"], full=False)
        self.assertIn("--cluster acme-only/us-central1-a/x: ", str(caught.exception))
        self.assertIn("declared scope does not list", str(caught.exception))
        with self.assertRaises(argparse.ArgumentTypeError) as caught:
            self.collect(scope_projects=PROJECT, scope_unread="payments-staging=denied", cluster=["payments-staging/us-central1-a/x"], full=False)
        self.assertIn("could not read (denied)", str(caught.exception))
        inside, _ = self.collect(scope_projects=PROJECT, cluster=[SEEDED], full=False)
        self.assertEqual(([r["cluster"] for r in inside["reviews"]], inside["scoped"]), ([SEEDED], True))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ur.main(["--scope-projects", PROJECT, "--cluster", "acme-only/us-central1-a/x"]), ur.EXIT_USAGE)
        self.assertIn("--cluster acme-only/us-central1-a/x: ", err.getvalue())
        self.assertFalse((self.home / ur.LOCK_FILENAME).exists())

    def test_a_declared_install_without_the_flags_lists_nothing(self):
        # The sandbox's scope guard: the operator says the install declares a scope, the collector got
        # no collector_args, so it reports that and lists nothing; the probe still answers.
        with mock.patch.dict(os.environ, {fleet_scope_env(): "true"}):
            with self.assertRaises(argparse.ArgumentTypeError) as caught:
                self.collect(scope_projects=None, project=None, full=False)
            self.assertIn("got no collector_args", str(caught.exception))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(ur.main(["--full"]), ur.EXIT_USAGE)
            self.assertIn("got no collector_args", err.getvalue())
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ur.main(["--probe-lock"]), 0)
            self.assertIn("is free", out.getvalue())
            # A declared scope with nothing readable is a boundary with nothing inside it.
            with self.assertRaises(argparse.ArgumentTypeError) as caught:
                self.collect(scope_projects=None, scope_unread="payments-staging=denied", project=None, full=False)
            self.assertIn("no project this install could read", str(caught.exception))
        self.assertFalse((self.home / ur.LEDGER_FILENAME).exists())
        self.assertFalse((self.home / ur.REPORTS_SUBDIR).exists())

    def test_main_hands_the_scope_flags_to_the_resolver_and_help_names_them(self):
        with mock.patch.object(ur.declared_scope, "set", side_effect=SystemExit(0)) as handed, self.assertRaises(SystemExit):
            ur.main(["--scope-projects", "ops-mgmt,payments-prod", "--scope-unread", "payments-staging=denied"])
        handed.assert_called_once_with("ops-mgmt,payments-prod", "payments-staging=denied")
        proc = subprocess.run([sys.executable, ur.__file__, "--help"], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--scope-projects", proc.stdout)
        self.assertIn("--scope-unread", proc.stdout)

    def test_after_upgrade_rejects_full_and_cluster(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            self.collect(full=True, after_upgrade=True)
        with self.assertRaises(argparse.ArgumentTypeError):
            self.collect(full=False, after_upgrade=True, cluster=[SEEDED])
        for argv, text in ((["--after-upgrade", "--full", "--scope-projects", PROJECT], ur.AFTER_UPGRADE_WITH_FULL_TEXT), (["--after-upgrade", "--cluster", SEEDED], ur.AFTER_UPGRADE_WITH_CLUSTER_TEXT), (["--after-upgrade", "--since", "30d"], ur.AFTER_UPGRADE_WITH_SINCE_TEXT)):
            with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(ur.main(argv), ur.EXIT_USAGE)
            self.assertIn(text, err.getvalue())

    def test_scoped_run_writes_only_the_operations_it_reviewed(self):
        self.collect()
        before = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        # A --cluster run (no --since) reviews seeded-a again over the widened window: its four
        # operations are already on record from the full run and the report says so; versions,
        # last_run and the symptom set do not move.
        result, _ = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc), cluster=[SEEDED])
        [review] = result["reviews"]
        self.assertEqual({op["already_reviewed"]["route"] for op in review["what_happened"]["operations"]}, {ur.ROUTE_FULL})
        after = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        self.assertEqual(after["clusters"][SEEDED], before["clusters"][SEEDED])
        self.assertEqual(after["clusters"][GEMMA], before["clusters"][GEMMA])
        # A scoped run on a cluster the ledger has never seen records only the operations it reviewed;
        # the next full run still reviews the cluster as first seen.
        twin = cluster_doc("seeded-a")
        twin["name"] = "seeded-twin"
        twin_ops = copy.deepcopy(ops_for("seeded-a"))
        for o in twin_ops:
            o["targetLink"] = o["targetLink"].replace("/clusters/seeded-a", "/clusters/seeded-twin")
            o["name"] += "-twin"
        twin_key = f"{PROJECT}/{LOCATION}/seeded-twin"
        fleet = FakeFleet(clusters=[cluster_doc("seeded-a"), cluster_doc("gemma-gpu-upgraded"), twin], operations=OPERATIONS + twin_ops)
        with mock.patch.dict(READS, {"seeded-twin": READS["seeded-a"]}):
            scoped, _ = self.collect(fleet, now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc), cluster=[twin_key])
            self.assertEqual([(r["cluster"], r["what_happened"]["status"], r["what_happened"]["versions_before"], r["reviewed"]) for r in scoped["reviews"]], [(twin_key, "new", None, True)])
            entry = ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][twin_key]
            self.assertEqual(set(entry), {ur.LEDGER_OPERATIONS_KEY})
            self.assertEqual([o["route"] for o in entry[ur.LEDGER_OPERATIONS_KEY]], [ur.ROUTE_SCOPED] * 4)
            full, _ = self.collect(fleet, now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        twin_review = next(r for r in full["reviews"] if r["cluster"] == twin_key)
        self.assertEqual((twin_review["what_happened"]["status"], twin_review["what_happened"]["versions_before"]), ("new", None))
        self.assertEqual({op["already_reviewed"]["route"] for op in twin_review["what_happened"]["operations"]}, {ur.ROUTE_SCOPED})

    def test_reviewed_operations_are_kept_while_inside_the_selection_window(self):
        self.collect()
        self.assertEqual(len(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED][ur.LEDGER_OPERATIONS_KEY]), 4)
        # Three weeks on, the window (the smaller of --since and last_run) has moved past every
        # recorded operation: none can be selected again, so none is kept.
        self.collect(now=datetime(2026, 10, 30, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED][ur.LEDGER_OPERATIONS_KEY], [])

    def test_entry_3_guard_survives_a_recheck_while_the_workload_stays_unavailable(self):
        workloads = copy.deepcopy(READS["seeded-a"]["workloads"])
        inventory = next(w for w in workloads if w["metadata"]["name"] == "inventory-api")
        inventory["spec"]["replicas"] = 2
        for cond in inventory["status"]["conditions"]:
            if cond["type"] == "Available":
                cond["lastTransitionTime"] = "2026-10-07T04:08:00Z"
        pods = copy.deepcopy(READS["seeded-a"]["pods"])
        next(p for p in pods if p["metadata"]["name"].startswith("inventory-api-"))["spec"]["nodeName"] = "gke-seeded-a-default-pool-62ac8ee0-d595"
        guard = ur.guard_id(SEEDED, 3, "seeded-stall/Deployment/inventory-api")
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "workloads": workloads, "pods": pods}}):
            first, _ = self.collect()
            self.assertIn(guard, {g["id"] for g in first["guards"]})
            self.assertIn(f"### 3, unclassified — {SEEDED} — `seeded-stall/Deployment/inventory-api`", ur.render_report(first))
            # Unchanged a week on: the operation is outside the window, the Deployment still unavailable;
            # the re-check reads the workloads and keeps the guard.
            second, fleet = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in second["rechecks"] if r["cluster"] == SEEDED]
        self.assertIn(guard, recheck["refreshed"])
        self.assertIn("workloads", recheck["commands"])
        self.assertIn(guard, {g["id"] for g in second["guards"]})
        # Available again: cleared.
        for cond in inventory["status"]["conditions"]:
            if cond["type"] == "Available":
                cond["status"] = "True"
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "workloads": workloads, "pods": pods}}):
            third, _ = self.collect(now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in third["rechecks"] if r["cluster"] == SEEDED]
        self.assertIn(guard, recheck["cleared"])
        self.assertNotIn(guard, {g["id"] for g in third["guards"]})

    def test_entry_3_guard_survives_a_full_review_while_the_workload_stays_unavailable(self):
        workloads = copy.deepcopy(READS["seeded-a"]["workloads"])
        inventory = next(w for w in workloads if w["metadata"]["name"] == "inventory-api")
        inventory["spec"]["replicas"] = 2
        for cond in inventory["status"]["conditions"]:
            if cond["type"] == "Available":
                cond["lastTransitionTime"] = "2026-10-07T04:08:00Z"
        pods = copy.deepcopy(READS["seeded-a"]["pods"])
        next(p for p in pods if p["metadata"]["name"].startswith("inventory-api-"))["spec"]["nodeName"] = "gke-seeded-a-default-pool-62ac8ee0-d595"
        guard = ur.guard_id(SEEDED, 3, "seeded-stall/Deployment/inventory-api")
        fresh = copy.deepcopy(next(o for o in ops_for("seeded-a") if "idle-batch-pool" in o["targetLink"]))
        fresh.update(name="operation-later", startTime="2026-10-15T16:00:00Z", endTime="2026-10-15T16:05:00Z")
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "workloads": workloads, "pods": pods}}):
            first, _ = self.collect()
            self.assertIn(guard, {g["id"] for g in first["guards"]})
            self.assertEqual([g["reads"] for g in first["guards"] if g["id"] == guard], [list(ur.FAILURE_GUARD_READS + ur.RECHECK_SPREAD_READS)])
            # A later upgrade re-reviews the cluster: the old operation is outside the window, so no
            # entry-3 row can be built, but the Deployment is still unavailable and the guard stays.
            second, _ = self.collect(FakeFleet(operations=OPERATIONS + [fresh]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
            [review] = [r for r in second["reviews"] if r["cluster"] == SEEDED]
            self.assertTrue(review["reviewed"])
            self.assertEqual([s for s in review["what_failed"] if s["category"] == ur.CATEGORY_UNAVAILABLE], [])
            self.assertIn("seeded-stall/Deployment/inventory-api", review["unavailable_workloads"])
            self.assertIn(guard, {g["id"] for g in second["guards"]})
            # A review whose workloads read failed cannot see the outage either way: the guard is
            # kept, and the stale Warning names the read that failed, not "not reviewed".
            third, _ = self.collect(FakeFleet(operations=OPERATIONS + [fresh], kubectl_fail=[("seeded-a", "workloads")]), now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc), cluster=[SEEDED])
            self.assertIn(guard, {g["id"] for g in third["guards"]})
            [stale] = [w for w in third["sections"]["warnings"] if w["kind"] == ur.INCIDENT_STALE_GUARD and w["object"] == "seeded-stall/Deployment/inventory-api"]
            self.assertEqual(stale["partial"], ["workloads"])
            self.assertIn(ur.STALE_PARTIAL_TEXT.format(failed="workloads"), ur.render_report(third))
            self.assertNotIn("cluster not reviewed this run", ur.render_report(third))
        # Available again, the operation still in the window: a full review drops it.
        for cond in inventory["status"]["conditions"]:
            if cond["type"] == "Available":
                cond["status"] = "True"
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "workloads": workloads, "pods": pods}}):
            fourth, _ = self.collect(FakeFleet(operations=OPERATIONS + [fresh]), now=datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc), cluster=[SEEDED])
        self.assertNotIn(guard, {g["id"] for g in fourth["guards"]})

    def test_version_skew_under_an_active_exclusion_is_info(self):
        gemma = cluster_doc("gemma-gpu-upgraded")
        gemma["nodePools"][0]["version"] = "1.30.5-gke.1000000"
        result, _ = self.collect(FakeFleet(clusters=[cluster_doc("seeded-a"), gemma]))
        review = next(r for r in result["reviews"] if r["cluster"] == GEMMA)
        skew_text = "pool default-pool at 1.30.5-gke.1000000, control plane at 1.32.13-gke.2427000 (2 minors apart)"
        note = ur.SKEW_OPEN_TEXT.format(skews=skew_text, state="exclusion hold-gpu-minor (NO_MINOR_UPGRADES) is active until 2026-10-21T00:00:00Z")
        self.assertEqual(review["skew_notes"], [note])
        self.assertNotIn(ur.CATEGORY_VERSION_SKEW, {s["category"] for s in review["what_failed"]})
        self.assertNotIn(5, {g["entry"] for g in result["guards"]})
        block = ur.render_report(result).split(ur.SECTION_INFO)[1].split(f"### {GEMMA}")[1].split("### ")[0]
        self.assertIn(note, block)
        # The skew is in the stored set, held or not, so a later run can grade its persistence.
        skew_key = "NodePool/default-pool|version-skew|version skew"
        self.assertIn(skew_key, ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][GEMMA]["symptoms"])
        # The exclusion over: the next run files entry 5 and writes its guard, the daily window kept.
        later, _ = self.collect(FakeFleet(clusters=[cluster_doc("seeded-a"), gemma]), now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc), cluster=[GEMMA])
        [review] = later["reviews"]
        self.assertEqual(review["skew_notes"], [])
        [row] = [s for s in review["what_failed"] if s["category"] == ur.CATEGORY_VERSION_SKEW]
        self.assertEqual(entries(row), {(5, ur.MEDIUM)})
        self.assertIn(f"### 5 — {GEMMA} — `NodePool/default-pool` (system)", ur.render_report(later))
        self.assertIn(ur.guard_id(GEMMA, 5, "NodePool/default-pool"), {g["id"] for g in later["guards"]})

    def test_version_skew_under_a_daily_window_is_graded_once_it_persists(self):
        # gemma two minors behind on default-pool, a daily window and no exclusion: the first full run
        # notes the skew with its age and records it; a run inside the open window six days on holds
        # it; the run after seven days re-selects the cluster on the record and files entry 5, under
        # the daily window as under any other; once the pool catches up the guard clears and the key
        # and its date leave the ledger.
        gemma = cluster_doc("gemma-gpu-upgraded")
        gemma["nodePools"][0]["version"] = "1.30.5-gke.1000000"
        gemma["maintenancePolicy"]["window"].pop("maintenanceExclusions")
        skew_key = "NodePool/default-pool|version-skew|version skew"
        skew_text = "pool default-pool at 1.30.5-gke.1000000, control plane at 1.32.13-gke.2427000 (2 minors apart)"
        first, _ = self.collect(FakeFleet(clusters=[cluster_doc("seeded-a"), gemma]))
        review = next(r for r in first["reviews"] if r["cluster"] == GEMMA)
        self.assertEqual(review["skew_notes"], [ur.SKEW_YOUNG_TEXT.format(skews=skew_text, age=ur.SKEW_AGE_FIRST_FORMAT, limit=ur.SKEW_STANDING_DAYS)])
        self.assertNotIn(5, {g["entry"] for g in first["guards"]})
        entry = ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][GEMMA]
        self.assertEqual((skew_key in entry["symptoms"], entry[ur.LEDGER_SKEW_FIRST_SEEN_KEY]), (True, {skew_key: "2026-10-08T18:00:00Z"}))
        # Six days on, inside the open 03:00-07:00 window: re-selected on the record, held as Info.
        held, _ = self.collect(FakeFleet(clusters=[cluster_doc("seeded-a"), gemma]), now=datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc))
        [review] = [r for r in held["reviews"] if r["cluster"] == GEMMA]
        self.assertEqual((review["what_happened"]["status"], review["what_happened"]["reasons"]), (ur.STATUS_REGRADED, [ur.SKEW_RESELECT_REASON]))
        self.assertEqual(review["skew_notes"], [ur.SKEW_OPEN_TEXT.format(skews=skew_text, state=ur.WINDOW_OPEN_STATE)])
        self.assertEqual([s for s in review["what_failed"] if s["category"] == ur.CATEGORY_VERSION_SKEW], [])
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][GEMMA][ur.LEDGER_SKEW_FIRST_SEEN_KEY], {skew_key: "2026-10-08T18:00:00Z"})
        # Eight days on, the window closed: graded, with the age from the first record.
        second, _ = self.collect(FakeFleet(clusters=[cluster_doc("seeded-a"), gemma]), now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        [review] = [r for r in second["reviews"] if r["cluster"] == GEMMA]
        self.assertEqual((review["what_happened"]["status"], review["what_happened"]["reasons"]), (ur.STATUS_REGRADED, [ur.SKEW_RESELECT_REASON]))
        [row] = [s for s in review["what_failed"] if s["category"] == ur.CATEGORY_VERSION_SKEW]
        self.assertEqual((entries(row), row["classifications"][0]["detail"]), ({(5, ur.MEDIUM)}, ur.SKEW_AGE_FORMAT.format(days=8, since="2026-10-08T18:00:00Z")))
        self.assertEqual(review["skew_notes"], [])
        self.assertIn(f"### 5 — {GEMMA} — `NodePool/default-pool` (system)", ur.render_report(second).split(ur.SECTION_WARNINGS)[1].split(ur.SECTION_INFO)[0])
        guard = ur.guard_id(GEMMA, 5, "NodePool/default-pool")
        self.assertIn(guard, {g["id"] for g in second["guards"]})
        self.assertNotIn(ur.PREDATES_UPGRADE_TEXT, ur.render_report(second))
        # A re-check inside the open window keeps the guard too: an older ledger without the stored
        # key leaves the cluster unchanged, and the re-check sees the skew still standing.
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        ledger["clusters"][GEMMA]["symptoms"] = [k for k in ledger["clusters"][GEMMA]["symptoms"] if k != skew_key]
        ur.write_json_atomically(self.home / ur.LEDGER_FILENAME, ledger)
        rechecked, _ = self.collect(FakeFleet(clusters=[cluster_doc("seeded-a"), gemma]), now=datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in rechecked["rechecks"] if r["cluster"] == GEMMA]
        self.assertEqual(([r["cluster"] for r in rechecked["reviews"]], recheck["cleared"]), ([], []))
        self.assertIn(guard, recheck["refreshed"])
        self.assertIn(skew_key, ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][GEMMA]["symptoms"])
        # The pool caught up: reviewed once more on the record, nothing filed, the guard cleared and
        # the key's date gone with it.
        gemma["nodePools"][0]["version"] = gemma["currentMasterVersion"]
        third, _ = self.collect(FakeFleet(clusters=[cluster_doc("seeded-a"), gemma]), now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc))
        [review] = [r for r in third["reviews"] if r["cluster"] == GEMMA]
        self.assertEqual([s for s in review["what_failed"] if s["category"] == ur.CATEGORY_VERSION_SKEW], [])
        self.assertNotIn(guard, {g["id"] for g in third["guards"]})
        entry = ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][GEMMA]
        self.assertEqual((skew_key in entry["symptoms"], entry[ur.LEDGER_SKEW_FIRST_SEEN_KEY]), (False, {}))
        fourth, _ = self.collect(FakeFleet(clusters=[cluster_doc("seeded-a"), gemma]), now=datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc))
        self.assertEqual([r["cluster"] for r in fourth["reviews"]], [])

    def test_crash_record_blocks_until_reset(self):
        self.collect()
        (self.home / ur.LEDGER_FILENAME).write_text("{broken")
        manifest = str(self.home / "manifest.json")
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--no-report"]), ur.EXIT_USAGE)
            # The ledger is gone and a crash record sits beside its place: every run refuses.
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--no-report"]), ur.EXIT_USAGE)
            self.assertIn(f"sits beside no live {ur.LEDGER_FILENAME}", err.getvalue())
            self.assertIn(f"Restore it as {ur.LEDGER_FILENAME}", err.getvalue())
            self.assertFalse((self.home / ur.LEDGER_FILENAME).exists())
            # --dry-run lists what a reset would archive, the record and the live guards file, and
            # moves none; the block stands.
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ur.main(["--reset-ledger", "--dry-run"]), 0)
            [record] = list(self.home.glob(ur.CRASH_RECORD_GLOB))
            self.assertIn(f"would archive 2 file(s) under {self.home / ur.ARCHIVE_SUBDIR}", out.getvalue())
            self.assertIn(record.name, out.getvalue())
            self.assertIn(ur.GUARDS_FILENAME, out.getvalue())
            self.assertFalse((self.home / ur.ARCHIVE_SUBDIR).exists())
            self.assertFalse((self.home / ur.RESET_MARKER_FILENAME).exists())
            # The reset, the second remedy after the restore: the record and the live guards file go
            # to the archive and the marker is left, so the next full run is a baseline the SOP files
            # nothing from.
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ur.main(["--reset-ledger"]), 0)
            self.assertIn("archived 2 file(s)", out.getvalue())
            self.assertEqual((list(self.home.glob(ur.CRASH_RECORD_GLOB)), (self.home / ur.GUARDS_FILENAME).exists()), ([], False))
            self.assertTrue(list((self.home / ur.ARCHIVE_SUBDIR).rglob("ledger.json.unreadable-*")))
            self.assertTrue(list((self.home / ur.ARCHIVE_SUBDIR).rglob(ur.GUARDS_FILENAME)))
            marker = json.loads((self.home / ur.RESET_MARKER_FILENAME).read_text())
            self.assertEqual((marker["version"], marker["reset_at"], sorted(marker["archived"])), (ur.RESET_MARKER_VERSION, "2026-10-08T18:00:00Z", sorted([record.name, ur.GUARDS_FILENAME])))
            # The archive root and its stamp are the store's: made at the store's mode by chmod, not
            # left under the caller's umask, so a root hand reset does not refuse the next uid's.
            [stamped] = [d for d in (self.home / ur.ARCHIVE_SUBDIR).iterdir() if d.is_dir()]
            self.assertEqual((stat.S_IMODE((self.home / ur.ARCHIVE_SUBDIR).stat().st_mode), stat.S_IMODE(stamped.stat().st_mode)), (ur.STORE_DIR_MODE, ur.STORE_DIR_MODE))
            # The baseline: every cluster new, the manifest and the report saying so, the marker gone
            # once the ledger is written; the run after it files as usual.
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--no-report", "--manifest-file", manifest]), 0)
            self.assertTrue((self.home / ur.LEDGER_FILENAME).exists() and (self.home / ur.GUARDS_FILENAME).exists())
            self.assertEqual(json.loads(Path(manifest).read_text())[ur.AFTER_RESET_KEY], True)
            self.assertIn("Baseline after --reset-ledger: the store was archived under", out.getvalue())
            self.assertIn(stamped.name, out.getvalue())
            self.assertFalse((self.home / ur.RESET_MARKER_FILENAME).exists())
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--no-report", "--manifest-file", manifest]), 0)
            self.assertEqual(json.loads(Path(manifest).read_text())[ur.AFTER_RESET_KEY], False)
            self.assertNotIn("Baseline after --reset-ledger", out.getvalue())
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ur.main(["--reset-ledger"]), 0)
            self.assertIn(ur.RESET_LEDGER_NOTHING_TEXT, out.getvalue())

    def test_reset_ledger_takes_the_lock_and_yields_to_a_running_tick(self):
        # The reset moves the state files and writes the marker, so it holds the lock as a run does:
        # while a run holds it, the reset waits the (patched) budget, prints the lock-held line, exits
        # 1 and moves nothing; the dry run only reads and needs no lock; once the lock is free the reset
        # proceeds and holds the lock while it works.
        self.collect()
        (self.home / ur.LEDGER_FILENAME).write_text("{broken")
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--no-report"]), ur.EXIT_USAGE)
            [record] = list(self.home.glob(ur.CRASH_RECORD_GLOB))
            lock_path = self.home / ur.LOCK_FILENAME
            held = ur.acquire_lock(lock_path)
            try:
                with mock.patch.object(ur, "LOCK_WAIT", timedelta(seconds=0.05)), mock.patch.object(ur, "LOCK_POLL_INTERVAL", timedelta(seconds=0.01)):
                    with redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(ur.main(["--reset-ledger"]), ur.EXIT_LOCK_HELD)
                    self.assertTrue(out.getvalue().startswith(ur.lock_held_line(lock_path, timedelta(seconds=0.05))), out.getvalue())
                    self.assertEqual((record.exists(), (self.home / ur.RESET_MARKER_FILENAME).exists(), (self.home / ur.ARCHIVE_SUBDIR).exists()), (True, False, False))
                    with redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(ur.main(["--reset-ledger", "--dry-run"]), 0)
                    self.assertIn("would archive 2 file(s)", out.getvalue())
            finally:
                held.close()
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ur.main(["--reset-ledger"]), 0)
            self.assertIn("archived 2 file(s)", out.getvalue())
            self.assertEqual((record.exists(), (self.home / ur.RESET_MARKER_FILENAME).exists()), (False, True))
            # The lock was released on the way out.
            again = ur.acquire_lock(lock_path, wait=timedelta(0))
            self.assertIsNotNone(again)
            again.close()

    def test_an_absent_guards_file_beside_a_live_ledger_is_a_crash_record(self):
        # Every run writes the guards and then the ledger, so a live ledger beside no guards file is a
        # hand delete; the guards are the memory the hold rule reads, and a run that started from none
        # would announce every held finding resolved. It is refused like a record, naming the file and
        # the restore; --reset-ledger archives the live ledger and the next full run is the baseline.
        self.collect()
        self.assertTrue((self.home / ur.LEDGER_FILENAME).exists() and (self.home / ur.GUARDS_FILENAME).exists())
        (self.home / ur.GUARDS_FILENAME).unlink()
        with self.assertRaises(ur.StateUnreadable) as caught:
            self.collect()
        self.assertEqual(str(caught.exception), ur.GUARDS_ABSENT_TEXT.format(guards=self.home / ur.GUARDS_FILENAME, ledger=self.home / ur.LEDGER_FILENAME, reports=ur.REPORTS_SUBDIR))
        self.assertTrue((self.home / ur.LEDGER_FILENAME).exists())
        manifest = str(self.home / "manifest.json")
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--no-report"]), ur.EXIT_USAGE)
            self.assertEqual(out.getvalue(), "")
            self.assertTrue(err.getvalue().splitlines()[-1].startswith(f"{self.home / ur.GUARDS_FILENAME} is absent beside a live"), err.getvalue())
            self.assertTrue(err.getvalue().splitlines()[-1].endswith(ur.EXIT_PROJECTS_FORMAT.format(projects=PROJECT, note="")))
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ur.main(["--reset-ledger", "--dry-run"]), 0)
            self.assertIn(f"would archive 1 file(s) under {self.home / ur.ARCHIVE_SUBDIR}", out.getvalue())
            self.assertIn(f": {ur.LEDGER_FILENAME}; nothing moved", out.getvalue())
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ur.main(["--reset-ledger"]), 0)
            self.assertIn("archived 1 file(s)", out.getvalue())
            self.assertEqual(((self.home / ur.LEDGER_FILENAME).exists(), len(list((self.home / ur.ARCHIVE_SUBDIR).rglob(ur.LEDGER_FILENAME)))), (False, 1))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--no-report", "--manifest-file", manifest]), 0)
            self.assertEqual(json.loads(Path(manifest).read_text())[ur.AFTER_RESET_KEY], True)
            self.assertTrue((self.home / ur.LEDGER_FILENAME).exists() and (self.home / ur.GUARDS_FILENAME).exists())
            self.assertFalse((self.home / ur.RESET_MARKER_FILENAME).exists())

    def test_reports_are_pruned_to_the_newest_fourteen_of_each_kind(self):
        reports = self.home / ur.REPORTS_SUBDIR
        reports.mkdir()
        for i in range(20):
            for suffix in ("", ur.SCOPED_SUFFIX):
                (reports / f"202609{i + 1:02}T000000Z{suffix}.md").write_text("old")
                (reports / f"202609{i + 1:02}T000000Z{suffix}.json").write_text("{}")
        (reports / "notes.md").write_text("kept: not a report name")
        self.collect()
        full = sorted(p.name for p in reports.glob("*Z.md"))
        scoped = sorted(p.name for p in reports.glob("*-scoped.md"))
        self.assertEqual(len(full), ur.REPORTS_KEPT)
        self.assertEqual(full[-1], "20261008T180000Z.md")
        self.assertEqual(full[0], "20260908T000000Z.md")
        self.assertEqual(len(scoped), ur.REPORTS_KEPT)
        self.assertEqual(len(list(reports.glob("*.json"))), 2 * ur.REPORTS_KEPT)
        self.assertTrue((reports / "notes.md").exists())

    def test_report_may_not_be_named_after_the_link(self):
        fleet = FakeFleet()
        with mock.patch.object(ur, "default_run", fleet), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                rc = ur.main(["--full", "--scope-projects", PROJECT, "--report", str(self.home / ur.REPORTS_SUBDIR / ur.LATEST_REPORT_LINK)])
        self.assertEqual(rc, ur.EXIT_USAGE)
        self.assertIn("must not be named", err.getvalue())
        self.assertEqual(fleet.calls, [])  # refused before any read
        self.assertFalse((self.home / ur.REPORTS_SUBDIR).exists())
        self.assertFalse((self.home / ur.LEDGER_FILENAME).exists())

    def test_non_utf8_state_file_is_set_aside_like_any_other(self):
        (self.home / ur.LEDGER_FILENAME).write_bytes(b'{"version": 1, "clusters": {}, "x": "\xff\xfe"}')
        with self.assertRaises(ur.StateUnreadable) as caught:
            self.collect()
        self.assertIn("is unreadable", str(caught.exception))
        self.assertTrue(list(self.home.glob(ur.CRASH_RECORD_GLOB)))

    def test_set_aside_says_when_the_move_failed(self):
        (self.home / ur.LEDGER_FILENAME).write_text("{broken")
        with mock.patch.object(ur.os, "replace", side_effect=PermissionError("read-only directory")):
            with self.assertRaises(ur.StateUnreadable) as caught:
                self.collect()
        self.assertIn("could not be moved aside (read-only directory); it is unchanged in place", str(caught.exception))
        self.assertNotIn("moved to", str(caught.exception))
        self.assertEqual((self.home / ur.LEDGER_FILENAME).read_text(), "{broken")

    def test_checked_list_omits_checks_whose_core_reads_failed(self):
        clean_reads = {**READS["seeded-a"], "events": [], "pdbs": [], "workloads": [], "storage": []}
        with mock.patch.dict(READS, {"seeded-a": clean_reads}):
            result, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods"), ("seeded-a", "nodes")]), cluster=[SEEDED])
        block = ur.render_report(result).split(f"### {SEEDED}")[1]
        self.assertIn(ur.PARTIAL_READ_TEXT.format(failed="pods, nodes"), block)
        risks = next(line for line in block.splitlines() if line.startswith(ur.PART_RISKS))
        checked, _, skipped = risks.partition(ur.NOT_CHECKED_TEXT)
        for entry in ("(12)", "(20)", "(4)", "(18)", "(14;"):
            self.assertNotIn(entry, checked)
            self.assertIn(entry, skipped)
        self.assertIn("(19)", checked)

    def test_only_full_is_full_and_full_rejects_cluster(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            self.collect(full=True, cluster=[SEEDED])
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(ur.main(["--full", "--scope-projects", PROJECT, "--cluster", SEEDED]), ur.EXIT_USAGE)
        self.assertIn(ur.FULL_WITH_CLUSTER_TEXT, err.getvalue())
        # A full run records the fleet; the same --project set without --full is scoped and changes nothing.
        self.collect()
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        ledger["clusters"]["other-project/us-central1-a/elsewhere"] = {"control_plane": "1.0.0", "node_pools": {}, "channel": "", "first_seen": "2026-10-01T00:00:00Z", "last_run": "2026-10-01T00:00:00Z"}
        ledger[ur.LEDGER_PROJECTS_KEY] = [PROJECT, "other-project"]
        ur.write_json_atomically(self.home / ur.LEDGER_FILENAME, ledger)
        scoped, _ = self.collect(full=False, now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertTrue(scoped["scoped"])
        self.assertEqual(scoped["scope_reason"], ur.NOT_FULL_TEXT)
        self.assertEqual(scoped["removed_clusters"], [])
        after = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        self.assertEqual(after[ur.LEDGER_PROJECTS_KEY], [PROJECT, "other-project"])
        self.assertIn("other-project/us-central1-a/elsewhere", after["clusters"])
        self.assertTrue(Path(scoped["report_path"]).name.endswith("-scoped.md"))
        self.assertEqual(os.readlink(self.home / ur.REPORTS_SUBDIR / ur.LATEST_REPORT_LINK), "20261008T180000Z.md")
        # With --full the absent project is pruned and the link moves.
        full, _ = self.collect(now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        self.assertFalse(full["scoped"])
        self.assertEqual(full["removed_clusters"], ["other-project/us-central1-a/elsewhere"])
        self.assertEqual(full["fleet"], [PROJECT])
        self.assertEqual(os.readlink(self.home / ur.REPORTS_SUBDIR / ur.LATEST_REPORT_LINK), "20261016T180000Z.md")

    def test_failed_listing_is_the_projects_gap_not_the_runs(self):
        self.collect(scope_projects=f"{PROJECT},other-project")
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        other = "other-project/us-central1-a/elsewhere"
        ledger["clusters"][other] = {"control_plane": "1.0.0", "node_pools": {}, "channel": "", "first_seen": "2026-10-01T00:00:00Z", "last_run": "2026-10-01T00:00:00Z"}
        ur.write_json_atomically(self.home / ur.LEDGER_FILENAME, ledger)
        guards = ur.load_json(self.home / ur.GUARDS_FILENAME, {})
        guards["guards"].append({**guards["guards"][0], "id": ur.guard_id(other, 7, "apps/Deployment/x"), "cluster": other, "entry": 7, "object": "apps/Deployment/x"})
        ur.write_json_atomically(self.home / ur.GUARDS_FILENAME, guards)
        later = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        result, _ = self.collect(FakeFleet(list_fail=["other-project"]), now=later, scope_projects=f"{PROJECT},other-project")
        self.assertFalse(result["scoped"])
        self.assertEqual(result["fleet"], [PROJECT, "other-project"])
        self.assertEqual(result["removed_clusters"], [])
        self.assertIn(other, ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"])
        self.assertIn(other, {g["cluster"] for g in result["guards"]})
        self.assertEqual([i for i in result["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD and i["cluster"] == other], [])
        report = ur.render_report(result)
        self.assertIn(f"- {other}: its project's listing failed (clusters list rc=1: PERMISSION_DENIED); ledger entry and guards kept unchanged", report.split(ur.INFO_FAILED_READS)[1])
        self.assertEqual(os.readlink(self.home / ur.REPORTS_SUBDIR / ur.LATEST_REPORT_LINK), "20261015T180000Z.md")

    def test_newly_displaced_replicas_make_an_already_recorded_row_new(self):
        # A StatefulSet carries no dated failure condition, so its pods are dated by their own evidence.
        def sts_pod(name, scheduled_at, created_at):
            p = pod(name, namespace="seeded-capacity", scheduled_message="0/4 nodes are available: 4 Insufficient cpu.", owner="StatefulSet")
            p["metadata"]["ownerReferences"][0]["name"] = "cache"
            p["metadata"]["creationTimestamp"] = created_at
            p["status"]["conditions"][0]["lastTransitionTime"] = scheduled_at
            return p
        sts = {"kind": "StatefulSet", "metadata": {"name": "cache", "namespace": "seeded-capacity"}, "spec": {"replicas": 2, "template": {"metadata": {"labels": {"app": "cache"}}, "spec": {"containers": [{"name": "c", "image": "busybox:1.36"}]}}}}
        old = sts_pod("cache-0", "2026-09-20T00:00:00Z", "2026-09-20T00:00:00Z")
        reads = {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [old], "workloads": READS["seeded-a"]["workloads"] + [sts]}
        with mock.patch.dict(READS, {"seeded-a": reads}):
            first, _ = self.collect()
        row = next(s for r in first["reviews"] for s in r["what_failed"] if s["object"] == "seeded-capacity/StatefulSet/cache")
        self.assertEqual((row["onset_source"], row["predates_upgrade"]), (ur.ONSET_FROM_POD, True))
        # Next week the cluster upgrades again; one more replica is displaced inside that window.
        bumped = cluster_doc("seeded-a")
        bumped["currentMasterVersion"] = "1.36.4-gke.1247000"
        new = sts_pod("cache-1", "2026-10-15T09:30:00Z", "2026-10-15T09:30:00Z")
        drain = copy.deepcopy(next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/pinned-inference-pool" in o["targetLink"]))
        drain.update(name="operation-second-drain", startTime="2026-10-15T09:00:00Z", endTime="2026-10-15T09:20:00Z")
        with mock.patch.dict(READS, {"seeded-a": {**reads, "pods": reads["pods"] + [new]}}):
            second, _ = self.collect(FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], operations=OPERATIONS + [drain]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        row = next(s for r in second["reviews"] for s in r["what_failed"] if s["object"] == "seeded-capacity/StatefulSet/cache")
        self.assertEqual((row["new_pods"], row["pre_existing_pods"]), (["cache-1"], ["cache-0"]))
        self.assertEqual((row["since"], row["predates_upgrade"]), (ur.SINCE_NEW, False))
        self.assertIn("seeded-capacity/StatefulSet/cache", [i["object"] for i in second["sections"]["errors"]])
        self.assertIn("; 1 pre-existing since 2026-09-20T00:00:00Z (e.g. cache-0)", row["classifications"][0]["evidence"])

    def test_probe_less_crash_loop_is_dated_by_its_start_not_its_last_flip(self):
        # The fixture's own shape: Ready flipped a second after the latest crash, 440 restarts, started before the window.
        looping = pod("legacy-worker", statuses=[{"name": "c0", "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": "2026-10-08T17:24:33Z"}}, "restartCount": 440}])
        looping["status"]["startTime"] = "2026-09-01T00:00:00Z"
        looping["status"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": "2026-10-08T17:24:34Z"}]
        reads = {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [looping]}
        with mock.patch.dict(READS, {"seeded-a": reads}):
            first, _ = self.collect()
        row = next(s for r in first["reviews"] for s in r["what_failed"] if s["name"] == "legacy-worker")
        self.assertEqual(row["onset"], "2026-09-01T00:00:00Z")
        self.assertTrue(row["predates_upgrade"])
        # A later upgrade with the symptom recorded: still "present before".
        bumped = cluster_doc("seeded-a")
        bumped["currentMasterVersion"] = "1.36.4-gke.1247000"
        master = copy.deepcopy(next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_MASTER" and "/clusters/seeded-a" in o["targetLink"]))
        master.update(name="operation-later", startTime="2026-10-15T09:00:00Z", endTime="2026-10-15T09:10:00Z")
        looping["status"]["conditions"][0]["lastTransitionTime"] = "2026-10-15T17:59:00Z"
        with mock.patch.dict(READS, {"seeded-a": reads}):
            second, _ = self.collect(FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], operations=OPERATIONS + [master]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        row = next(s for r in second["reviews"] for s in r["what_failed"] if s["name"] == "legacy-worker")
        self.assertEqual((row["since"], row["predates_upgrade"]), (ur.SINCE_BEFORE, True))

    def test_owner_onset_dates_a_pod_the_drain_recreated(self):
        # payments-api's pod was created inside the default-pool drain on 10-07 (Saturday's rebuild);
        # its Deployment has been Available=False for a month: the symptom predates the upgrade.
        workloads = copy.deepcopy(READS["seeded-a"]["workloads"])
        payments = next(w for w in workloads if w["metadata"]["name"] == "payments-api")
        payments["status"] = {"conditions": [{"type": "Available", "status": "False", "lastTransitionTime": "2026-09-08T00:00:00Z"}, {"type": "Progressing", "status": "True", "lastTransitionTime": "2026-09-25T17:02:46Z"}]}
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "workloads": workloads}}):
            result, _ = self.collect()
        row = next(s for r in result["reviews"] for s in r["what_failed"] if s["object"] == PAYMENTS)
        self.assertEqual((row["onset_source"], row["onset"], row["recreated_pods"], row["predates_upgrade"]), (ur.ONSET_FROM_POD, "2026-10-07T04:03:40Z", ["payments-api-79b77b8c67-vfrh9"], True))
        self.assertEqual(row["age_proof"], {"what": "Available=False", "since": "2026-09-08T00:00:00Z"})
        self.assertIn("; owner proves age: Available=False since 2026-09-08T00:00:00Z", row["classifications"][0]["evidence"])
        self.assertTrue(next(i for i in result["sections"]["warnings"] if i["object"] == PAYMENTS)["predates_upgrade"])
        # With no False condition the owner proves nothing; its ReplicaSet's age is not proof.
        payments["status"] = {"conditions": [{"type": "Available", "status": "True", "lastTransitionTime": "2026-10-01T00:00:00Z"}]}
        self.assertNotIn(PAYMENTS, ur.owner_age_proofs(workloads))
        self.assertEqual(ur.owner_rollouts(READS["seeded-a"]["owners"])[PAYMENTS], "2026-09-25T17:02:46Z")
        # An Available=False transition inside the window (a probe-less loop's) proves nothing.
        payments["status"] = {"conditions": [{"type": "Available", "status": "False", "lastTransitionTime": "2026-10-08T17:24:34Z"}]}
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "workloads": workloads}}):
            with tempfile.TemporaryDirectory() as fresh_home, mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: fresh_home, ur.STORE_HOME_ENV: fresh_home}), redirect_stderr(io.StringIO()):
                unproven = ur.collect(args(), run=FakeFleet(), now=NOW)
        row = next(s for r in unproven["reviews"] for s in r["what_failed"] if s["object"] == PAYMENTS)
        self.assertEqual(row["age_proof"], {"what": "Available=False", "since": "2026-10-08T17:24:34Z"})
        self.assertEqual((row["predates_upgrade"], row.get("recreated_only")), (False, True))

    def test_available_deployment_with_an_old_replicaset_has_no_owner_evidence(self):
        # Four replicas; one OOM-killed on a node the default-pool drain rebuilt; conditions True; ReplicaSet months
        # old. A pod can run for months on an old ReplicaSet and fail only on the rebuilt node: no proof of age.
        def fleet_reads(created, ready_at):
            dep = {"kind": "Deployment", "metadata": {"name": "api-fleet", "namespace": "seeded-debug"}, "spec": {"replicas": 4, "template": {"metadata": {"labels": {"app": "api-fleet"}}, "spec": {"containers": [{"name": "api", "image": "eclipse-temurin:8u302-jre"}]}}}, "status": {"conditions": [{"type": "Available", "status": "True", "lastTransitionTime": "2026-07-01T00:10:00Z"}, {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable", "lastTransitionTime": "2026-07-01T00:10:00Z"}]}}
            rs = {"kind": "ReplicaSet", "metadata": {"name": "api-fleet-5d8f9c7b6", "namespace": "seeded-debug", "creationTimestamp": "2026-07-01T00:00:00Z", "ownerReferences": [{"kind": "Deployment", "name": "api-fleet"}]}}
            p = pod("api-fleet-5d8f9c7b6-k2m4p", namespace="seeded-debug", images=["eclipse-temurin:8u302-jre"], statuses=[{"name": "c0", "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": ready_at}}, "restartCount": 5}], node="gke-seeded-a-default-pool-62ac8ee0-d595")
            p["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "api-fleet-5d8f9c7b6"}]
            p["metadata"]["creationTimestamp"] = created
            p["status"]["startTime"] = created
            p["status"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": ready_at}]
            return dep, rs, p
        dep, rs, p = fleet_reads("2026-10-07T04:05:00Z", "2026-10-08T17:00:00Z")
        obj = "seeded-debug/Deployment/api-fleet"
        reads = {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [p], "workloads": READS["seeded-a"]["workloads"] + [dep], "owners": READS["seeded-a"]["owners"] + [rs]}
        with mock.patch.dict(READS, {"seeded-a": reads}):
            first, _ = self.collect()
        row = next(s for r in first["reviews"] for s in r["what_failed"] if s["object"] == obj)
        self.assertEqual((row["onset_source"], row["onset"], row["recreated_pods"], row.get("recreated_only"), row["predates_upgrade"], row["age_proof"]), (ur.ONSET_FROM_POD, "2026-10-07T04:05:00Z", ["api-fleet-5d8f9c7b6-k2m4p"], True, False, None))
        self.assertEqual({(c["entry"], c["confidence"]) for c in row["classifications"]}, {(14, ur.MEDIUM)})
        self.assertIn(ur.RECREATED_DETAIL, row["classifications"][0]["detail"])
        self.assertNotIn("owner proves age", row["classifications"][0]["evidence"])
        incident = next(i for i in first["sections"]["warnings"] if i["object"] == obj)
        self.assertTrue(incident["recreated_only"])
        self.assertFalse(incident["predates_upgrade"])
        # A later full run whose stored set lacks it: new, and the high entry-14 signature is an Error; the
        # in-window ReplicaSet is noted as a rollout and decides nothing.
        reads_without = {**READS["seeded-a"], "workloads": READS["seeded-a"]["workloads"] + [dep], "owners": READS["seeded-a"]["owners"] + [rs]}
        with mock.patch.dict(READS, {"seeded-a": reads_without}):
            self.collect(now=datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc))
        bumped = cluster_doc("seeded-a")
        bumped["currentMasterVersion"] = "1.36.4-gke.1247000"
        drain = copy.deepcopy(next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/default-pool" in o["targetLink"]))
        drain.update(name="operation-second-drain", startTime="2026-10-15T09:00:00Z", endTime="2026-10-15T09:20:00Z")
        dep, rs, p = fleet_reads("2026-10-15T09:05:00Z", "2026-10-15T17:00:00Z")
        rs["metadata"]["creationTimestamp"] = "2026-10-15T09:02:00Z"
        later_reads = {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [p], "workloads": READS["seeded-a"]["workloads"] + [dep], "owners": READS["seeded-a"]["owners"] + [rs]}
        with mock.patch.dict(READS, {"seeded-a": later_reads}):
            second, _ = self.collect(FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], operations=OPERATIONS + [drain]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        row = next(s for r in second["reviews"] for s in r["what_failed"] if s["object"] == obj)
        self.assertEqual((row["since"], row["predates_upgrade"], row.get("recreated_only")), (ur.SINCE_NEW, False, None))
        self.assertEqual({(c["entry"], c["confidence"]) for c in row["classifications"]}, {(14, ur.HIGH)})
        self.assertIn("; rollout during the window: current ReplicaSet created 2026-10-15T09:02:00Z", row["classifications"][0]["evidence"])
        self.assertIn(obj, [i["object"] for i in second["sections"]["errors"]])
        # The same Deployment Available=False for a month: owner evidence, predates the upgrade.
        dep, rs, p = fleet_reads("2026-10-07T04:05:00Z", "2026-10-08T17:00:00Z")
        dep["status"]["conditions"][0] = {"type": "Available", "status": "False", "lastTransitionTime": "2026-09-08T00:00:00Z"}
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [p], "workloads": READS["seeded-a"]["workloads"] + [dep], "owners": READS["seeded-a"]["owners"] + [rs]}}):
            with tempfile.TemporaryDirectory() as fresh_home, mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: fresh_home, ur.STORE_HOME_ENV: fresh_home}):
                with redirect_stderr(io.StringIO()):
                    third = ur.collect(args(), run=FakeFleet(), now=NOW)
        row = next(s for r in third["reviews"] for s in r["what_failed"] if s["object"] == obj)
        self.assertEqual((row["onset_source"], row["onset"], row["predates_upgrade"], row.get("recreated_only")), (ur.ONSET_FROM_POD, "2026-10-07T04:05:00Z", True, None))
        self.assertEqual(row["age_proof"], {"what": "Available=False", "since": "2026-09-08T00:00:00Z"})
        self.assertTrue(next(i for i in third["sections"]["warnings"] if i["object"] == obj)["predates_upgrade"])

    def test_recreated_pod_without_owner_date_is_medium_on_a_first_run(self):
        # A StatefulSet pod created inside the default-pool drain, crash-looping on a cgroup v1 runtime: entry 14
        # names the mechanism and the pool was touched, but a recreation carries a crash loop over.
        p = pod("legacy-db-0", namespace="seeded-shapes", owner="StatefulSet", images=["eclipse-temurin:8u302-jre"], statuses=[{"name": "c0", "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-10-08T17:00:00Z"}}, "restartCount": 12}])
        p["metadata"]["ownerReferences"][0]["name"] = "legacy-db"
        p["metadata"]["creationTimestamp"] = "2026-10-07T04:05:00Z"
        p["status"]["startTime"] = "2026-10-07T04:05:00Z"
        p["status"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": "2026-10-08T17:00:01Z"}]
        sts = {"kind": "StatefulSet", "metadata": {"name": "legacy-db", "namespace": "seeded-shapes"}, "spec": {"replicas": 1, "template": {"metadata": {"labels": {"app": "legacy-db"}}, "spec": {"containers": [{"name": "c", "image": "busybox:1.36"}]}}}}
        reads = {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [p], "workloads": READS["seeded-a"]["workloads"] + [sts]}
        with mock.patch.dict(READS, {"seeded-a": reads}):
            first, _ = self.collect()
        obj = "seeded-shapes/StatefulSet/legacy-db"
        row = next(s for r in first["reviews"] for s in r["what_failed"] if s["object"] == obj)
        self.assertEqual((row["onset_source"], row["recreated_pods"], row.get("recreated_only")), (ur.ONSET_FROM_POD, ["legacy-db-0"], True))
        self.assertEqual({(c["entry"], c["confidence"]) for c in row["classifications"]}, {(14, ur.MEDIUM)})
        self.assertIn(ur.RECREATED_DETAIL, row["classifications"][0]["detail"])
        incident = next(i for i in first["sections"]["warnings"] if i["object"] == obj)
        self.assertTrue(incident["recreated_only"])
        self.assertNotIn(obj, [i["object"] for i in first["sections"]["errors"]])
        self.assertIn(ur.RECREATED_TEXT, ur.render_report(first))
        # A later full run grades it by the stored set: recorded, so present before.
        bumped = cluster_doc("seeded-a")
        bumped["currentMasterVersion"] = "1.36.4-gke.1247000"
        drain = copy.deepcopy(next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/default-pool" in o["targetLink"]))
        drain.update(name="operation-second-drain", startTime="2026-10-15T09:00:00Z", endTime="2026-10-15T09:20:00Z")
        p["metadata"]["creationTimestamp"] = "2026-10-15T09:05:00Z"
        p["status"]["startTime"] = "2026-10-15T09:05:00Z"
        p["status"]["conditions"][0]["lastTransitionTime"] = "2026-10-15T17:00:01Z"
        with mock.patch.dict(READS, {"seeded-a": reads}):
            second, _ = self.collect(FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], operations=OPERATIONS + [drain]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        row = next(s for r in second["reviews"] for s in r["what_failed"] if s["object"] == obj)
        self.assertEqual((row["since"], row["predates_upgrade"], row.get("recreated_only")), (ur.SINCE_BEFORE, True, None))
        self.assertEqual({c["confidence"] for c in row["classifications"]}, {ur.HIGH})
        self.assertNotIn(obj, [i["object"] for i in second["sections"]["errors"]])
        # Not recorded by the previous full run: new, and the high signature stands as an Error.
        reads_without = {**READS["seeded-a"], "workloads": READS["seeded-a"]["workloads"] + [sts]}
        with mock.patch.dict(READS, {"seeded-a": reads_without}):
            self.collect(now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        bumped["currentMasterVersion"] = "1.36.5-gke.1000000"
        drain.update(name="operation-third-drain", startTime="2026-10-22T09:00:00Z", endTime="2026-10-22T09:20:00Z")
        p["metadata"]["creationTimestamp"] = "2026-10-22T09:05:00Z"
        p["status"]["startTime"] = "2026-10-22T09:05:00Z"
        p["status"]["conditions"][0]["lastTransitionTime"] = "2026-10-22T17:00:01Z"
        with mock.patch.dict(READS, {"seeded-a": reads}):
            third, _ = self.collect(FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], operations=OPERATIONS + [drain]), now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc))
        row = next(s for r in third["reviews"] for s in r["what_failed"] if s["object"] == obj)
        self.assertEqual((row["since"], row["predates_upgrade"]), (ur.SINCE_NEW, False))
        self.assertIn(obj, [i["object"] for i in third["sections"]["errors"]])

    def test_crash_loop_since_before_the_window_predates_the_upgrade(self):
        looping = pod("legacy-worker", statuses=[{"name": "c0", "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": "2026-10-08T17:55:00Z"}}}])
        looping["status"]["startTime"] = "2026-09-01T00:00:00Z"
        looping["status"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": "2026-09-08T00:00:00Z"}]
        looping["spec"]["containers"][0]["image"] = "eclipse-temurin:8u302-jre"
        looping["status"]["containerStatuses"][0]["lastState"]["terminated"]["reason"] = "OOMKilled"
        looping["status"]["containerStatuses"][0]["lastState"]["terminated"]["exitCode"] = 137
        reads = {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [looping]}
        with mock.patch.dict(READS, {"seeded-a": reads}):
            result, _ = self.collect()
        review = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        row = next(s for s in review["what_failed"] if s["name"] == "legacy-worker")
        # Restarted since, so the earlier of its start (09-01) and the Ready transition (09-08) dates it.
        self.assertEqual((row["onset"], row["since"]), ("2026-09-01T00:00:00Z", ur.SINCE_FIRST_SEEN))
        self.assertTrue(row["predates_upgrade"])
        self.assertEqual(row["classifications"][0]["confidence"], ur.HIGH)  # the signature is the mechanism; the grade is a Warning all the same
        incident = next(i for i in result["sections"]["warnings"] if i["object"] == "apps/Pod/legacy-worker")
        self.assertTrue(incident["predates_upgrade"])
        self.assertNotIn("apps/Pod/legacy-worker", [i["object"] for i in result["sections"]["errors"]])

    def test_full_refuses_a_project_override_and_since(self):
        with self.assertRaises(argparse.ArgumentTypeError) as caught:
            self.collect(full=True, scope_projects=None, project=PROJECT)
        self.assertEqual(str(caught.exception), ur.FULL_WITH_PROJECT_TEXT)
        with self.assertRaises(argparse.ArgumentTypeError) as caught:
            self.collect(full=True, since="30")
        self.assertEqual(str(caught.exception), ur.FULL_WITH_SINCE_TEXT)
        self.assertFalse((self.home / ur.LEDGER_FILENAME).exists())

    def test_reset_ledger_archives_beside_a_named_ledger_path(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        ledger = elsewhere / "my-ledger.json"
        ledger.write_text("{broken")
        with self.assertRaises(ur.StateUnreadable):
            self.collect(ledger=str(ledger))
        self.assertTrue(list(elsewhere.glob("my-ledger.json.unreadable-*")))
        with mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ur.main(["--reset-ledger"]), 0)
            self.assertIn(ur.RESET_LEDGER_NOTHING_TEXT, out.getvalue())
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ur.main(["--reset-ledger", "--ledger", str(ledger)]), 0)
        self.assertIn("archived 1 file(s)", out.getvalue())
        self.assertEqual(list(elsewhere.glob("my-ledger.json.unreadable-*")), [])
        result, _ = self.collect(ledger=str(ledger))
        self.assertEqual((result["scoped"], result[ur.AFTER_RESET_KEY], (self.home / ur.RESET_MARKER_FILENAME).exists()), (False, True, False))

    def test_budget_failure_guard_survives_a_master_only_review(self):
        first, _ = self.collect()
        gid = ur.guard_id(SEEDED, 1, "seeded-capacity/PodDisruptionBudget/inference-server")
        self.assertIn(gid, {g["id"] for g in first["guards"]})
        bumped = cluster_doc("seeded-a")
        bumped["currentMasterVersion"] = "1.36.4-gke.1247000"
        master = copy.deepcopy(next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_MASTER" and "/clusters/seeded-a" in o["targetLink"]))
        master.update(name="operation-master-only", startTime="2026-10-15T09:00:00Z", endTime="2026-10-15T09:10:00Z")
        second, _ = self.collect(FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], operations=OPERATIONS + [master]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        kept = next(g for g in second["guards"] if g["id"] == gid)
        self.assertEqual((kept["first_seen"], kept["last_seen"]), ("2026-10-08T18:00:00Z", "2026-10-15T18:00:00Z"))
        # The budget lets a disruption through: the review drops it like any other.
        pdbs = copy.deepcopy(READS["seeded-a"]["pdbs"])
        pdbs[0]["status"]["disruptionsAllowed"] = 1
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "pdbs": pdbs}}):
            third, _ = self.collect(FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], operations=OPERATIONS + [master]), now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc), cluster=[SEEDED])
        self.assertNotIn(gid, {g["id"] for g in third["guards"]})

    def test_unchanged_cluster_versions_are_refreshed(self):
        self.collect()
        grown = cluster_doc("seeded-a")
        grown["nodePools"].append({"name": "new-pool", "version": "1.35.8-gke.1380001", "status": "RUNNING", "config": {"effectiveCgroupMode": ur.CGROUP_V2_MODE}})
        result, _ = self.collect(FakeFleet(clusters=[grown, cluster_doc("gemma-gpu-upgraded")]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(result["reviews"], [])
        entry = ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED]
        self.assertIn("new-pool", entry["node_pools"])
        self.assertEqual(entry["last_run"], "2026-10-08T18:00:00Z")

    def test_budget_that_holds_two_consecutive_drains_is_an_error_each_time(self):
        first, _ = self.collect()
        self.assertIn("seeded-capacity/PodDisruptionBudget/inference-server", [i["object"] for i in first["sections"]["errors"]])
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        self.assertTrue(any(k.startswith("seeded-capacity/PodDisruptionBudget/inference-server|pdb|disruptionsAllowed=0|op=operation-1791433235916") for k in ledger["clusters"][SEEDED]["symptoms"]))
        # A week later the pool is drained again and the budget holds it again: a new incident of a new operation.
        bumped = cluster_doc("seeded-a")
        bumped["currentMasterVersion"] = "1.36.4-gke.1247000"
        drain = copy.deepcopy(next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/pinned-inference-pool" in o["targetLink"]))
        drain.update(name="operation-second-hold", startTime="2026-10-15T09:00:00Z", endTime="2026-10-15T10:30:00Z")
        second, _ = self.collect(FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], operations=OPERATIONS + [drain]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        row = next(s for r in second["reviews"] for s in r["what_failed"] if s["category"] == "pdb")
        self.assertEqual((row["since"], row["predates_upgrade"], row["operation"]), (ur.SINCE_NEW, False, "operation-second-hold"))
        self.assertIn("seeded-capacity/PodDisruptionBudget/inference-server", [i["object"] for i in second["sections"]["errors"]])

    def test_failed_review_keeps_the_pre_upgrade_versions_for_the_retry(self):
        self.collect()
        bumped = cluster_doc("seeded-a")
        bumped["currentMasterVersion"] = "1.36.4-gke.1247000"
        later = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        failed, _ = self.collect(FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], broken=["seeded-a"]), now=later)
        review = next(r for r in failed["reviews"] if r["cluster"] == SEEDED)
        self.assertEqual((review["reviewed"], review["partial"]), (False, list(ur.CORE_READS)))
        entry = ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED]
        self.assertEqual((entry["control_plane"], entry["last_run"], entry["partial_read"]), ("1.35.8-gke.1380001", "2026-10-08T18:00:00Z", "2026-10-15T18:00:00Z"))
        # The retry, even with the operations listing down, is selected by the version diff and reports before and after.
        retry, _ = self.collect(FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], ops_fail=[PROJECT]), now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        review = next(r for r in retry["reviews"] if r["cluster"] == SEEDED)
        self.assertTrue(review["reviewed"])
        self.assertEqual((review["what_happened"]["versions_before"]["control_plane"], review["what_happened"]["versions_after"]["control_plane"]), ("1.35.8-gke.1380001", "1.36.4-gke.1247000"))
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED]["control_plane"], "1.36.4-gke.1247000")

    def test_unread_operations_leave_the_pool_gate_unknown(self):
        bumped = cluster_doc("seeded-a")
        bumped["currentMasterVersion"] = "1.36.4-gke.1247000"
        self.collect()
        result, _ = self.collect(FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], ops_fail=[PROJECT]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        review = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        details = [c["detail"] for s in review["what_failed"] for c in s["classifications"] if c["entry"] is not None]
        self.assertTrue(details)
        # Entry 2 names its mechanism and would be high: it says the gate is unknown, never closed.
        capacity = [c["detail"] for s in review["what_failed"] for c in s["classifications"] if c["entry"] == 2]
        self.assertTrue(capacity and all(ur.GATE_UNKNOWN_TEXT in d for d in capacity), capacity)
        self.assertFalse(any(ur.GATE_CLOSED_TEXT in d for d in details))
        self.assertEqual({c["confidence"] for s in review["what_failed"] for c in s["classifications"] if c["entry"] is not None}, {ur.MEDIUM})
        self.assertEqual(result["sections"]["errors"], [])

    def test_outside_fleet_incident_says_why_it_has_no_guard(self):
        result, _ = self.collect(project=None, full=False)
        report = ur.render_report(result)
        self.assertIn(ur.OUTSIDE_FLEET_GUARD_TEXT, report)
        self.assertNotIn(ur.NO_GUARD_TEXT, report.split(f"### 2 — {SEEDED}")[1].split("### ")[0])

    def test_store_defaults_live_under_the_store_home(self):
        result, _ = self.collect()
        self.assertEqual(Path(result["ledger_path"]), self.home / ur.LEDGER_FILENAME)
        self.assertEqual(Path(result["guards_path"]), self.home / ur.GUARDS_FILENAME)
        self.assertEqual(Path(result["json_path"]), self.home / ur.REPORTS_SUBDIR / "20261008T180000Z.json")
        self.assertEqual(Path(result["report_path"]), self.home / ur.REPORTS_SUBDIR / "20261008T180000Z.md")
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
        # The manifest's candidates carry the same tenant text in `impact` (a classification's detail:
        # the webhook name) and in `excerpt`, and `draft` copies both into the findings document, so
        # both are escaped as the report's cells are: no pipe, backtick or newline survives.
        manifest_text = Path(result["manifest_path"]).read_text()
        self.assertNotIn("gate|x", manifest_text)
        candidates = [c for e in json.loads(manifest_text)["clusters"] for c in e.get("candidates") or []]
        self.assertTrue(any("gate/x" in c["impact"] for c in candidates), [c["impact"][:120] for c in candidates])
        for c in candidates:
            for field in ("impact", "excerpt"):
                self.assertFalse(any(ch in c[field] for ch in "|`\n"), (field, c[field]))

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
        # A --cluster run is scoped: seeded-a's live guards are outside it and not reported as stale.
        self.assertTrue(result["scoped"])
        self.assertEqual([i for i in result["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD], [])
        self.assertEqual(result["scope_reason"], f"{ur.NOT_FULL_TEXT}; --cluster named {GEMMA}")
        self.assertIn(ur.INFO_SCOPED.format(reason=result["scope_reason"]), ur.render_report(result))
        self.assertTrue(Path(result["report_path"]).name.endswith("-scoped.md"))
        self.assertEqual(os.readlink(self.home / ur.REPORTS_SUBDIR / ur.LATEST_REPORT_LINK), "20261008T180000Z.md")
        missing, _ = self.collect(cluster=[f"{PROJECT}/{LOCATION}/nope"])
        self.assertEqual(missing["reviews"], [])
        self.assertIn("named by --cluster but not listed", missing["failed_reads"][0])

    def test_discovery_runs_without_project_flag(self):
        # An install that declares no scope and a hand run without flags: the listing, as before.
        result, fleet = self.collect(project=None, scope_projects=None)
        self.assertEqual(result["projects"], [PROJECT])
        self.assertTrue(any("projects list" in " ".join(c) for c in fleet.calls))
        # No environment variable takes part, as collect.py's discover_fleet.
        with mock.patch.dict(os.environ, {"MONITORED_PROJECT_IDS": "a-project,b-project", "GKE_PROJECT_ID": "c-project"}):
            result, fleet = self.collect(project=None, scope_projects=None)
        self.assertEqual(result["projects"], [PROJECT])
        self.assertEqual([c for c in fleet.calls if "config" in c], [["gcloud", "config", "get-value", "project"]])


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: self.tmp.name, ur.STORE_HOME_ENV: self.tmp.name})
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
        # Two of inference-server's three Pending pods were displaced inside the pinned-inference-pool
        # drain; a Pending replica created in the window is new. The third has waited since 09-25.
        self.assertEqual([line for line in errors.splitlines() if line.startswith("### ")], [
            f"### 2 — {SEEDED} — `{INFERENCE}`",
            f"### 1 — {SEEDED} — `seeded-capacity/PodDisruptionBudget/inference-server`",
        ])
        inference = errors.split("### 2 —")[1].split("### 1 —")[0]
        self.assertNotIn(ur.PREDATES_UPGRADE_TEXT, inference)
        self.assertIn("; e.g. inference-server-778b78fdb8-ld26r; 1 pre-existing since 2026-09-25T17:02:48Z (e.g. inference-server-778b78fdb8-cp2pf)", inference)
        for part in (ur.PART_WHAT_HAPPENED, ur.PART_WHAT_FAILED, ur.PART_MITIGATE, ur.PART_MITIGATION_SET_UP):
            self.assertIn(part, inference)
        self.assertIn("| UPGRADE_NODES | pinned-inference-pool | 2026-10-08T04:20:35Z | 2026-10-08T05:24:10Z | 63 min | DONE |  |", inference)
        self.assertIn("| control plane | - | 1.35.8-gke.1380001 |", inference)
        self.assertIn("| Unschedulable | first seen | 2. No spare capacity for the displaced pods | high | 3 of 4 pods: Insufficient cpu; e.g. inference-server-778b78fdb8-ld26r; 1 pre-existing since 2026-09-25T17:02:48Z (e.g. inference-server-778b78fdb8-cp2pf) |", inference)
        # One entry per symptom: the "didn't match" clauses for the pools the pod does not target are
        # not a second finding, so entry 12 appears nowhere on this incident.
        self.assertNotIn("12. A node label is removed", inference)
        self.assertIn(f"- guard `{ur.guard_id(SEEDED, 2, INFERENCE)}` failure entry 2 (high), first seen 2026-10-08T18:00:00Z", inference)
        self.assertIn("Read today: the readiness mode of `fleet-upgrade-verification` grades it `blocked`, and the obtainability audit reports it as `blocking-pdb`.", errors)
        # Warnings: medium, system, unclassified.
        headings = [line for line in warnings.splitlines() if line.startswith("### ")]
        self.assertEqual(headings, [
            f"### 2 — {GEMMA} — `{KUBE_DNS}` (system)",
            f"### 6 — {GEMMA} — `{TUNER}`",
            f"### 14 — {SEEDED} — `{PAYMENTS}`",
            f"### unclassified — {SEEDED} — `seeded-stall/Deployment/inventory-api`",
        ])
        self.assertIn(f"{ur.PART_WHAT_HAPPENED} new (first seen); channel EXTENDED; cluster status RUNNING.", warnings)
        self.assertIn(ur.NO_OPERATION_LINE, warnings.split("### 2")[1].split("### 6")[0])
        self.assertIn("| Error | first seen | 6. A served API version is removed (best effort: a name, not an API call) | medium | 2 of 2 pods: CronJob pod in Error; name mentions flowcontrol", warnings)
        self.assertIn(f"- guard `{ur.guard_id(SEEDED, 14, PAYMENTS)}` failure entry 14 (medium), first seen 2026-10-08T18:00:00Z", warnings)
        # Info: one block per reviewed cluster, with the next upgrade, the risks and the baseline.
        self.assertIn(f"### {SEEDED} — 4 incident(s) above", info)
        self.assertIn(f"### {GEMMA} — 2 incident(s) above", info)
        seeded_block = info.split(f"### {SEEDED}")[1]
        self.assertIn(f"{ur.PART_NEXT_UPGRADE} channel REGULAR; target 1.35.8-gke.1225000; cluster at 1.35.8-gke.1380001, at or ahead of the target. Window: daily at 03:00 UTC for 4h; next opens 2026-10-09T03:00:00Z. Exclusions: none.", seeded_block)
        self.assertIn("| `seeded-shapes/Deployment/legacy-registry-pull` | 20. Images on a retired registry | high | image k8s.gcr.io/pause:3.9 |", seeded_block)
        self.assertIn(f"{ur.PART_BASELINE} control plane 1.35.8-gke.1380001; pools default-pool 1.35.8-gke.1380001, idle-batch-pool 1.35.8-gke.1380001, pinned-inference-pool 1.35.8-gke.1380001; 23 pods, 1 budgets, 9 shapes. Symptom baseline recorded: 4 symptom(s), 4 first seen. first run: graded by onset only, no previous symptom set. Guards written: 13.", seeded_block)
        gemma_block = info.split(f"### {GEMMA}")[1].split("### ")[0]
        self.assertIn("behind the target. Window: DAILY at", gemma_block)
        self.assertIn("Exclusions: hold-gpu-minor (NO_MINOR_UPGRADES) until 2026-10-21T00:00:00Z [active].", gemma_block)
        self.assertNotIn(ur.NONE_LINE, info)
        # `_none_` is reserved for an empty severity section: an unclassified
        # incident says what it lacks instead.
        self.assertNotIn(ur.NONE_LINE, report)
        self.assertIn(ur.UNCLASSIFIED_MITIGATION_TEXT, warnings)
        self.assertIn(f"- guard `{SEEDED}#failure#unclassified#seeded-stall/Deployment/inventory-api` failure entry unclassified (medium), first seen 2026-10-08T18:00:00Z", warnings)
        self.assertNotIn(ur.NO_GUARD_TEXT, warnings)

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
        self.assertIn(f"{ur.SECTION_ERRORS}\n\n{ur.NONE_LINE}\n", report)
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
        # The incident is its own kind, keyed by the operation's name; type and target are in the text.
        self.assertEqual(op_incident["object"], f"Operation/{bad['name']}")
        report = ur.render_report(result)
        self.assertIn(f"### operation DONE — {SEEDED} — `Operation/{bad['name']}`", report)
        self.assertIn(ur.OPERATION_GUARD_FORMAT.format(id=ur.guard_id(SEEDED, ur.ENTRY_OPERATION, f"Operation/{bad['name']}"), first_seen="2026-10-08T18:00:00Z", reviews=1, type="UPGRADE_NODES", target="idle-batch-pool"), report)
        self.assertIn(f"{ur.PART_WHAT_FAILED} UPGRADE_NODES on idle-batch-pool ended DONE: Instance", report)
        self.assertIn(f"{ur.PART_MITIGATE} {ur.OPERATION_MITIGATE_TEXT}", report)
        # The manifest candidate's impact is the report's sentence; entry 2's repair joins it only
        # when GKE's error is capacity-shaped (the fixture's stockout is), never for a webhook refusal.
        candidate = ur._incident_candidate(op_incident, None)
        self.assertTrue(candidate["impact"].startswith(ur.OPERATION_MITIGATE_TEXT))
        self.assertIn(ur.MITIGATIONS[2]["mitigate_after"], candidate["impact"])
        refused = copy.deepcopy(op_incident)
        refused["operation"] = {**op_incident["operation"], "error": 'admission webhook "gate.example.io" denied the request'}
        self.assertEqual(ur._incident_candidate(refused, None)["impact"], ur.OPERATION_MITIGATE_TEXT)

    def test_main_writes_report_and_latest_link(self):
        home = Path(self.tmp.name)
        report_path = home / "reports" / "20261008T180000Z.md"
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = ur.main(["--full", "--scope-projects", PROJECT, "--output", str(home / "out.json"), "--report", str(report_path)])
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
                    ur.main(["--full", "--scope-projects", PROJECT, "--output", str(home / "out.json")])
        self.assertEqual(order, ["out.json", ur.GUARDS_FILENAME, ur.LEDGER_FILENAME])
        self.assertTrue((home / ur.REPORTS_SUBDIR / ur.LATEST_REPORT_LINK).exists())
        self.assertEqual([p.name for p in (home / ur.REPORTS_SUBDIR).glob("*Z.md")], ["20261008T180000Z.md"])
        self.assertTrue((home / ur.GUARDS_FILENAME).exists())
        self.assertFalse((home / ur.LEDGER_FILENAME).exists())

    def test_main_dry_run_prints_but_writes_nothing(self):
        home = Path(self.tmp.name)
        with mock.patch.object(ur, "default_run", FakeFleet()), mock.patch.object(ur, "now_utc", lambda: NOW):
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                rc = ur.main(["--project", PROJECT, "--dry-run", "--report", str(home / "r.md")])
        self.assertEqual(rc, 0)
        self.assertIn(f"### 2 — {SEEDED} — `{INFERENCE}`", out.getvalue())
        self.assertFalse((home / "r.md").exists())
        self.assertFalse((home / ur.LEDGER_FILENAME).exists())

    def test_bad_since_is_a_usage_error(self):
        with mock.patch.object(ur, "default_run", FakeFleet()):
            with redirect_stderr(io.StringIO()) as err:
                rc = ur.main(["--project", PROJECT, "--since", "yesterday", "--no-report"])
        self.assertEqual(rc, 2)
        self.assertIn("--since takes", err.getvalue())


class ChecksRunTest(unittest.TestCase):
    """Every cluster row carries the SOP's `checks_run`, copied from the reads
    that ran rather than retyped by the worker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: self.tmp.name, ur.STORE_HOME_ENV: self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def collect(self, fleet=None, now=NOW, **overrides):
        fleet = fleet or FakeFleet()
        with redirect_stderr(io.StringIO()):
            return ur.collect(args(**overrides), run=fleet, now=now), fleet

    def test_a_reviewed_cluster_carries_every_check_with_the_read_that_ran_it(self):
        result, _ = self.collect()
        seeded = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertEqual([c["check"] for c in seeded["commands"]], [check for check, _ in ur.CHECK_READS])
        by_check = {c["check"]: c["command"] for c in seeded["commands"]}
        operations = by_check[ur.CHECK_OPERATION_FAILED]
        self.assertTrue(operations.startswith(f"gcloud container operations list --project {PROJECT} --filter "), operations)
        self.assertIn("UPGRADE_MASTER OR UPGRADE_NODES", operations)
        self.assertIn("endTime>=2026-09-24T18:00:00Z OR status!=DONE", operations)
        pods = by_check[ur.CHECK_BROKE_WORKLOAD]
        self.assertEqual(pods, f"KUBECONFIG={self.tmp.name}/.kubeconfigs/kubeconfig_{PROJECT}_seeded-a_{LOCATION}.yaml kubectl get pods -A -o json")
        self.assertTrue(by_check[ur.CHECK_NODE_BROKEN].endswith(" kubectl get nodes -o json"))
        for check in (ur.CHECK_SYMPTOM_TENTATIVE, ur.CHECK_SYMPTOM_UNCLASSIFIED, ur.CHECK_SYMPTOM_PREDATES):
            self.assertEqual(by_check[check], pods)
        self.assertEqual([check for check, _ in ur.CHECK_READS], [ur.CHECK_OPERATION_FAILED, ur.CHECK_BROKE_WORKLOAD, ur.CHECK_NODE_BROKEN, ur.CHECK_SYMPTOM_TENTATIVE, ur.CHECK_SYMPTOM_UNCLASSIFIED, ur.CHECK_SYMPTOM_PREDATES])

    def test_a_failed_read_drops_the_checks_it_backs(self):
        result, _ = self.collect(FakeFleet(kubectl_fail={("seeded-a", "pods")}))
        seeded = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertEqual([c["check"] for c in seeded["commands"]], [ur.CHECK_OPERATION_FAILED, ur.CHECK_NODE_BROKEN])
        self.assertIn("pods", seeded["partial"])

    def test_an_unreadable_cluster_carries_the_listing_backed_check_alone(self):
        # Its credentials failed, but the project's operations listing answered: the operation check
        # ran for it; with the listing failed too, nothing did.
        result, _ = self.collect(FakeFleet(broken={"seeded-a"}))
        seeded = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertEqual([c["check"] for c in seeded["commands"]], [ur.CHECK_OPERATION_FAILED])
        unread, _ = self.collect(FakeFleet(broken={"seeded-a"}, ops_fail={PROJECT}), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        seeded = next(r for r in unread["reviews"] if r["cluster"] == SEEDED)
        self.assertEqual(seeded["commands"], [])

    def test_an_unchanged_cluster_is_backed_by_the_listing_and_its_re_check(self):
        self.collect()
        second, _ = self.collect()
        rows = {u["cluster"]: u for u in second["unchanged"]}
        self.assertEqual(set(rows), {GEMMA, SEEDED})
        for row in rows.values():
            by_check = {c["check"]: c["command"] for c in row["commands"]}
            self.assertEqual(list(by_check), [check for check, _ in ur.CHECK_READS])
            self.assertTrue(by_check[ur.CHECK_OPERATION_FAILED].startswith("gcloud container operations list"))
            # A full run refreshes every fleet cluster's pods, so the checks that read pods name that read.
            self.assertIn(" kubectl get pods -A -o json", by_check[ur.CHECK_SYMPTOM_TENTATIVE])
            self.assertIn(" kubectl get nodes -o json", by_check[ur.CHECK_NODE_BROKEN])

    def test_an_unchanged_cluster_whose_recheck_read_failed_drops_the_checks_it_backs(self):
        self.collect()
        second, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods")]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        row = next(u for u in second["unchanged"] if u["cluster"] == SEEDED)
        # The listing backs a check whose read did not run, never one whose read ran and failed.
        self.assertEqual([c["check"] for c in row["commands"]], [ur.CHECK_OPERATION_FAILED, ur.CHECK_NODE_BROKEN])
        self.assertEqual([c["check"] for c in row["checks_unevaluated"]], [ur.CHECK_BROKE_WORKLOAD, ur.CHECK_SYMPTOM_TENTATIVE, ur.CHECK_SYMPTOM_UNCLASSIFIED, ur.CHECK_SYMPTOM_PREDATES])
        self.assertTrue(all(c["reason"] == "pods read failed" for c in row["checks_unevaluated"]))
        self.assertTrue(row["limitations"].startswith("reads that failed: pods:"))
        other = next(u for u in second["unchanged"] if u["cluster"] == GEMMA)
        self.assertEqual((other["checks_unevaluated"], "limitations" in other), ([], False))

    def test_a_scoped_unchanged_cluster_without_guards_is_backed_by_the_listing_alone(self):
        # A scoped run re-reads an unchanged cluster only for the guards it holds; with
        # none, the listing that found no upgrade is the one command behind every check.
        self.collect()
        ur.write_json_atomically(self.home / ur.GUARDS_FILENAME, ur.empty_guards())
        second, fleet = self.collect(FakeFleet(clusters=[cluster_doc("gemma-gpu-upgraded")]), full=False)
        self.assertEqual([u["cluster"] for u in second["unchanged"]], [GEMMA])
        commands = {c["check"]: c["command"] for c in second["unchanged"][0]["commands"]}
        self.assertEqual(set(commands), {check for check, _ in ur.CHECK_READS})
        self.assertEqual(set(commands.values()), {commands[ur.CHECK_OPERATION_FAILED]})
        self.assertTrue(commands[ur.CHECK_OPERATION_FAILED].startswith("gcloud container operations list"))
        self.assertFalse(any(c[0] == "kubectl" for c in fleet.calls))

    def test_a_failed_operations_listing_vouches_for_no_operation_check(self):
        # One 429 on `operations list` must not publish upgrade-operation-failed as run.
        result, _ = self.collect(FakeFleet(ops_fail={PROJECT}))
        self.assertEqual(result["operations_unread"], [PROJECT])
        self.assertTrue(any(e.startswith(f"{PROJECT}: gcloud container operations") for e in result["failed_reads"]))
        for review in result["reviews"]:
            checks = [c["check"] for c in review["commands"]]
            self.assertNotIn(ur.CHECK_OPERATION_FAILED, checks)
            self.assertIn(ur.CHECK_BROKE_WORKLOAD, checks)
        # The next run, with the listing still failing: unchanged rows carry only what a re-read backs.
        second, _ = self.collect(FakeFleet(ops_fail={PROJECT}))
        for row in second["unchanged"]:
            checks = [c["check"] for c in row["commands"]]
            self.assertNotIn(ur.CHECK_OPERATION_FAILED, checks)
            self.assertTrue(all(" kubectl get " in c["command"] for c in row["commands"]))
        # An unchanged row names the listing that failed: the operation check is unevaluated and the
        # failed listing is a limitation, as on a review.
        for row in second["unchanged"]:
            self.assertIn({"check": ur.CHECK_OPERATION_FAILED, "reason": "operations read failed"}, row["checks_unevaluated"])
            self.assertTrue(row["limitations"].startswith("reads that failed: gcloud container operations"), row["limitations"])
        # And a scoped run of a cluster with no guard to re-read carries no command at all, and still
        # says why the operation check did not run.
        ur.write_json_atomically(self.home / ur.GUARDS_FILENAME, ur.empty_guards())
        third, _ = self.collect(FakeFleet(clusters=[cluster_doc("gemma-gpu-upgraded")], ops_fail={PROJECT}), full=False)
        self.assertEqual([(u["cluster"], u["commands"]) for u in third["unchanged"]], [(GEMMA, [])])
        [row] = third["unchanged"]
        self.assertEqual(row["checks_unevaluated"], [{"check": ur.CHECK_OPERATION_FAILED, "reason": "operations read failed"}])
        self.assertIn("gcloud container operations", row["limitations"])

    def test_a_review_with_no_answered_read_carries_the_operation_check_alone(self):
        every_read = {("seeded-a", key) for key in READS["seeded-a"]}
        result, _ = self.collect(FakeFleet(kubectl_fail=every_read))
        seeded = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertEqual((seeded["answered"], [c["check"] for c in seeded["commands"]]), ([], [ur.CHECK_OPERATION_FAILED]))
        self.assertEqual(seeded["partial"], list(ur.CORE_READS))


class ManifestTest(unittest.TestCase):
    """The collector manifest speaks the fleet-audit `finish` contract: it is
    accepted by `load_manifest`, survives `cross_check_manifest` against the
    document the SOP would write, and its held guards are what `finish`
    still flags."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        scratch = self.home / "scratch"
        scratch.mkdir()
        self.env = mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: self.tmp.name, ur.STORE_HOME_ENV: self.tmp.name, "FLEET_AUDIT_SCRATCH_DIR": str(scratch)})
        self.env.start()
        import audit_report

        self.audit_report = audit_report
        # `SCRATCH_DIR` is bound at import; the run record a real `start` writes there
        # must not reach this suite.
        self.scratch = mock.patch.object(audit_report, "SCRATCH_DIR", str(scratch))
        self.scratch.start()

    def tearDown(self):
        self.scratch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def collect(self, fleet=None, now=NOW, **overrides):
        fleet = fleet or FakeFleet()
        overrides.setdefault("manifest_file", str(self.home / "manifest.json"))
        with redirect_stderr(io.StringIO()):
            return ur.collect(args(**overrides), run=fleet, now=now), fleet

    def document_from(self, manifest):
        """The findings document the SOP writes from this manifest: every
        collected target in scope.clusters with the manifest's checks_run,
        every other target skipped, every candidate a finding."""
        clusters, skipped, findings = [], [], []
        for entry in manifest["clusters"]:
            if entry["outcome"] == ur.MANIFEST_OUTCOME_COLLECTED:
                cluster = {"name": entry["name"], "checks_run": [{"check": c["check"], "command": c["command"]} for c in entry["commands"]]}
                if entry.get("limitations"):
                    cluster["limitations"] = entry["limitations"]
                clusters.append(cluster)
                findings.extend({"check": c["check"], "cluster": c["cluster"], "namespace": c["namespace"], "object": c["object"]} for c in entry["candidates"])
            elif entry["outcome"] != ur.MANIFEST_OUTCOME_OUT_OF_SCOPE:
                skipped.append({"cluster": entry["name"], "reason": entry["error"]})
        return {"audit": ur.AUDIT_ID, "scope": {"clusters": clusters, "skipped": skipped}, "findings": findings}


    def test_manifest_is_the_finish_contract(self):
        result, _ = self.collect()
        path = Path(result["manifest_path"])
        manifest = self.audit_report.load_manifest(str(path), ur.AUDIT_ID)
        self.assertEqual((manifest["version"], manifest["audit"], manifest["started_at"], manifest["finished_at"]), (ur.MANIFEST_VERSION, ur.AUDIT_ID, "2026-10-08T18:00:00Z", "2026-10-08T18:00:00Z"))
        self.assertEqual(len(manifest["checks_revision"]), ur.REVISION_DIGEST_CHARS)
        self.assertEqual([(c["name"], c["outcome"]) for c in manifest["clusters"]], [(GEMMA, "collected"), (SEEDED, "collected")])
        seeded = next(c for c in manifest["clusters"] if c["name"] == SEEDED)
        self.assertEqual([c["check"] for c in seeded["commands"]], [check for check, _ in ur.CHECK_READS])
        self.assertTrue(all(c["rc"] == 0 for c in seeded["commands"]))
        checks = {c["check"]: c for c in seeded["candidates"]}
        self.assertEqual(checks[ur.CHECK_BROKE_WORKLOAD]["severity"], ur.MANIFEST_SEVERITY_MAJOR)
        self.assertEqual({c["severity"] for c in seeded["candidates"] if c["check"] != ur.CHECK_BROKE_WORKLOAD}, {ur.MANIFEST_SEVERITY_MINOR})
        unclassified = next(c for c in seeded["candidates"] if c["check"] == ur.CHECK_SYMPTOM_UNCLASSIFIED)
        self.assertEqual((unclassified["namespace"], unclassified["object"]), ("seeded-stall", "Deployment/inventory-api"))
        self.assertEqual(unclassified["impact"], ur.UNCLASSIFIED_MITIGATION_TEXT)
        self.assertTrue(unclassified["command"].endswith(" kubectl get pods -A -o json"))
        budget = next(c for c in seeded["candidates"] if c["object"] == "PodDisruptionBudget/inference-server")
        self.assertEqual((budget["check"], budget["severity"], budget["namespace"]), (ur.CHECK_BROKE_WORKLOAD, ur.MANIFEST_SEVERITY_MAJOR, "seeded-capacity"))
        self.assertIn(f"Mitigate before: {ur.BUDGET_DOWNSTREAM_MITIGATE_BEFORE}", budget["impact"])
        self.assertNotIn("Give the budget room", budget["impact"])
        self.assertEqual(seeded["facts"]["versions_after"]["control_plane"], "1.35.8-gke.1380001")
        self.assertEqual(len(seeded["facts"]["operations"]), 4)
        self.assertEqual(manifest["partial"], False)
        self.assertEqual(manifest["still_flagged"], [])
        # The ids `finish` derives from the candidates are the ones the SOP's findings get.
        for candidate in seeded["candidates"]:
            self.assertEqual(self.audit_report.derive_finding_id(candidate), self.audit_report.derive_finding_id({k: candidate[k] for k in ("check", "cluster", "namespace", "object")}))
        self.assertIsNone(self.audit_report.cross_check_manifest(self.document_from(manifest), manifest))

    def test_cross_check_rejects_what_the_manifest_says_did_not_happen(self):
        # Unreachable: its credentials failed and its project's operations listing failed too, so no
        # read of it answered (with the listing answered the entry is collected on that one check).
        result, _ = self.collect(FakeFleet(broken=["gemma-gpu-upgraded"], ops_fail=[PROJECT]))
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        gemma = next(c for c in manifest["clusters"] if c["name"] == GEMMA)
        self.assertEqual(gemma["outcome"], ur.MANIFEST_OUTCOME_UNREACHABLE)
        self.assertIn("get-credentials rc=1", gemma["error"])
        document = self.document_from(manifest)
        self.assertEqual([s["cluster"] for s in document["scope"]["skipped"]], [GEMMA])
        self.audit_report.cross_check_manifest(document, manifest)
        # A collected target dropped from the document.
        without_seeded = {**document, "scope": {"clusters": [], "skipped": document["scope"]["skipped"]}}
        with self.assertRaises(self.audit_report.ValidationError):
            self.audit_report.cross_check_manifest(without_seeded, manifest)
        # An unreachable target neither skipped nor listed.
        unaccounted = {**document, "scope": {"clusters": document["scope"]["clusters"], "skipped": []}}
        with self.assertRaises(self.audit_report.ValidationError):
            self.audit_report.cross_check_manifest(unaccounted, manifest)
        # A check claimed with no command behind it.
        claimed = json.loads(json.dumps(document))
        claimed["scope"]["clusters"][0]["checks_run"].append({"check": "made-up-check", "command": "true"})
        with self.assertRaises(self.audit_report.ValidationError):
            self.audit_report.cross_check_manifest(claimed, manifest)

    def test_failed_reads_are_unevaluated_checks_and_a_partial_manifest(self):
        result, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods")]))
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        seeded = next(c for c in manifest["clusters"] if c["name"] == SEEDED)
        self.assertEqual(seeded["outcome"], ur.MANIFEST_OUTCOME_COLLECTED)
        self.assertEqual([c["check"] for c in seeded["commands"]], [ur.CHECK_OPERATION_FAILED, ur.CHECK_NODE_BROKEN])
        self.assertEqual({c["check"] for c in seeded["checks_unevaluated"]}, {ur.CHECK_BROKE_WORKLOAD, ur.CHECK_SYMPTOM_TENTATIVE, ur.CHECK_SYMPTOM_UNCLASSIFIED, ur.CHECK_SYMPTOM_PREDATES})
        self.assertTrue(seeded["limitations"].startswith("reads that failed: pods:"))
        self.assertTrue(manifest["partial"])
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
        # The same on a re-checked cluster: a full run, then a week on the pods read fails in the
        # re-check. Every failure guard needed it: held, no candidate, the pods-backed checks
        # unevaluated and out of `checks_run` (the listing does not back them), the run partial.
        self.collect(now=datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc))
        later, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods")]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        manifest = json.loads(Path(later["manifest_path"]).read_text())
        seeded = next(c for c in manifest["clusters"] if c["name"] == SEEDED)
        self.assertEqual((seeded["outcome"], seeded["facts"]["status"]), (ur.MANIFEST_OUTCOME_COLLECTED, "unchanged"))
        self.assertEqual([c["check"] for c in seeded["commands"]], [ur.CHECK_OPERATION_FAILED, ur.CHECK_NODE_BROKEN])
        self.assertEqual({c["check"] for c in seeded["checks_unevaluated"]}, {ur.CHECK_BROKE_WORKLOAD, ur.CHECK_SYMPTOM_TENTATIVE, ur.CHECK_SYMPTOM_UNCLASSIFIED, ur.CHECK_SYMPTOM_PREDATES})
        self.assertTrue(seeded["limitations"].startswith("reads that failed: pods:"))
        self.assertEqual(seeded["candidates"], [])
        self.assertEqual([h for h in manifest["still_flagged"] if h["cluster"] == SEEDED], [])
        self.assertTrue(manifest["partial"])
        held = [g for g in later["guards"] if g["cluster"] == SEEDED and g["kind"] == ur.GUARD_KIND_FAILURE and g["entry"] != 1]
        self.assertTrue(held)
        # Held as they stood: last seen by the full run, no review counted.
        self.assertTrue(all(g["last_seen"] == "2026-10-09T18:00:00Z" for g in held))
        # The budget guard's keep rule read pdbs, which answered: refreshed, but its check's own
        # read (pods) failed, so it emits no candidate either.
        [budget] = [g for g in later["guards"] if g["cluster"] == SEEDED and g["entry"] == 1 and g["kind"] == ur.GUARD_KIND_FAILURE]
        self.assertEqual(budget["last_seen"], "2026-10-15T18:00:00Z")
        self.assertTrue(any(w["kind"] == ur.INCIDENT_STALE_GUARD and w["cluster"] == SEEDED for w in later["sections"]["warnings"]))
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_failed_listing_and_upgrading_clusters_are_gate_failed(self):
        self.collect(scope_projects=f"{PROJECT},other-project")
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        other = "other-project/us-central1-a/elsewhere"
        ledger["clusters"][other] = {"control_plane": "1.0.0", "node_pools": {}, "channel": "", "first_seen": "2026-10-01T00:00:00Z", "last_run": "2026-10-01T00:00:00Z"}
        ur.write_json_atomically(self.home / ur.LEDGER_FILENAME, ledger)
        ops = copy.deepcopy(OPERATIONS)
        running = next(o for o in ops if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/pinned-inference-pool" in o["targetLink"])
        running["status"], running["endTime"] = "RUNNING", None
        result, _ = self.collect(FakeFleet(list_fail=["other-project"], operations=ops), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc), scope_projects=f"{PROJECT},other-project")
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name[other]["outcome"], ur.MANIFEST_OUTCOME_GATE_FAILED)
        self.assertIn("listing failed", by_name[other]["error"])
        # Mid-window the run read nothing on it: a skipped target with the operation in flight as
        # its error, no candidate (nothing was observed), so `finish` holds the stream's findings on
        # it rather than resolving them; that costs a partial run.
        seeded = by_name[SEEDED]
        self.assertEqual(seeded["outcome"], ur.MANIFEST_OUTCOME_GATE_FAILED)
        self.assertIn("upgrading now", seeded["error"])
        self.assertNotIn("candidates", seeded)
        self.assertNotIn("checks_not_applicable", seeded)
        self.assertEqual([h for h in manifest["still_flagged"] if h["cluster"] == SEEDED], [])
        self.assertTrue(manifest["partial"])
        document = self.document_from(manifest)
        self.assertIn(SEEDED, {s["cluster"] for s in document["scope"]["skipped"]})
        self.audit_report.cross_check_manifest(document, manifest)
        # One cluster mid-window alone still makes the run partial: a cluster nobody read.
        alone, _ = self.collect(FakeFleet(operations=ops), now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        manifest = json.loads(Path(alone["manifest_path"]).read_text())
        self.assertTrue(manifest["partial"])
        self.assertEqual([(c["name"], c["outcome"]) for c in manifest["clusters"]], [(GEMMA, ur.MANIFEST_OUTCOME_COLLECTED), (SEEDED, ur.MANIFEST_OUTCOME_GATE_FAILED)])
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_held_guards_are_still_flagged_candidates(self):
        reads = {**READS["seeded-a"], "events": READS["seeded-a"]["events"] + [event("FailedAttachVolume", "AttachVolume.Attach failed for volume pv-1", name="inference-server-778b78fdb8-zzzzz", namespace="seeded-capacity")]}
        with mock.patch.dict(READS, {"seeded-a": reads}):
            self.collect()
        # The first run filed inference-server as an Error (`upgrade-broke-workload`); its guards,
        # the event-only attach one among them, carry that check. The second run re-observes the
        # pod guard, and the event guard through its object still failing: one candidate under the
        # original check, a Warning, with each guard's persistence, so the harness holds the
        # finding by its own id.
        second, _ = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        manifest = json.loads(Path(second["manifest_path"]).read_text())
        [held] = [h for h in manifest["still_flagged"] if h["object"] == "Deployment/inference-server"]
        self.assertEqual((held["check"], held["cluster"], held["namespace"], held["guards"]), (ur.CHECK_BROKE_WORKLOAD, SEEDED, "seeded-capacity", [ur.guard_id(SEEDED, 2, INFERENCE), ur.guard_id(SEEDED, 19, INFERENCE)]))
        seeded = next(c for c in manifest["clusters"] if c["name"] == SEEDED)
        self.assertEqual(seeded["outcome"], ur.MANIFEST_OUTCOME_COLLECTED)
        [candidate] = [c for c in seeded["candidates"] if c["object"] == "Deployment/inference-server"]
        self.assertEqual((candidate["check"], candidate["severity"], candidate["entries"]), (ur.CHECK_BROKE_WORKLOAD, ur.MANIFEST_SEVERITY_MINOR, "2, 19"))
        self.assertEqual(candidate["persistence"], ["persists since 2026-10-08T18:00:00Z, 2 review(s)", "persists since 2026-10-08T18:00:00Z, 2 review(s)"])
        self.assertNotIn("not_recheckable", candidate)
        self.assertTrue(candidate["excerpt"].startswith(f"guard {ur.guard_id(SEEDED, 2, INFERENCE)} persists since 2026-10-08T18:00:00Z, 2 review(s): "))
        self.assertTrue(candidate["command"].endswith(" kubectl get pods -A -o json"))
        # `finish` spells the set the way the ledger spells ids: derived, then clipped; the id is the
        # first run's finding id, so the finding is held, not resolved and refiled.
        held_id = self.audit_report.published_id(held)
        self.assertIn(held_id, self.audit_report.collector_flagged_ids(manifest))
        self.assertIn(held_id, self.audit_report.still_flagged_ids(manifest, {"findings": []}))
        self.assertEqual(held_id, self.audit_report.published_id({"check": ur.CHECK_BROKE_WORKLOAD, "cluster": SEEDED, "namespace": "seeded-capacity", "object": "Deployment/inference-server"}))
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_error_from_the_after_upgrade_route_is_filed_major_by_the_next_full_run(self):
        # Sunday's full run, then a pool upgrade on Tuesday that displaces a new replica: the
        # after-upgrade wake files it as an Error (`major`) and writes its guard. The next Sunday
        # reviews the cluster on the operation's end time, grades the row against the stored set
        # (not recorded, created inside the window: new), files the same identity at `major`, and
        # the guard carries Tuesday's first_seen with two reviews; the operation line says the
        # after-upgrade route reviewed it already.
        self.collect()
        tuesday_op = copy.deepcopy(next(o for o in ops_for("seeded-a") if "pinned-inference-pool" in o["targetLink"]))
        tuesday_op.update(name="operation-tuesday", startTime="2026-10-13T16:30:00Z", endTime="2026-10-13T17:00:00Z")
        displaced = pod("web-5d8f9c7b6-abcde", namespace="seeded-capacity", scheduled_message="0/4 nodes are available: 1 Insufficient cpu, 3 node(s) didn't match Pod's node affinity/selector.", node_selector={"cloud.google.com/gke-nodepool": "pinned-inference-pool"})
        displaced["metadata"]["creationTimestamp"] = "2026-10-13T16:40:00Z"
        web = "seeded-capacity/Pod/web-5d8f9c7b6-abcde"
        identity = {"check": ur.CHECK_BROKE_WORKLOAD, "cluster": SEEDED, "namespace": "seeded-capacity", "object": "Pod/web-5d8f9c7b6-abcde"}
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [displaced]}}):
            tuesday, _ = self.collect(FakeFleet(operations=OPERATIONS + [tuesday_op]), now=datetime(2026, 10, 13, 17, 30, tzinfo=timezone.utc), full=False, after_upgrade=True)
            self.assertTrue(tuesday["scoped"])
            manifest = json.loads(Path(tuesday["manifest_path"]).read_text())
            [filed] = [c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == identity["object"]]
            self.assertEqual((filed["check"], filed["severity"]), (ur.CHECK_BROKE_WORKLOAD, ur.MANIFEST_SEVERITY_MAJOR))
            self.assertEqual(self.audit_report.published_id(filed), self.audit_report.published_id(identity))
            guard = ur.guard_id(SEEDED, 2, web)
            self.assertEqual([(g["check"], g["reviews"], g["first_seen"]) for g in tuesday["guards"] if g["id"] == guard], [(ur.CHECK_BROKE_WORKLOAD, 1, "2026-10-13T17:30:00Z")])
            sunday, _ = self.collect(FakeFleet(operations=OPERATIONS + [tuesday_op]), now=datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc))
        review = next(r for r in sunday["reviews"] if r["cluster"] == SEEDED)
        [op] = review["what_happened"]["operations"]
        self.assertEqual(op["already_reviewed"]["route"], ur.ROUTE_AFTER_UPGRADE)
        self.assertIn(ur.ALREADY_REVIEWED_FORMAT.format(route=ur.ROUTE_AFTER_UPGRADE, at="2026-10-13T17:30:00Z"), ur.render_report(sunday))
        manifest = json.loads(Path(sunday["manifest_path"]).read_text())
        [again] = [c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == identity["object"]]
        self.assertEqual((again["check"], again["severity"]), (ur.CHECK_BROKE_WORKLOAD, ur.MANIFEST_SEVERITY_MAJOR))
        # The incident's candidate is the object's one candidate and carries the guard's persistence.
        self.assertEqual(again["persistence"], ["persists since 2026-10-13T17:30:00Z, 2 review(s)"])
        self.assertTrue(again["excerpt"].endswith("persists since 2026-10-13T17:30:00Z, 2 review(s)"), again["excerpt"])
        self.assertEqual(len([c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == identity["object"]]), 1)
        self.assertEqual(self.audit_report.published_id(again), self.audit_report.published_id(identity))
        self.assertIn(web, [i["object"] for i in sunday["sections"]["errors"]])
        self.assertEqual([(g["check"], g["reviews"], g["first_seen"]) for g in sunday["guards"] if g["id"] == guard], [(ur.CHECK_BROKE_WORKLOAD, 2, "2026-10-13T17:30:00Z")])
        # The report's guard line carries the store's record, not this run's stamp.
        [line] = [l for l in ur.render_report(sunday).splitlines() if l.startswith(f"- guard `{guard}`")]
        self.assertTrue(line.endswith("first seen 2026-10-13T17:30:00Z, 2 review(s)"), line)
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_a_regraded_incident_moves_the_finding_identity_once(self):
        # inference-server is an Error on the first run (`upgrade-broke-workload`). The cluster upgrades
        # again with the symptom standing: the review grades it as predating the upgrade and files it
        # under `symptom-predates-upgrade`. One finding per object: the new candidate carries the new
        # check, no held candidate rides beside it under the old one, the guard follows, and the next
        # unchanged run holds it under the new check alone.
        first, _ = self.collect()
        guard = ur.guard_id(SEEDED, 2, INFERENCE)
        self.assertEqual([g["check"] for g in first["guards"] if g["id"] == guard], [ur.CHECK_BROKE_WORKLOAD])
        bumped = cluster_doc("seeded-a")
        bumped["currentMasterVersion"] = "1.36.4-gke.1247000"
        master = copy.deepcopy(next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_MASTER" and "/clusters/seeded-a" in o["targetLink"]))
        master.update(name="operation-second-master", startTime="2026-10-15T09:00:00Z", endTime="2026-10-15T09:10:00Z")
        fleet = FakeFleet(clusters=[bumped, cluster_doc("gemma-gpu-upgraded")], operations=OPERATIONS + [master])
        second, _ = self.collect(fleet, now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        review = next(r for r in second["reviews"] if r["cluster"] == SEEDED)
        self.assertTrue(next(i for i in second["sections"]["warnings"] if i["object"] == INFERENCE)["predates_upgrade"])
        manifest = json.loads(Path(second["manifest_path"]).read_text())
        seeded = next(c for c in manifest["clusters"] if c["name"] == SEEDED)
        inference = [c for c in seeded["candidates"] if c["object"] == "Deployment/inference-server"]
        self.assertEqual([(c["check"], c["severity"], c.get("persistence")) for c in inference], [(ur.CHECK_SYMPTOM_PREDATES, ur.MANIFEST_SEVERITY_MINOR, ["persists since 2026-10-08T18:00:00Z, 2 review(s)"])])
        self.assertEqual([h for h in manifest["still_flagged"] if h["object"] == "Deployment/inference-server"], [])
        self.assertEqual([(g["check"], g["reviews"]) for g in second["guards"] if g["id"] == guard], [(ur.CHECK_SYMPTOM_PREDATES, 2)])
        ids = [self.audit_report.derive_finding_id(f) for f in self.document_from(manifest)["findings"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
        # Unchanged a week on: the held candidate rides under the new check only.
        third, _ = self.collect(fleet, now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc))
        manifest = json.loads(Path(third["manifest_path"]).read_text())
        seeded = next(c for c in manifest["clusters"] if c["name"] == SEEDED)
        held = [c for c in seeded["candidates"] if c["object"] == "Deployment/inference-server"]
        self.assertEqual([(c["check"], c["persistence"]) for c in held], [(ur.CHECK_SYMPTOM_PREDATES, ["persists since 2026-10-08T18:00:00Z, 3 review(s)"])])
        self.assertEqual([h["check"] for h in manifest["still_flagged"] if h["object"] == "Deployment/inference-server"], [ur.CHECK_SYMPTOM_PREDATES])

    def test_persisting_guard_keeps_its_check_id_and_a_cleared_guard_emits_nothing(self):
        # payments-api's entry-14 guard (pod-sourced, a Warning) persists through two re-checks under
        # `upgrade-symptom-tentative` with its review count; once the pods are healthy the re-check
        # clears it and the manifest emits nothing for it, so the harness resolves the finding.
        first, _ = self.collect()
        first_manifest = json.loads(Path(first["manifest_path"]).read_text())
        [filed] = [c for c in next(e for e in first_manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == "Deployment/payments-api"]
        filed_id = self.audit_report.published_id(filed)
        self.assertEqual(filed["check"], ur.CHECK_SYMPTOM_TENTATIVE)
        guard = ur.guard_id(SEEDED, 14, PAYMENTS)
        self.assertEqual([(g["check"], g["reviews"]) for g in first["guards"] if g["id"] == guard], [(ur.CHECK_SYMPTOM_TENTATIVE, 1)])
        for week, reviews in ((15, 2), (22, 3)):
            run, _ = self.collect(now=datetime(2026, 10, week, 18, 0, tzinfo=timezone.utc))
            manifest = json.loads(Path(run["manifest_path"]).read_text())
            [candidate] = [c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == "Deployment/payments-api"]
            self.assertEqual((candidate["check"], candidate["severity"], candidate["persistence"]), (ur.CHECK_SYMPTOM_TENTATIVE, ur.MANIFEST_SEVERITY_MINOR, [f"persists since 2026-10-08T18:00:00Z, {reviews} review(s)"]))
            self.assertEqual(self.audit_report.published_id(candidate), filed_id)
            self.assertIn(filed_id, self.audit_report.still_flagged_ids(manifest, {"findings": []}))
            self.assertEqual([(g["check"], g["reviews"]) for g in run["guards"] if g["id"] == guard], [(ur.CHECK_SYMPTOM_TENTATIVE, reviews)])
            self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
        healthy = [p for p in READS["seeded-a"]["pods"] if "payments-api" not in p["metadata"]["name"]]
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "pods": healthy}}):
            cleared, _ = self.collect(now=datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc))
        self.assertNotIn(guard, {g["id"] for g in cleared["guards"]})
        manifest = json.loads(Path(cleared["manifest_path"]).read_text())
        self.assertEqual([c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == "Deployment/payments-api"], [])
        self.assertNotIn(filed_id, self.audit_report.still_flagged_ids(manifest, {"findings": []}))
        self.assertNotIn(filed_id, self.audit_report.collector_flagged_ids(manifest))

    def test_every_collected_entry_carries_the_six_checks_the_roster_reads(self):
        # The contract main's `coverage_gaps` reads: every `scope.clusters` entry is measured against
        # the stream's roster, and a check in neither `checks_run` nor `checks_not_applicable` is a
        # gap. A reviewed cluster carries all six with the read that decided each. An unchanged
        # cluster carries all six too: the operations listing that found no upgrade, with the
        # cluster's versions, answers every check that asks what the upgrade did, and a check whose
        # read the re-check made is backed by that read instead. The roster is `CHECK_READS`; until
        # the SOP registers it in `AUDITS`, the test registers it the same way.
        roster = tuple(check for check, _ in ur.CHECK_READS)
        self.assertEqual(len(roster), 6)
        spec = self.audit_report.AuditSpec("Upgrade retrospective", "upgrade_retrospective_sop.md", roster)
        first, _ = self.collect()
        second, _ = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(sorted(u["cluster"] for u in second["unchanged"]), [GEMMA, SEEDED])
        with mock.patch.dict(self.audit_report.AUDITS, {ur.AUDIT_ID: spec}):
            for label, run in (("reviewed", first), ("unchanged", second)):
                with self.subTest(label):
                    manifest = json.loads(Path(run["manifest_path"]).read_text())
                    self.assertEqual([c["outcome"] for c in manifest["clusters"]], [ur.MANIFEST_OUTCOME_COLLECTED] * 2)
                    for entry in manifest["clusters"]:
                        self.assertEqual([c["check"] for c in entry["commands"]], list(roster))
                        self.assertNotIn("checks_unevaluated", entry)
                        self.assertNotIn("limitations", entry)
                    self.assertEqual(self.audit_report.coverage_gaps(self.document_from(manifest)), [])
                    self.assertFalse(manifest["partial"])
            seeded = next(c for c in json.loads(Path(second["manifest_path"]).read_text())["clusters"] if c["name"] == SEEDED)
            commands = {c["check"]: c["command"] for c in seeded["commands"]}
            self.assertTrue(commands[ur.CHECK_OPERATION_FAILED].startswith("gcloud container operations list"))
            self.assertIn(" kubectl get pods -A -o json", commands[ur.CHECK_BROKE_WORKLOAD])
            self.assertIn(" kubectl get nodes -o json", commands[ur.CHECK_NODE_BROKEN])
            # A read that ran and failed is the gap `coverage_gaps` names: the four pod checks leave
            # `checks_run`, the read is the limitation, and the run is partial.
            third, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods")]), now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc))
            manifest = json.loads(Path(third["manifest_path"]).read_text())
            [gap] = self.audit_report.coverage_gaps(self.document_from(manifest))
            self.assertTrue(gap.startswith(f"{SEEDED}: partially audited — 4 of 6 applicable checks did not run ({ur.CHECK_BROKE_WORKLOAD}, "), gap)
            self.assertIn("reads that failed: pods", gap)
            self.assertTrue(manifest["partial"])

    def test_a_guard_keeps_its_id_first_seen_and_reviews_across_a_run_that_did_not_read_it(self):
        # Sunday 1 files the entry-1 finding on inference-server's budget. Sunday 2 the pdbs read
        # fails: the guard is held as it stands, no candidate for it, the read is a limitation and the
        # run is partial, so `finish` resolves nothing and the entry leaves the ledger body for the
        # week. Sunday 3 reads: the candidate returns under Sunday 1's id with the first-seen date
        # and the review count the gap did not move.
        budget = "seeded-capacity/PodDisruptionBudget/inference-server"
        identity = {"check": ur.CHECK_BROKE_WORKLOAD, "cluster": SEEDED, "namespace": "seeded-capacity", "object": "PodDisruptionBudget/inference-server"}
        guard = ur.guard_id(SEEDED, 1, budget)
        first, _ = self.collect()
        manifest = json.loads(Path(first["manifest_path"]).read_text())
        [filed] = [c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == identity["object"]]
        self.assertEqual((filed["check"], filed["severity"]), (ur.CHECK_BROKE_WORKLOAD, ur.MANIFEST_SEVERITY_MAJOR))
        filed_id = self.audit_report.published_id(filed)
        self.assertEqual(filed_id, self.audit_report.published_id(identity))
        self.assertEqual([(g["first_seen"], g["reviews"]) for g in first["guards"] if g["id"] == guard], [("2026-10-08T18:00:00Z", 1)])
        second, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pdbs")]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        [recheck] = [r for r in second["rechecks"] if r["cluster"] == SEEDED]
        self.assertEqual(recheck["held"][guard], ["pdbs"])
        self.assertTrue(all(missing == ["pdbs"] for missing in recheck["held"].values()), recheck["held"])
        self.assertEqual([(g["first_seen"], g["reviews"], g["last_seen"]) for g in second["guards"] if g["id"] == guard], [("2026-10-08T18:00:00Z", 1, "2026-10-08T18:00:00Z")])
        manifest = json.loads(Path(second["manifest_path"]).read_text())
        seeded = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        self.assertEqual(seeded["outcome"], ur.MANIFEST_OUTCOME_COLLECTED)
        self.assertEqual([c for c in seeded["candidates"] if c["object"] == identity["object"]], [])
        self.assertTrue(seeded["limitations"].startswith("reads that failed: pdbs:"), seeded["limitations"])
        self.assertTrue(manifest["partial"])
        self.assertNotIn(filed_id, self.audit_report.still_flagged_ids(manifest, {"findings": []}))
        document = self.document_from(manifest)
        self.assertTrue(any(gap.startswith(f"{SEEDED}: partially audited") for gap in self.audit_report.coverage_gaps(document)))
        self.audit_report.cross_check_manifest(document, manifest)
        third, _ = self.collect(now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc))
        manifest = json.loads(Path(third["manifest_path"]).read_text())
        [again] = [c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == identity["object"]]
        self.assertEqual(self.audit_report.published_id(again), filed_id)
        self.assertEqual((again["check"], again["persistence"]), (ur.CHECK_BROKE_WORKLOAD, ["persists since 2026-10-08T18:00:00Z, 2 review(s)"]))
        self.assertIn(filed_id, self.audit_report.still_flagged_ids(manifest, {"findings": []}))
        self.assertEqual([(g["first_seen"], g["reviews"]) for g in third["guards"] if g["id"] == guard], [("2026-10-08T18:00:00Z", 2)])
        self.assertFalse(manifest["partial"])
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_a_guard_keeps_its_record_across_a_sunday_its_cluster_was_upgrading(self):
        # The same gap with the cluster upgrading on Sunday 2: `gate-failed`, nothing read, no
        # candidate, the guard untouched (the ledger keeps Sunday 1 as its last run). Sunday 3 reviews
        # the cluster on the operation that ended and files the hold afresh, an Error under Sunday 1's
        # id with no persistence note; the guard carries its first-seen date and two reviews.
        budget = "seeded-capacity/PodDisruptionBudget/inference-server"
        identity = {"check": ur.CHECK_BROKE_WORKLOAD, "cluster": SEEDED, "namespace": "seeded-capacity", "object": "PodDisruptionBudget/inference-server"}
        guard = ur.guard_id(SEEDED, 1, budget)
        first, _ = self.collect()
        filed_id = self.audit_report.published_id(identity)
        self.assertIn(filed_id, self.audit_report.collector_flagged_ids(json.loads(Path(first["manifest_path"]).read_text())))
        running = copy.deepcopy(next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/pinned-inference-pool" in o["targetLink"]))
        running.update(name="operation-second-pinned", startTime="2026-10-15T16:00:00Z", endTime=None, status="RUNNING")
        second, _ = self.collect(FakeFleet(operations=OPERATIONS + [running]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        manifest = json.loads(Path(second["manifest_path"]).read_text())
        seeded = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        self.assertEqual(seeded["outcome"], ur.MANIFEST_OUTCOME_GATE_FAILED)
        self.assertNotIn("candidates", seeded)
        self.assertTrue(manifest["partial"])
        self.assertNotIn(filed_id, self.audit_report.still_flagged_ids(manifest, {"findings": []}))
        self.assertEqual([(g["first_seen"], g["reviews"], g["last_seen"]) for g in second["guards"] if g["id"] == guard], [("2026-10-08T18:00:00Z", 1, "2026-10-08T18:00:00Z")])
        done = {**running, "status": "DONE", "endTime": "2026-10-15T17:05:00Z"}
        third, _ = self.collect(FakeFleet(operations=OPERATIONS + [done]), now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc))
        manifest = json.loads(Path(third["manifest_path"]).read_text())
        [again] = [c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == identity["object"]]
        self.assertEqual(self.audit_report.published_id(again), filed_id)
        self.assertEqual((again["check"], again["severity"], again.get("persistence")), (ur.CHECK_BROKE_WORKLOAD, ur.MANIFEST_SEVERITY_MAJOR, ["persists since 2026-10-08T18:00:00Z, 2 review(s)"]))
        self.assertIn(SEEDED, [r["cluster"] for r in third["reviews"]])
        self.assertIn(filed_id, self.audit_report.still_flagged_ids(manifest, {"findings": []}))
        self.assertEqual([(g["first_seen"], g["reviews"]) for g in third["guards"] if g["id"] == guard], [("2026-10-08T18:00:00Z", 2)])
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_a_raising_recheck_is_a_failed_read_of_every_kind_it_would_have_read(self):
        # The exception boundary around a re-check: the reads it would have made are failed reads,
        # so the unchanged row lists their checks as unevaluated, the listing backs the operation
        # check alone, the guards are held untouched with no candidate, and the run is partial, as
        # after a credentials failure, rather than every check claimed as run beside a limitation
        # saying none was evaluated.
        first, _ = self.collect()
        held = [g for g in first["guards"] if g["cluster"] == SEEDED]
        before = {g["id"]: (g["last_seen"], g["reviews"]) for g in held}

        def boom(*args, **kwargs):
            raise RuntimeError("planted")

        with mock.patch.object(ur, "collect_symptoms", boom):
            second, _ = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        needed = ur.recheck_reads_needed(held, refresh=True)
        self.assertTrue({"pods", "nodes", "owners"} <= needed)
        [recheck] = [r for r in second["rechecks"] if r["cluster"] == SEEDED]
        self.assertEqual((recheck["failed_reads"], recheck["cleared"], recheck["refreshed"], recheck["commands"]), (sorted(needed), [], [], {}))
        self.assertEqual(recheck["held"], {g["id"]: sorted(ur.recheck_reads_for(g)) for g in held})
        self.assertEqual(recheck["errors"], ["re-check failed: RuntimeError('planted')"])
        unevaluated = [check for check, read in ur.CHECK_READS if read in needed]
        self.assertEqual(len(unevaluated), 5)
        row = next(u for u in second["unchanged"] if u["cluster"] == SEEDED)
        self.assertEqual(([c["check"] for c in row["commands"]], [c["check"] for c in row["checks_unevaluated"]]), ([ur.CHECK_OPERATION_FAILED], unevaluated))
        self.assertIn("re-check failed: RuntimeError('planted')", row["limitations"])
        self.assertEqual({g["id"]: (g["last_seen"], g["reviews"]) for g in second["guards"] if g["cluster"] == SEEDED}, before)
        manifest = json.loads(Path(second["manifest_path"]).read_text())
        seeded = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        self.assertEqual((seeded["outcome"], [c["check"] for c in seeded["commands"]], seeded["candidates"]), (ur.MANIFEST_OUTCOME_COLLECTED, [ur.CHECK_OPERATION_FAILED], []))
        self.assertEqual([c["check"] for c in seeded["checks_unevaluated"]], unevaluated)
        self.assertTrue(manifest["partial"])
        self.assertEqual([h for h in manifest["still_flagged"] if h["cluster"] == SEEDED], [])
        self.assertIn(ur.RECHECK_HELD_FORMAT.format(count=len(held)), ur.render_report(second))
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_a_failed_feeding_read_is_a_limitation_and_an_info_read_is_not(self):
        # The read map: budgets feed the pod checks, so a failed pdbs read on a review is a limitation
        # with no check unevaluated; the budget guard it could have shown is held unchanged with no
        # candidate, what the pods read shows is filed, and the run is partial. The webhooks read costs
        # Info alone: its failure is a "Reads that failed" line and nothing else.
        budget = "seeded-capacity/PodDisruptionBudget/inference-server"
        guard = ur.guard_id(SEEDED, 1, budget)
        first, _ = self.collect()
        before = next(g for g in first["guards"] if g["id"] == guard)["last_seen"]
        pinned = next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/pinned-inference-pool" in o["targetLink"])
        second_op = {**copy.deepcopy(pinned), "name": "operation-second-pinned", "startTime": "2026-10-15T16:00:00Z", "endTime": "2026-10-15T16:40:00Z"}
        pdbs_failed, _ = self.collect(FakeFleet(operations=OPERATIONS + [second_op], kubectl_fail=[("seeded-a", "pdbs")]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        review = next(r for r in pdbs_failed["reviews"] if r["cluster"] == SEEDED)
        self.assertEqual(review["partial"], ["pdbs"])
        manifest = json.loads(Path(pdbs_failed["manifest_path"]).read_text())
        seeded = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        self.assertEqual(seeded["outcome"], ur.MANIFEST_OUTCOME_COLLECTED)
        self.assertNotIn("checks_unevaluated", seeded)
        self.assertTrue(seeded["limitations"].startswith("reads that failed: pdbs:"), seeded["limitations"])
        self.assertEqual([c["check"] for c in seeded["commands"]], [check for check, _ in ur.CHECK_READS])
        self.assertEqual([g["last_seen"] for g in pdbs_failed["guards"] if g["id"] == guard], [before])
        objects = [c["object"] for c in seeded["candidates"]]
        self.assertNotIn("PodDisruptionBudget/inference-server", objects)
        self.assertIn("Deployment/inference-server", objects)
        self.assertTrue(manifest["partial"])
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
        third_op = {**second_op, "name": "operation-third-pinned", "startTime": "2026-10-22T16:00:00Z", "endTime": "2026-10-22T16:40:00Z"}
        webhooks_failed, _ = self.collect(FakeFleet(operations=OPERATIONS + [second_op, third_op], kubectl_fail=[("seeded-a", "webhooks")]), now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc))
        review = next(r for r in webhooks_failed["reviews"] if r["cluster"] == SEEDED)
        self.assertTrue(review["reviewed"])
        self.assertEqual(review["baseline"]["shape_reads_failed"], [e for e in review["read_errors"] if e.startswith("webhooks:")])
        self.assertTrue(any(line.startswith(f"{SEEDED}: webhooks:") for line in webhooks_failed["failed_reads"]))
        manifest = json.loads(Path(webhooks_failed["manifest_path"]).read_text())
        seeded = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        self.assertNotIn("limitations", seeded)
        self.assertNotIn("checks_unevaluated", seeded)
        self.assertFalse(manifest["partial"])
        self.assertIn(ur.INFO_FAILED_READS, ur.render_report(webhooks_failed))
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_an_operation_inside_the_settle_floor_holds_its_cluster_on_every_route(self):
        # The settle floor applies on every route: a cluster whose operation reached DONE less than
        # fifteen minutes before a full run is listed as settling with the upgrading clusters,
        # `gate-failed` in the manifest with nothing read and its last run unchanged; a forced
        # `--cluster` run inside the floor gets the same line; the full run fifteen minutes on reviews it.
        self.collect()
        pinned = next(o for o in OPERATIONS if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/pinned-inference-pool" in o["targetLink"])
        fresh = {**copy.deepcopy(pinned), "name": "operation-fresh", "startTime": "2026-10-15T17:40:00Z", "endTime": "2026-10-15T17:55:00Z"}
        later = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        held_run, fleet = self.collect(FakeFleet(operations=OPERATIONS + [fresh]), now=later)
        self.assertFalse(held_run["scoped"])
        self.assertEqual([r["cluster"] for r in held_run["reviews"]], [])
        self.assertEqual([(u["cluster"], u["operation"]["name"], u.get("settling"), u.get("waiting")) for u in held_run["upgrading"]], [(SEEDED, "operation-fresh", True, 0)])
        self.assertFalse(any("get-credentials" in c and "seeded-a" in c for c in fleet.calls))
        line = ur.SETTLING_LINE.format(cluster=SEEDED, operation="UPGRADE_NODES", target="pinned-inference-pool", end="2026-10-15T17:55:00Z", minutes=15)
        self.assertTrue(line.endswith("or the next full run"))
        self.assertIn("- " + line, ur.render_report(held_run))
        manifest = json.loads(Path(held_run["manifest_path"]).read_text())
        seeded = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        self.assertEqual((seeded["outcome"], "candidates" in seeded, manifest["partial"]), (ur.MANIFEST_OUTCOME_GATE_FAILED, False, True))
        self.assertIn("under 15 min ago", seeded["error"])
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED]["last_run"], "2026-10-08T18:00:00Z")
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
        forced, _ = self.collect(FakeFleet(operations=OPERATIONS + [fresh]), now=later, cluster=[SEEDED], full=False)
        self.assertEqual(([r["cluster"] for r in forced["reviews"]], [(u["cluster"], u.get("settling")) for u in forced["upgrading"]]), ([], [(SEEDED, True)]))
        reviewed, _ = self.collect(FakeFleet(operations=OPERATIONS + [fresh]), now=later + timedelta(minutes=15))
        self.assertEqual([r["cluster"] for r in reviewed["reviews"]], [SEEDED])
        self.assertIn("operation-fresh", [op["name"] for op in reviewed["reviews"][0]["what_happened"]["operations"]])
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][SEEDED]["last_run"], "2026-10-15T18:15:00Z")

    def test_a_failed_operation_is_its_own_finding_with_a_guard_re_checked_through_the_listing(self):
        # A failed operation is an incident of its own kind, `Operation/<name>` under
        # `upgrade-operation-failed`, with a failure guard re-checked through the listing every run
        # makes: refreshed while the listing still shows the failure (the held candidate carries the
        # persistence), held with the check unevaluated when the listing fails, cleared with an Info
        # line when a later operation of the same type on the same target reaches DONE without error.
        ops = copy.deepcopy(OPERATIONS)
        errored = next(o for o in ops if o.get("error"))
        bad = next(o for o in ops if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/idle-batch-pool" in o["targetLink"])
        bad["error"], bad["statusMessage"] = errored["error"], errored["statusMessage"]
        name = bad["name"]
        obj = f"Operation/{name}"
        identity = {"check": ur.CHECK_OPERATION_FAILED, "cluster": SEEDED, "namespace": "", "object": obj}
        guard = ur.guard_id(SEEDED, ur.ENTRY_OPERATION, obj)
        first, _ = self.collect(FakeFleet(operations=ops))
        manifest = json.loads(Path(first["manifest_path"]).read_text())
        [filed] = [c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == obj]
        self.assertEqual((filed["check"], filed["severity"], filed["namespace"]), (ur.CHECK_OPERATION_FAILED, ur.MANIFEST_SEVERITY_MAJOR, ""))
        filed_id = self.audit_report.published_id(filed)
        self.assertEqual(filed_id, self.audit_report.published_id(identity))
        [record] = [g for g in first["guards"] if g["id"] == guard]
        self.assertEqual((record["kind"], record["entry"], record["source"], record["check"], record["reviews"], record["reads"], record[ur.OPERATION_GUARD_START_KEY]), (ur.GUARD_KIND_FAILURE, ur.ENTRY_OPERATION, ur.OPERATIONS_READ, ur.CHECK_OPERATION_FAILED, 1, [], "2026-10-08T03:46:22Z"))
        self.assertEqual((record[ur.OPERATION_GUARD_TYPE_KEY], record[ur.OPERATION_GUARD_TARGET_KEY]), ("UPGRADE_NODES", "idle-batch-pool"))
        self.assertTrue(record["evidence"].startswith("UPGRADE_NODES on idle-batch-pool ended DONE: Instance"), record["evidence"])
        self.assertIn(f"### operation DONE — {SEEDED} — `{obj}`", ur.render_report(first))
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
        # Sunday 2, the listing unchanged: the cluster is unchanged, the guard is refreshed through the
        # listing and its candidate is held under the same id with the persistence.
        second, _ = self.collect(FakeFleet(operations=ops), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(second["operation_guards"], {"refreshed": [guard], "cleared": [], "held": [], "unlisted": []})
        self.assertIn(SEEDED, [u["cluster"] for u in second["unchanged"]])
        manifest = json.loads(Path(second["manifest_path"]).read_text())
        [held] = [c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == obj]
        self.assertEqual((held["check"], held["severity"], held["persistence"], held["entries"]), (ur.CHECK_OPERATION_FAILED, ur.MANIFEST_SEVERITY_MINOR, ["persists since 2026-10-08T18:00:00Z, 2 review(s)"], ur.ENTRY_OPERATION))
        self.assertTrue(held["impact"].startswith(ur.OPERATION_MITIGATE_TEXT))
        self.assertTrue(held["command"].startswith("gcloud container operations list"))
        self.assertEqual(self.audit_report.published_id(held), filed_id)
        self.assertIn(filed_id, self.audit_report.still_flagged_ids(manifest, {"findings": []}))
        self.assertEqual([(g["reviews"], g["last_seen"]) for g in second["guards"] if g["id"] == guard], [(2, "2026-10-15T18:00:00Z")])
        self.assertIn(ur.OPERATION_GUARDS_SUMMARY_FORMAT.format(refreshed=1, held=0, unlisted=0), ur.render_report(second))
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
        # A Sunday whose listing failed: the guard is held as it stands, the operation check is
        # unevaluated, no candidate and no stale Warning for it, the run partial.
        third, _ = self.collect(FakeFleet(operations=ops, ops_fail={PROJECT}), now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(third["operation_guards"], {"refreshed": [], "cleared": [], "held": [guard], "unlisted": []})
        self.assertEqual([(g["reviews"], g["last_seen"]) for g in third["guards"] if g["id"] == guard], [(2, "2026-10-15T18:00:00Z")])
        manifest = json.loads(Path(third["manifest_path"]).read_text())
        seeded = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        self.assertIn({"check": ur.CHECK_OPERATION_FAILED, "reason": "operations read failed"}, seeded["checks_unevaluated"])
        self.assertEqual([c for c in seeded["candidates"] if c["object"] == obj], [])
        self.assertEqual([w for w in third["sections"]["warnings"] if w["kind"] == ur.INCIDENT_STALE_GUARD and w["object"] == obj], [])
        self.assertTrue(manifest["partial"])
        self.assertNotIn(filed_id, self.audit_report.still_flagged_ids(manifest, {"findings": []}))
        # Sunday 4, a later operation of the same type on the same target DONE without error: the
        # listing reaches the failed operation through the guard's recorded start, the guard is
        # cleared with an Info line, and no candidate carries the id.
        retry = {**copy.deepcopy(bad), "name": "operation-retry", "startTime": "2026-10-27T03:00:00Z", "endTime": "2026-10-27T03:12:00Z"}
        retry.pop("error", None)
        retry.pop("statusMessage", None)
        fourth, fleet = self.collect(FakeFleet(operations=ops + [retry]), now=datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc))
        listing = next(c for c in fleet.calls if "operations" in c and "list" in c)
        self.assertIn("2026-10-08T03:46:22Z", " ".join(listing))
        self.assertEqual([(item["id"], item["reason"]) for item in fourth["operation_guards"]["cleared"]], [(guard, ur.OPERATION_GUARD_RETRIED_FORMAT.format(type="UPGRADE_NODES", target="idle-batch-pool", name="operation-retry", end="2026-10-27T03:12:00Z"))])
        self.assertNotIn(guard, {g["id"] for g in fourth["guards"]})
        manifest = json.loads(Path(fourth["manifest_path"]).read_text())
        self.assertEqual([c for e in manifest["clusters"] for c in e.get("candidates") or [] if c["object"] == obj], [])
        self.assertNotIn(filed_id, self.audit_report.collector_flagged_ids(manifest))
        self.assertIn("- " + ur.OPERATION_GUARD_CLEARED_FORMAT.format(id=guard, reason=ur.OPERATION_GUARD_RETRIED_FORMAT.format(type="UPGRADE_NODES", target="idle-batch-pool", name="operation-retry", end="2026-10-27T03:12:00Z")), ur.render_report(fourth))
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_a_failed_operation_on_an_unreachable_cluster_is_published_through_the_listing(self):
        # The listing that decides `upgrade-operation-failed` answered although the cluster's credentials
        # did not: the entry is collected with that one check, the five kubectl-backed checks are
        # unevaluated, the credentials error is the limitation, and the Error the report shows is the
        # entry's candidate. With the listing failed too, nothing answered: unreachable, no candidate.
        ops = copy.deepcopy(OPERATIONS)
        errored = next(o for o in ops if o.get("error"))
        bad = next(o for o in ops if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/idle-batch-pool" in o["targetLink"])
        bad["error"], bad["statusMessage"] = errored["error"], errored["statusMessage"]
        obj = f"Operation/{bad['name']}"
        result, _ = self.collect(FakeFleet(operations=ops, broken=["seeded-a"]))
        self.assertIn(obj, [i["object"] for i in result["sections"]["errors"]])
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        seeded = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        self.assertEqual((seeded["outcome"], [c["check"] for c in seeded["commands"]]), (ur.MANIFEST_OUTCOME_COLLECTED, [ur.CHECK_OPERATION_FAILED]))
        self.assertEqual([c["check"] for c in seeded["checks_unevaluated"]], [check for check, read in ur.CHECK_READS if read != ur.OPERATIONS_READ])
        self.assertTrue(seeded["limitations"].startswith("reads that failed: get-credentials rc=1"), seeded["limitations"])
        self.assertEqual([(c["check"], c["severity"], c["object"]) for c in seeded["candidates"]], [(ur.CHECK_OPERATION_FAILED, ur.MANIFEST_SEVERITY_MAJOR, obj)])
        self.assertTrue(manifest["partial"])
        document = self.document_from(manifest)
        self.assertIn(SEEDED, [c["name"] for c in document["scope"]["clusters"]])
        self.audit_report.cross_check_manifest(document, manifest)
        self.assertIn(ur.guard_id(SEEDED, ur.ENTRY_OPERATION, obj), {g["id"] for g in result["guards"]})
        unread, _ = self.collect(FakeFleet(operations=ops, broken=["seeded-a"], ops_fail={PROJECT}), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        manifest = json.loads(Path(unread["manifest_path"]).read_text())
        seeded = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        self.assertEqual((seeded["outcome"], "candidates" in seeded), (ur.MANIFEST_OUTCOME_UNREACHABLE, False))
        self.assertEqual([i for i in unread["sections"]["errors"] if i["cluster"] == SEEDED], [])

    def test_a_review_whose_pods_read_failed_files_no_candidate_under_an_unevaluated_check(self):
        # With the pods read failed the events read still shows FailedScheduling and BackOff rows: they
        # stay Warnings in the report, but the entry lists the four pod-backed checks as unevaluated and
        # files no candidate under them, fresh or held; the harness files them once the read answers.
        result, _ = self.collect(FakeFleet(kubectl_fail=[("seeded-a", "pods")]))
        warnings = [i for i in result["sections"]["warnings"] if i["cluster"] == SEEDED and i["kind"] == ur.INCIDENT_SYMPTOM]
        self.assertTrue(warnings)
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        seeded = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        unevaluated = {c["check"] for c in seeded["checks_unevaluated"]}
        self.assertEqual(unevaluated, {ur.CHECK_BROKE_WORKLOAD, ur.CHECK_SYMPTOM_TENTATIVE, ur.CHECK_SYMPTOM_UNCLASSIFIED, ur.CHECK_SYMPTOM_PREDATES})
        self.assertEqual([c for c in seeded["candidates"] if c["check"] in unevaluated], [])
        self.assertTrue(all(w["check"] in unevaluated for w in warnings), [w["check"] for w in warnings])
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_an_operation_guard_is_held_when_unlisted_and_cleared_by_a_retry_or_a_gone_pool(self):
        # GKE retains operation records for a limited time: a listing that answered without the
        # guard's operation holds the guard, its candidate standing as it was and an Info line saying
        # so; a later clean operation of the same type on the same target clears it even then, and so
        # does a cluster record that no longer names the target pool.
        ops = copy.deepcopy(OPERATIONS)
        errored = next(o for o in ops if o.get("error"))
        bad = next(o for o in ops if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/idle-batch-pool" in o["targetLink"])
        bad["error"], bad["statusMessage"] = errored["error"], errored["statusMessage"]
        obj = f"Operation/{bad['name']}"
        guard = ur.guard_id(SEEDED, ur.ENTRY_OPERATION, obj)
        self.collect(FakeFleet(operations=ops))
        without = [o for o in ops if o["name"] != bad["name"]]
        unlisted, _ = self.collect(FakeFleet(operations=without), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(unlisted["operation_guards"], {"refreshed": [], "cleared": [], "held": [], "unlisted": [{"id": guard, "reason": ur.OPERATION_GUARD_UNLISTED_FORMAT.format(name=bad["name"])}]})
        self.assertEqual([(g["reviews"], g["last_seen"]) for g in unlisted["guards"] if g["id"] == guard], [(1, "2026-10-15T18:00:00Z")])
        manifest = json.loads(Path(unlisted["manifest_path"]).read_text())
        [held] = [c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == obj]
        self.assertEqual((held["check"], held["severity"], held["persistence"]), (ur.CHECK_OPERATION_FAILED, ur.MANIFEST_SEVERITY_MINOR, ["persists since 2026-10-08T18:00:00Z, 1 review(s)"]))
        report = ur.render_report(unlisted)
        self.assertIn(ur.OPERATION_GUARDS_SUMMARY_FORMAT.format(refreshed=0, held=0, unlisted=1), report)
        self.assertIn("- " + ur.OPERATION_GUARD_HELD_FORMAT.format(id=guard, reason=ur.OPERATION_GUARD_UNLISTED_FORMAT.format(name=bad["name"])), report)
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
        retry = {**copy.deepcopy(bad), "name": "operation-retry", "startTime": "2026-10-20T03:00:00Z", "endTime": "2026-10-20T03:12:00Z"}
        retry.pop("error", None)
        retry.pop("statusMessage", None)
        # A clean operation of the same type on another cluster's pool of the same name is no retry
        # of this one: the guard stays held.
        elsewhere = {**retry, "name": "operation-elsewhere", "targetLink": retry["targetLink"].replace("/clusters/seeded-a/", "/clusters/gemma-gpu-upgraded/")}
        self.assertNotEqual(elsewhere["targetLink"], retry["targetLink"])
        other_cluster, _ = self.collect(FakeFleet(operations=without + [elsewhere]), now=datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc))
        self.assertEqual((other_cluster["operation_guards"]["cleared"], [u["id"] for u in other_cluster["operation_guards"]["unlisted"]]), ([], [guard]))
        self.assertIn(guard, {g["id"] for g in other_cluster["guards"]})
        # A later clean operation of the same type on the same target clears it, the failed one
        # still unlisted.
        cleared, _ = self.collect(FakeFleet(operations=without + [retry]), now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc))
        self.assertEqual([(item["id"], item["reason"]) for item in cleared["operation_guards"]["cleared"]], [(guard, ur.OPERATION_GUARD_RETRIED_FORMAT.format(type="UPGRADE_NODES", target="idle-batch-pool", name="operation-retry", end="2026-10-20T03:12:00Z"))])
        self.assertNotIn(guard, {g["id"] for g in cleared["guards"]})
        manifest = json.loads(Path(cleared["manifest_path"]).read_text())
        self.assertEqual([c for e in manifest["clusters"] for c in e.get("candidates") or [] if c["object"] == obj], [])

    def test_an_operation_guard_clears_when_its_target_pool_is_gone(self):
        ops = copy.deepcopy(OPERATIONS)
        errored = next(o for o in ops if o.get("error"))
        bad = next(o for o in ops if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/idle-batch-pool" in o["targetLink"])
        bad["error"], bad["statusMessage"] = errored["error"], errored["statusMessage"]
        obj = f"Operation/{bad['name']}"
        guard = ur.guard_id(SEEDED, ur.ENTRY_OPERATION, obj)
        self.collect(FakeFleet(operations=ops))
        shrunk = cluster_doc("seeded-a")
        shrunk["nodePools"] = [p for p in shrunk["nodePools"] if p["name"] != "idle-batch-pool"]
        gone, _ = self.collect(FakeFleet(clusters=[shrunk, cluster_doc("gemma-gpu-upgraded")], operations=ops), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual([(item["id"], item["reason"]) for item in gone["operation_guards"]["cleared"]], [(guard, ur.OPERATION_GUARD_POOL_GONE_FORMAT.format(target="idle-batch-pool"))])
        self.assertNotIn(guard, {g["id"] for g in gone["guards"]})
        manifest = json.loads(Path(gone["manifest_path"]).read_text())
        self.assertEqual([c for e in manifest["clusters"] for c in e.get("candidates") or [] if c["object"] == obj], [])
        self.assertIn("- " + ur.OPERATION_GUARD_CLEARED_FORMAT.format(id=guard, reason=ur.OPERATION_GUARD_POOL_GONE_FORMAT.format(target="idle-batch-pool")), ur.render_report(gone))

    def test_a_regraded_guard_is_held_under_its_latest_check(self):
        # Tuesday's after-upgrade wake files a displaced replica under `upgrade-broke-workload`; Sunday
        # re-grades it (pinned now to a pool no operation in the window touched: medium, a Warning
        # under `upgrade-symptom-tentative`), the guard's check moves and the old id leaves
        # `collector_flagged_ids`; the next Sunday's re-check emits the held candidate under the
        # guard's latest check with Tuesday's first-seen date and three reviews.
        self.collect()
        tuesday_op = copy.deepcopy(next(o for o in ops_for("seeded-a") if "pinned-inference-pool" in o["targetLink"]))
        tuesday_op.update(name="operation-tuesday", startTime="2026-10-13T16:30:00Z", endTime="2026-10-13T17:00:00Z")
        displaced = pod("web-5d8f9c7b6-abcde", namespace="seeded-capacity", scheduled_message="0/4 nodes are available: 1 Insufficient cpu, 3 node(s) didn't match Pod's node affinity/selector.", node_selector={"cloud.google.com/gke-nodepool": "pinned-inference-pool"})
        displaced["metadata"]["creationTimestamp"] = "2026-10-13T16:40:00Z"
        web = "seeded-capacity/Pod/web-5d8f9c7b6-abcde"
        guard = ur.guard_id(SEEDED, 2, web)
        old_identity = {"check": ur.CHECK_BROKE_WORKLOAD, "cluster": SEEDED, "namespace": "seeded-capacity", "object": "Pod/web-5d8f9c7b6-abcde"}
        new_identity = {**old_identity, "check": ur.CHECK_SYMPTOM_TENTATIVE}
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [displaced]}}):
            tuesday, _ = self.collect(FakeFleet(operations=OPERATIONS + [tuesday_op]), now=datetime(2026, 10, 13, 17, 30, tzinfo=timezone.utc), full=False, after_upgrade=True)
        self.assertEqual([(g["check"], g["reviews"], g["first_seen"]) for g in tuesday["guards"] if g["id"] == guard], [(ur.CHECK_BROKE_WORKLOAD, 1, "2026-10-13T17:30:00Z")])
        repinned = copy.deepcopy(displaced)
        repinned["spec"]["nodeSelector"] = {"cloud.google.com/gke-nodepool": "idle-batch-pool"}
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "pods": READS["seeded-a"]["pods"] + [repinned]}}):
            sunday, _ = self.collect(FakeFleet(operations=OPERATIONS + [tuesday_op]), now=datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc))
            manifest = json.loads(Path(sunday["manifest_path"]).read_text())
            [regraded] = [c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == old_identity["object"]]
            self.assertEqual((regraded["check"], regraded["severity"], regraded["persistence"]), (ur.CHECK_SYMPTOM_TENTATIVE, ur.MANIFEST_SEVERITY_MINOR, ["persists since 2026-10-13T17:30:00Z, 2 review(s)"]))
            self.assertEqual([(g["check"], g["reviews"]) for g in sunday["guards"] if g["id"] == guard], [(ur.CHECK_SYMPTOM_TENTATIVE, 2)])
            flagged = self.audit_report.collector_flagged_ids(manifest)
            self.assertNotIn(self.audit_report.published_id(old_identity), flagged)
            self.assertIn(self.audit_report.published_id(new_identity), flagged)
            self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
            next_sunday, _ = self.collect(FakeFleet(operations=OPERATIONS + [tuesday_op]), now=datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc))
        self.assertIn(SEEDED, [u["cluster"] for u in next_sunday["unchanged"]])
        manifest = json.loads(Path(next_sunday["manifest_path"]).read_text())
        [held] = [c for c in next(e for e in manifest["clusters"] if e["name"] == SEEDED)["candidates"] if c["object"] == old_identity["object"]]
        self.assertEqual((held["check"], held["severity"], held["persistence"]), (ur.CHECK_SYMPTOM_TENTATIVE, ur.MANIFEST_SEVERITY_MINOR, ["persists since 2026-10-13T17:30:00Z, 3 review(s)"]))
        self.assertEqual(self.audit_report.published_id(held), self.audit_report.published_id(new_identity))
        self.assertNotIn(self.audit_report.published_id(old_identity), self.audit_report.collector_flagged_ids(manifest))
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_two_guards_on_one_object_follow_one_check_across_a_regrade(self):
        # Sunday A files inventory-api's entry-3 outage and its unclassified pod symptom as one Error
        # under `upgrade-broke-workload`, two guards on one object. Sunday B reviews the cluster again
        # with the pool operation outside the window: no entry-3 row, the keep rule re-submits that
        # guard, and the pod symptom, recorded now, re-grades the object; every guard of the object
        # takes the new check, so the old identity is resolved once. Sunday C, unchanged, re-checks
        # both and emits one held candidate and one `still_flagged` entry, never a second finding
        # under the identity B resolved.
        workloads = copy.deepcopy(READS["seeded-a"]["workloads"])
        inventory = next(w for w in workloads if w["metadata"]["name"] == "inventory-api")
        inventory["spec"]["replicas"] = 2
        for cond in inventory["status"]["conditions"]:
            if cond["type"] == "Available":
                cond["lastTransitionTime"] = "2026-10-07T04:08:00Z"
        pods = copy.deepcopy(READS["seeded-a"]["pods"])
        next(p for p in pods if p["metadata"]["name"].startswith("inventory-api-"))["spec"]["nodeName"] = "gke-seeded-a-default-pool-62ac8ee0-d595"
        obj = "seeded-stall/Deployment/inventory-api"
        spread, unclassified = ur.guard_id(SEEDED, 3, obj), ur.guard_id(SEEDED, None, obj)
        later = copy.deepcopy(next(o for o in ops_for("seeded-a") if "idle-batch-pool" in o["targetLink"]))
        later.update(name="operation-later", startTime="2026-10-15T16:00:00Z", endTime="2026-10-15T16:05:00Z")
        old_identity = {"check": ur.CHECK_BROKE_WORKLOAD, "cluster": SEEDED, "namespace": "seeded-stall", "object": "Deployment/inventory-api"}
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "workloads": workloads, "pods": pods}}):
            first, _ = self.collect()
            self.assertEqual({g["id"]: g["check"] for g in first["guards"] if g["object"] == obj}, {spread: ur.CHECK_BROKE_WORKLOAD, unclassified: ur.CHECK_BROKE_WORKLOAD})
            second, _ = self.collect(FakeFleet(operations=OPERATIONS + [later]), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
            [review] = [r for r in second["reviews"] if r["cluster"] == SEEDED]
            self.assertEqual([s for s in review["what_failed"] if s["category"] == ur.CATEGORY_UNAVAILABLE], [])
            self.assertIn(obj, review["unavailable_workloads"])
            checks = {g["id"]: g["check"] for g in second["guards"] if g["object"] == obj}
            self.assertEqual(set(checks), {spread, unclassified})
            [moved] = set(checks.values())
            self.assertNotEqual(moved, ur.CHECK_BROKE_WORKLOAD)
            manifest = json.loads(Path(second["manifest_path"]).read_text())
            seeded = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
            self.assertEqual([c["check"] for c in seeded["candidates"] if c["object"] == "Deployment/inventory-api"], [moved])
            self.assertNotIn(self.audit_report.published_id(old_identity), self.audit_report.collector_flagged_ids(manifest))
            self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
            third, _ = self.collect(FakeFleet(operations=OPERATIONS + [later]), now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc))
        self.assertIn(SEEDED, [u["cluster"] for u in third["unchanged"]])
        [recheck] = [r for r in third["rechecks"] if r["cluster"] == SEEDED]
        self.assertTrue({spread, unclassified} <= set(recheck["refreshed"]))
        manifest = json.loads(Path(third["manifest_path"]).read_text())
        seeded = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        [held] = [c for c in seeded["candidates"] if c["object"] == "Deployment/inventory-api"]
        self.assertEqual((held["check"], len(held["persistence"])), (moved, 2))
        [flagged] = [h for h in manifest["still_flagged"] if h["object"] == "Deployment/inventory-api"]
        self.assertEqual((flagged["check"], sorted(flagged["guards"])), (moved, sorted([spread, unclassified])))
        self.assertNotIn(self.audit_report.published_id(old_identity), self.audit_report.collector_flagged_ids(manifest))
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
        # The belt and braces: were two checks ever to reach the held path, one object is one candidate.
        disagreeing = [{**g, "check": ur.CHECK_BROKE_WORKLOAD if g["id"] == spread else moved, "last_seen": "x"} for g in third["guards"] if g["object"] == obj]
        candidates, flagged = ur._held_candidates(SEEDED, disagreeing, set(), {}, "x")
        self.assertEqual((len(candidates), len(flagged), candidates[0]["object"]), (1, 1, "Deployment/inventory-api"))

    def test_an_unread_declared_project_is_held_and_is_the_manifests_limitation(self):
        # `--scope-unread`: the project is not swept, its known clusters are "Reads that failed"
        # lines and `unreachable` manifest entries the document skips, its entry and guards are held,
        # the fleet record keeps it, and the tool's unread note is the manifest's top-level limitation.
        self.collect(scope_projects=f"{PROJECT},other-project")
        other = "other-project/us-central1-a/elsewhere"
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        ledger["clusters"][other] = {"control_plane": "1.0.0", "node_pools": {}, "channel": "", "first_seen": "2026-10-01T00:00:00Z", "last_run": "2026-10-01T00:00:00Z"}
        ur.write_json_atomically(self.home / ur.LEDGER_FILENAME, ledger)
        guards = ur.load_json(self.home / ur.GUARDS_FILENAME, {})
        guard = ur.guard_id(other, 7, "apps/Deployment/x")
        guards["guards"].append({**guards["guards"][0], "id": guard, "cluster": other, "entry": 7, "object": "apps/Deployment/x"})
        ur.write_json_atomically(self.home / ur.GUARDS_FILENAME, guards)
        result, fleet = self.collect(scope_projects=PROJECT, scope_unread="other-project=denied", now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertFalse(any("other-project" in " ".join(c) for c in fleet.calls))
        self.assertEqual((result["fleet"], result["removed_clusters"], result["scope_unread"]), ([PROJECT, "other-project"], [], [("other-project", "denied")]))
        line = ur.UNREAD_PROJECT_TEXT.format(cluster=other, project="other-project", outcome="denied")
        self.assertIn(line, result["failed_reads"])
        self.assertIn(other, ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"])
        self.assertIn(guard, {g["id"] for g in result["guards"]})
        self.assertEqual([i for i in result["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD and i["cluster"] == other], [])
        self.assertIn(line, ur.render_report(result).split(ur.INFO_FAILED_READS)[1])
        self.assertIn(ur.INFO_SCOPE_NOTE.format(note=ur.fleet_scope_args.unread_note([("other-project", "denied")])), ur.render_report(result))
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        entry = next(c for c in manifest["clusters"] if c["name"] == other)
        self.assertEqual((entry["outcome"], entry["project"]), (ur.MANIFEST_OUTCOME_UNREACHABLE, "other-project"))
        self.assertIn("declared scope but was not read (denied)", entry["error"])
        self.assertEqual(manifest["limitations"], ur.fleet_scope_args.unread_note([("other-project", "denied")]))
        self.assertIn("other-project (denied)", manifest["limitations"])
        self.assertTrue(manifest["partial"])
        self.assertEqual([c["error"] for c in manifest["clusters"] if c["name"] == ur.UNENUMERATED_PROJECTS_TARGET], [manifest["limitations"]])
        document = self.document_from(manifest)
        self.assertEqual([s["cluster"] for s in document["scope"]["skipped"]], [other, ur.UNENUMERATED_PROJECTS_TARGET])
        self.audit_report.cross_check_manifest(document, manifest)

    def test_a_full_run_handed_the_unresolved_row_prunes_no_project(self):
        # The fleet_scope tool's `declared-scope=unresolved` row names no project (the render answered, or
        # a folder, organisation or selector lookup is unresolved), so the handed set is not the fleet
        # (`sweep_is_declared` is false). A first run with the row still records the project it read, so
        # its reviews write guards. A later full run with the row prunes no project: a ledger project
        # outside the handed set is held with its guards, its known clusters "Reads that failed" lines
        # and `unreachable` manifest entries the document skips, the fleet stays the ledger's set plus
        # the projects read, and the row's note is the manifest's limitation. The next resolved scope
        # prunes against the handed set as before.
        row = f"{ur.fleet_scope_args.UNRESOLVED_SCOPE_ROW}=unresolved"
        first, fleet = self.collect(scope_projects=PROJECT, scope_unread=row)
        self.assertFalse(ur.declared_scope.sweep_is_declared())
        self.assertFalse(any("projects list" in " ".join(c) for c in fleet.calls))
        self.assertEqual((first["fleet"], first["removed_clusters"], first["scope_unread"], first["outside_fleet"], first["held_clusters"], first["scoped"]), ([PROJECT], [], [], [], {}, False))
        self.assertTrue(first["guards"] and all(g["cluster"] in (GEMMA, SEEDED) for g in first["guards"]))
        manifest = json.loads(Path(first["manifest_path"]).read_text())
        self.assertEqual((manifest["limitations"], manifest["partial"], manifest["fleet"]), (ur.fleet_scope_args.UNRESOLVED_SCOPE_NOTE, True, [PROJECT]))
        # No known cluster carries the gap, so the siblings' unenumerated-projects target does: a
        # `gate-failed` entry the document skips, which is what makes `finish` read the run as partial.
        [gap] = [c for c in manifest["clusters"] if c["name"] == ur.UNENUMERATED_PROJECTS_TARGET]
        self.assertEqual((gap["outcome"], gap["error"]), (ur.MANIFEST_OUTCOME_GATE_FAILED, ur.fleet_scope_args.UNRESOLVED_SCOPE_NOTE))
        document = self.document_from(manifest)
        self.assertIn(ur.UNENUMERATED_PROJECTS_TARGET, [s["cluster"] for s in document["scope"]["skipped"]])
        self.assertTrue(self.audit_report.coverage_gaps(document))
        self.audit_report.cross_check_manifest(document, manifest)
        # The reconcile resolves the scope to two projects; the second gains a cluster and a guard.
        self.collect(scope_projects=f"{PROJECT},other-project", now=datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc))
        other = "other-project/us-central1-a/elsewhere"
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        ledger["clusters"][other] = {"control_plane": "1.0.0", "node_pools": {}, "channel": "", "first_seen": "2026-10-01T00:00:00Z", "last_run": "2026-10-01T00:00:00Z"}
        ur.write_json_atomically(self.home / ur.LEDGER_FILENAME, ledger)
        guards = ur.load_json(self.home / ur.GUARDS_FILENAME, {})
        guard = ur.guard_id(other, 7, "apps/Deployment/x")
        guards["guards"].append({**guards["guards"][0], "id": guard, "cluster": other, "entry": 7, "object": "apps/Deployment/x"})
        ur.write_json_atomically(self.home / ur.GUARDS_FILENAME, guards)
        # A Sunday on which the snapshot does not answer: the tool hands the host project and the row.
        held, fleet = self.collect(scope_projects=PROJECT, scope_unread=row, now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertFalse(any("other-project" in " ".join(c) for c in fleet.calls))
        line = ur.SCOPE_UNRESOLVED_HOLD_TEXT.format(cluster=other, project="other-project", row=ur.fleet_scope_args.UNRESOLVED_SCOPE_ROW, outcome="unresolved")
        self.assertEqual((held["fleet"], held["removed_clusters"], held["scope_unread"], held["scoped"], held["held_clusters"]), ([PROJECT, "other-project"], [], [], False, {other: line.split(": ", 1)[1]}))
        self.assertIn(line, held["failed_reads"])
        report = ur.render_report(held)
        self.assertIn(line, report.split(ur.INFO_FAILED_READS)[1])
        self.assertNotIn(ur.INFO_REMOVED, report)
        self.assertIn(ur.INFO_SCOPE_NOTE.format(note=ur.fleet_scope_args.UNRESOLVED_SCOPE_NOTE), report)
        self.assertIn(other, ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"])
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})[ur.LEDGER_PROJECTS_KEY], [PROJECT, "other-project"])
        self.assertIn(guard, {g["id"] for g in held["guards"]})
        self.assertEqual([i for i in held["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD and i["cluster"] == other], [])
        manifest = json.loads(Path(held["manifest_path"]).read_text())
        entry = next(c for c in manifest["clusters"] if c["name"] == other)
        self.assertEqual((entry["outcome"], entry["project"], "candidates" in entry), (ur.MANIFEST_OUTCOME_UNREACHABLE, "other-project", False))
        self.assertIn("declared scope is not resolved yet (declared-scope=unresolved)", entry["error"])
        self.assertEqual((manifest["limitations"], manifest["partial"], manifest["fleet"]), (ur.fleet_scope_args.UNRESOLVED_SCOPE_NOTE, True, [PROJECT, "other-project"]))
        document = self.document_from(manifest)
        self.assertIn(other, [s["cluster"] for s in document["scope"]["skipped"]])
        self.audit_report.cross_check_manifest(document, manifest)
        # The next full run handed the resolved set prunes against it as before.
        narrowed, _ = self.collect(scope_projects=PROJECT, now=datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc))
        self.assertEqual((narrowed["fleet"], narrowed["removed_clusters"], narrowed["held_clusters"]), ([PROJECT], [other], {}))
        self.assertNotIn(guard, {g["id"] for g in narrowed["guards"]})

    def test_a_full_run_on_an_undeclared_install_holds_what_an_incomplete_listing_omits(self):
        # On an install that declares no scope the listing is the fleet, so a `projects list` that
        # fails, or answers without the active project (filtered), is a set that is not the fleet: a
        # full run sweeps what it has, prunes no project, holds every ledger project outside the
        # listing with its guards, carries the listing's note as the manifest's gap and the report's
        # Scope line, and records the fleet as the ledger's set plus what it read. A complete listing
        # that no longer names a project prunes it, as before.
        both, _ = self.collect(FakeFleet(projects_listed=[PROJECT, "other-project"]), full=True, scope_projects=None, project=None)
        self.assertEqual((both["fleet"], both["scoped"], both.get("scope_note")), ([PROJECT, "other-project"], False, None))
        other = "other-project/us-central1-a/elsewhere"
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        ledger["clusters"][other] = {"control_plane": "1.0.0", "node_pools": {}, "channel": "", "first_seen": "2026-10-01T00:00:00Z", "last_run": "2026-10-01T00:00:00Z"}
        ur.write_json_atomically(self.home / ur.LEDGER_FILENAME, ledger)
        guards = ur.load_json(self.home / ur.GUARDS_FILENAME, {})
        guard = ur.guard_id(other, 7, "apps/Deployment/x")
        guards["guards"].append({**guards["guards"][0], "id": guard, "cluster": other, "entry": 7, "object": "apps/Deployment/x"})
        ur.write_json_atomically(self.home / ur.GUARDS_FILENAME, guards)
        failed, _ = self.collect(FakeFleet(projects_list_rc=1), full=True, scope_projects=None, project=None, now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertTrue(failed["scope_note"].startswith("gcloud projects list rc=1: PERMISSION_DENIED"), failed["scope_note"])
        self.assertEqual((failed["projects"], failed["fleet"], failed["removed_clusters"], failed["scoped"]), ([PROJECT], [PROJECT, "other-project"], [], False))
        line = ur.LISTING_INCOMPLETE_HOLD_TEXT.format(cluster=other, project="other-project")
        self.assertEqual(failed["held_clusters"], {other: line.split(": ", 1)[1]})
        self.assertIn(line, failed["failed_reads"])
        self.assertIn(guard, {g["id"] for g in failed["guards"]})
        self.assertIn(other, ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"])
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})[ur.LEDGER_PROJECTS_KEY], [PROJECT, "other-project"])
        self.assertEqual([i for i in failed["sections"]["warnings"] if i["kind"] == ur.INCIDENT_STALE_GUARD and i["cluster"] == other], [])
        report = ur.render_report(failed)
        self.assertIn(ur.INFO_SCOPE_NOTE.format(note=failed["scope_note"]), report)
        self.assertNotIn(ur.INFO_REMOVED, report)
        manifest = json.loads(Path(failed["manifest_path"]).read_text())
        self.assertEqual((manifest["partial"], manifest["limitations"], manifest["fleet"]), (True, failed["scope_note"], [PROJECT, "other-project"]))
        entry = next(c for c in manifest["clusters"] if c["name"] == other)
        self.assertEqual((entry["outcome"], entry["error"]), (ur.MANIFEST_OUTCOME_UNREACHABLE, line.split(": ", 1)[1]))
        [gap] = [c for c in manifest["clusters"] if c["name"] == ur.UNENUMERATED_PROJECTS_TARGET]
        self.assertEqual((gap["outcome"], gap["error"]), (ur.MANIFEST_OUTCOME_GATE_FAILED, failed["scope_note"]))
        document = self.document_from(manifest)
        self.assertEqual([s["cluster"] for s in document["scope"]["skipped"]], [other, ur.UNENUMERATED_PROJECTS_TARGET])
        self.audit_report.cross_check_manifest(document, manifest)
        # A listing that answers without the active project is filtered: the same hold, the third
        # project it did name swept and joined.
        filtered, _ = self.collect(FakeFleet(projects_listed=["third-project"]), full=True, scope_projects=None, project=None, now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(filtered["scope_note"], ur.LISTING_FILTERED_NOTE_FORMAT.format(project=PROJECT, count=1))
        self.assertEqual((filtered["projects"], filtered["fleet"], filtered["removed_clusters"], list(filtered["held_clusters"])), ([PROJECT, "third-project"], [PROJECT, "other-project", "third-project"], [], [other]))
        # No active gcloud project: the listing cannot be checked against one, so it holds whatever it
        # names; an empty one is the failed case, and nothing is swept or pruned.
        unchecked, _ = self.collect(FakeFleet(projects_listed=["third-project"], active_project=""), full=True, scope_projects=None, project=None, now=datetime(2026, 10, 16, 19, 0, tzinfo=timezone.utc))
        self.assertEqual((unchecked["scope_note"], unchecked["projects"], unchecked["removed_clusters"], sorted(unchecked["held_clusters"])), (ur.LISTING_UNCHECKED_NOTE_FORMAT.format(count=1), ["third-project"], [], sorted([GEMMA, SEEDED, other])))
        self.assertIn(guard, {g["id"] for g in unchecked["guards"]})
        empty, _ = self.collect(FakeFleet(projects_listed=[], active_project=""), full=True, scope_projects=None, project=None, now=datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc))
        self.assertEqual((empty["scope_note"], empty["projects"], empty["fleet"], empty["removed_clusters"], sorted(empty["held_clusters"])), (ur.LISTING_EMPTY_NOTE_TEXT, [], [PROJECT, "other-project", "third-project"], [], sorted([GEMMA, SEEDED, other])))
        self.assertIn(guard, {g["id"] for g in empty["guards"]})
        self.assertEqual(sorted(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"]), sorted([GEMMA, SEEDED, other]))
        self.assertTrue(any(line.startswith("project discovery named no project") for line in empty["failed_reads"]), empty["failed_reads"])
        manifest = json.loads(Path(empty["manifest_path"]).read_text())
        self.assertEqual((manifest["partial"], manifest["limitations"], [c["name"] for c in manifest["clusters"] if c["outcome"] == ur.MANIFEST_OUTCOME_GATE_FAILED]), (True, ur.LISTING_EMPTY_NOTE_TEXT, [ur.UNENUMERATED_PROJECTS_TARGET]))
        # A complete listing that no longer names the project prunes it, as before.
        complete, _ = self.collect(FakeFleet(projects_listed=[PROJECT]), full=True, scope_projects=None, project=None, now=datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc))
        self.assertEqual((complete.get("scope_note"), complete["fleet"], complete["removed_clusters"]), (None, [PROJECT], [other]))
        self.assertNotIn(guard, {g["id"] for g in complete["guards"]})

    def test_a_review_that_raises_keeps_the_listing_backed_operation_check_and_guard(self):
        # `_safe_review`'s fallback keeps what the listing established before any read, as `review_cluster`
        # records it first: a failed operation on a cluster whose review raised is `collected` on the
        # operation check with the five kubectl checks unevaluated, its Error the entry's `major`
        # candidate, the operation guard written, and the raise the limitation, as the credentials path
        # already is. With the listing failed too, nothing backs the check: gate-failed, no candidate.
        ops = copy.deepcopy(OPERATIONS)
        errored = next(o for o in ops if o.get("error"))
        bad = next(o for o in ops if o["operationType"] == "UPGRADE_NODES" and "/clusters/seeded-a/nodePools/idle-batch-pool" in o["targetLink"])
        bad["error"], bad["statusMessage"] = errored["error"], errored["statusMessage"]
        obj = f"Operation/{bad['name']}"
        guard = ur.guard_id(SEEDED, ur.ENTRY_OPERATION, obj)

        def boom(*args, **kwargs):
            raise RuntimeError("planted")

        with mock.patch.object(ur, "collect_symptoms", boom):
            result, _ = self.collect(FakeFleet(operations=ops))
        seeded = next(r for r in result["reviews"] if r["cluster"] == SEEDED)
        self.assertEqual((seeded["reviewed"], seeded["partial"], seeded["answered"], [c["check"] for c in seeded["commands"]]), (False, list(ur.CORE_READS), [], [ur.CHECK_OPERATION_FAILED]))
        self.assertEqual([g["id"] for g in seeded["guards"]], [guard])
        self.assertTrue(any(e.startswith("review failed: RuntimeError('planted')") for e in seeded["read_errors"]))
        [incident] = [i for i in result["sections"]["errors"] if i["object"] == obj]
        self.assertEqual((incident["kind"], [g["id"] for g in incident["guards"]]), (ur.INCIDENT_OPERATION, [guard]))
        report = ur.render_report(result)
        self.assertNotIn(ur.OPERATION_NO_GUARD_TEXT, report)
        self.assertIn(f"guard `{guard}` first seen", report)
        self.assertIn(f"- {SEEDED}: review failed:", report)
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        entry = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        self.assertEqual((entry["outcome"], [c["check"] for c in entry["commands"]]), (ur.MANIFEST_OUTCOME_COLLECTED, [ur.CHECK_OPERATION_FAILED]))
        self.assertEqual([c["check"] for c in entry["checks_unevaluated"]], [check for check, read in ur.CHECK_READS if read != ur.OPERATIONS_READ])
        self.assertEqual([(c["check"], c["severity"], c["object"]) for c in entry["candidates"]], [(ur.CHECK_OPERATION_FAILED, ur.MANIFEST_SEVERITY_MAJOR, obj)])
        self.assertTrue(entry["limitations"].startswith("reads that failed: review failed: RuntimeError('planted')"), entry["limitations"])
        self.assertTrue(manifest["partial"])
        self.assertIn(guard, {g["id"] for g in result["guards"]})
        document = self.document_from(manifest)
        self.assertIn(SEEDED, [c["name"] for c in document["scope"]["clusters"]])
        self.audit_report.cross_check_manifest(document, manifest)
        with mock.patch.object(ur, "collect_symptoms", boom):
            unread, _ = self.collect(FakeFleet(operations=ops, ops_fail={PROJECT}), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        manifest = json.loads(Path(unread["manifest_path"]).read_text())
        entry = next(e for e in manifest["clusters"] if e["name"] == SEEDED)
        self.assertEqual((entry["outcome"], "candidates" in entry), (ur.MANIFEST_OUTCOME_GATE_FAILED, False))
        self.assertEqual([i for i in unread["sections"]["errors"] if i["cluster"] == SEEDED], [])

    def test_three_guards_on_one_object_are_one_candidate(self):
        # Two event-only guards on inference-server (a volume attach, a webhook rejection) beside its
        # pod-sourced entry-2 guard. The next run re-observes the pod guard, and the event guards
        # through the object still failing: no stale Warning, one candidate, one identity.
        events = READS["seeded-a"]["events"] + [
            event("FailedAttachVolume", "AttachVolume.Attach failed for volume pv-1", name="inference-server-778b78fdb8-zzzzz", namespace="seeded-capacity"),
            event("FailedCreate", 'failed calling webhook "gate.example.io"', kind="ReplicaSet", name="inference-server-778b78fdb8", namespace="seeded-capacity"),
        ]
        with mock.patch.dict(READS, {"seeded-a": {**READS["seeded-a"], "events": events}}):
            first, _ = self.collect()
        self.assertEqual({g["entry"] for g in first["guards"] if g["object"] == INFERENCE and g["kind"] == ur.GUARD_KIND_FAILURE}, {2, 7, 19})
        second, _ = self.collect(now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual([w for w in second["sections"]["warnings"] if w["kind"] == ur.INCIDENT_STALE_GUARD], [])
        [recheck] = [r for r in second["rechecks"] if r["cluster"] == SEEDED]
        self.assertTrue({ur.guard_id(SEEDED, e, INFERENCE) for e in (2, 7, 19)} <= set(recheck["refreshed"]))
        manifest = json.loads(Path(second["manifest_path"]).read_text())
        seeded = next(c for c in manifest["clusters"] if c["name"] == SEEDED)
        # All three guards were filed with the object's Error (`upgrade-broke-workload`): one
        # candidate under that check, the re-observed pod guard and the two held event guards in it.
        [candidate] = [c for c in seeded["candidates"] if c["object"] == "Deployment/inference-server"]
        self.assertEqual((candidate["check"], candidate["namespace"], candidate["entries"], candidate["severity"]), (ur.CHECK_BROKE_WORKLOAD, "seeded-capacity", "2, 7, 19", ur.MANIFEST_SEVERITY_MINOR))
        self.assertEqual(candidate["persistence"], ["persists since 2026-10-08T18:00:00Z, 2 review(s)"] * 3)
        self.assertNotIn("not_recheckable", candidate)
        [held] = [h for h in manifest["still_flagged"] if h["object"] == "Deployment/inference-server"]
        self.assertEqual((held["check"], held["guards"]), (ur.CHECK_BROKE_WORKLOAD, [ur.guard_id(SEEDED, 2, INFERENCE), ur.guard_id(SEEDED, 7, INFERENCE), ur.guard_id(SEEDED, 19, INFERENCE)]))
        document = self.document_from(manifest)
        ids = [self.audit_report.derive_finding_id(f) for f in document["findings"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.audit_report.cross_check_manifest(document, manifest)

    def test_an_api_refusal_naming_another_project_is_a_failed_read_not_an_empty_project(self):
        # With a quota project whose Kubernetes Engine API is off, every `--project X` call fails with
        # a refusal naming the quota project; read as X's own, every project listed empty and a full
        # run pruned the fleet. The refusal is "no cluster here" only when it names this project
        # (`collect.refusal_owner`, the rule fleet_waste and fleet_stockout carry); otherwise it is a
        # failed read, held, for both listings.
        self.collect()
        quota, _ = self.collect(FakeFleet(api_off=[PROJECT], api_off_names="quota-proj"), now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual((quota["removed_clusters"], quota["projects"], quota["fleet"], quota["operations_unread"]), ([], [PROJECT], [PROJECT], [PROJECT]))
        self.assertTrue(any(line.startswith(f"{PROJECT}: clusters list refused and the refusal names another project ('quota-proj')") for line in quota["failed_reads"]), quota["failed_reads"])
        self.assertTrue(any(line.startswith(f"{PROJECT}: operations list refused and the refusal names another project ('quota-proj')") for line in quota["failed_reads"]), quota["failed_reads"])
        self.assertTrue({GEMMA, SEEDED} <= {g["cluster"] for g in quota["guards"]})
        self.assertEqual(sorted(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"]), [GEMMA, SEEDED])
        manifest = json.loads(Path(quota["manifest_path"]).read_text())
        self.assertEqual({c["name"]: c["outcome"] for c in manifest["clusters"]}, {GEMMA: ur.MANIFEST_OUTCOME_GATE_FAILED, SEEDED: ur.MANIFEST_OUTCOME_GATE_FAILED})
        self.assertTrue(manifest["partial"])
        self.assertNotIn(ur.INFO_REMOVED, ur.render_report(quota))
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
        # A refusal naming this project is the empty project it always was: its clusters leave.
        own, _ = self.collect(FakeFleet(api_off=[PROJECT]), now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))
        self.assertEqual((own["removed_clusters"], own["failed_reads"]), ([GEMMA, SEEDED], []))

    def test_the_read_budget_ends_the_reads_and_holds_the_rest_for_the_next_run(self):
        # The run's own deadline under the SOP's 600 s, checked as each review and re-check starts:
        # past it no further cluster is read. What was read is written; a cluster not reached is held
        # (ledger and guards untouched, `gate-failed` with the budget reason, no stale Warning) and,
        # since reviews run in last-reviewed order, read first next run; a re-check the deadline defers
        # holds its guards with their checks unevaluated. With the budget never spent nothing changes.
        # The budget is spent the moment the first review's last read answers: every subprocess
        # consults the clock, so the flag flips on the fleet's side rather than by counting calls.
        state = {"spent": False}
        fleet = FakeFleet()

        def run(argv, **kwargs):
            result = fleet(argv, **kwargs)
            if argv[0] == "kubectl" and argv[2] == ur.KUBECTL_READS[-1][1][2]:
                state["spent"] = True
            return result

        def clock():
            return float(ur.RUN_DEADLINE_S) if state["spent"] else 0.0

        manifest_file = str(self.home / "manifest.json")
        with redirect_stderr(io.StringIO()):
            first = ur.collect(args(manifest_file=manifest_file), run=run, now=NOW, max_workers=1, clock=clock)
        self.assertEqual(([r["cluster"] for r in first["reviews"]], first["deferred"], first["budget_spent"], first["deferred_rechecks"]), ([GEMMA], [SEEDED], True, 0))
        self.assertEqual(sorted(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"]), [GEMMA])
        self.assertEqual({g["cluster"] for g in first["guards"]}, {GEMMA})
        self.assertIn(ur.BUDGET_SPENT_TEXT.format(cluster=SEEDED, seconds=ur.RUN_DEADLINE_S, count=1), first["failed_reads"])
        self.assertEqual([w for w in first["sections"]["warnings"] if w["kind"] == ur.INCIDENT_STALE_GUARD], [])
        report = ur.render_report(first)
        self.assertIn(ur.INFO_BUDGET_SPENT.format(seconds=ur.RUN_DEADLINE_S, reviewed=1, rechecked=0, cut=0, cut_rechecks=0, deferred=1, deferred_rechecks=0), report)
        manifest = json.loads(Path(first["manifest_path"]).read_text())
        entry = next(c for c in manifest["clusters"] if c["name"] == SEEDED)
        self.assertEqual((entry["outcome"], entry["error"], manifest["partial"]), (ur.MANIFEST_OUTCOME_GATE_FAILED, ur.BUDGET_SPENT_REASON.format(seconds=ur.RUN_DEADLINE_S), True))
        document = self.document_from(manifest)
        self.assertIn(SEEDED, [s["cluster"] for s in document["scope"]["skipped"]])
        self.audit_report.cross_check_manifest(document, manifest)
        # The next run reads the held cluster first (never reviewed sorts before GEMMA's last run) and
        # defers GEMMA's re-check: its guards held, their checks unevaluated, the reason on the report.
        state["spent"] = False
        with redirect_stderr(io.StringIO()):
            second = ur.collect(args(manifest_file=manifest_file), run=run, now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc), max_workers=1, clock=clock)
        self.assertEqual(([r["cluster"] for r in second["reviews"]], second["deferred"], second["deferred_rechecks"]), ([SEEDED], [], 1))
        self.assertEqual(sorted(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"]), [GEMMA, SEEDED])
        self.assertTrue({GEMMA, SEEDED} <= {g["cluster"] for g in second["guards"]})
        manifest = json.loads(Path(second["manifest_path"]).read_text())
        gemma = next(c for c in manifest["clusters"] if c["name"] == GEMMA)
        self.assertEqual(gemma["outcome"], ur.MANIFEST_OUTCOME_COLLECTED)
        self.assertTrue(gemma.get("checks_unevaluated"))
        self.assertIn(ur.RECHECK_DEFERRED_TEXT.format(seconds=ur.RUN_DEADLINE_S), gemma["limitations"])
        report = ur.render_report(second)
        self.assertIn(ur.STALE_UNREAD_TEXT.format(errors=ur.RECHECK_DEFERRED_TEXT.format(seconds=ur.RUN_DEADLINE_S)), report)
        held = len([g for g in first["guards"] if g["cluster"] == GEMMA and g.get("entry") != ur.ENTRY_OPERATION])
        self.assertIn(ur.RECHECK_DEFERRED_LINE.format(guards=held), report)
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
        with redirect_stderr(io.StringIO()):
            third = ur.collect(args(manifest_file=manifest_file), run=FakeFleet(), now=datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc), max_workers=1)
        self.assertEqual((third["deferred"], third["deferred_rechecks"], third["budget_spent"]), ([], 0, False))
        self.assertNotIn("Read budget spent", ur.render_report(third))

    def test_a_stalled_read_is_cut_at_the_time_left_and_the_run_ends_under_the_limit(self):
        # Every subprocess gets the smaller of its own timeout and the time left under the deadline,
        # and one attempted past it fails at once with the budget as its error: a cluster whose API
        # server hangs costs the run the time left, never a timeout per read, so the run ends under
        # the terminal's limit with the manifest written. The cut cluster is a partial review (here
        # nothing answered: the credentials-failed shape, `collected` on the operation check alone,
        # `partial_read` recorded); the next cluster is held for the next run.
        # The run reads the fake's clock, which the stall advances by the time the read was given
        # rather than sleeping: the cut is asserted on that clock, not on a second of wall time.
        fleet = FakeFleet(stall={("gemma-gpu-upgraded", "pods"): 30.0})
        manifest_file = str(self.home / "manifest.json")
        with mock.patch.object(ur, "RUN_DEADLINE_S", 1), redirect_stderr(io.StringIO()):
            result = ur.collect(args(manifest_file=manifest_file), run=fleet, now=NOW, max_workers=1, clock=fleet.clock)
        [(_, _, given)] = fleet.timeouts
        self.assertEqual((given, fleet.now), (1.0, 1.0))
        self.assertEqual(len([c for c in fleet.calls if c[0] == "kubectl"]), 1)
        gemma = next(r for r in result["reviews"] if r["cluster"] == GEMMA)
        self.assertEqual((gemma["reviewed"], gemma["answered"], gemma["partial"]), (False, [], list(ur.CORE_READS)))
        # The read the deadline fell inside carries the budget as its error, the hang's own text after
        # it, so the operator reads a spent budget rather than a hung API server.
        self.assertTrue(any(e.startswith("pods: kubectl get pods rc=124: " + ur.BUDGET_CUT_TEXT.format(seconds=1, given=1) + "; timed out") for e in gemma["read_errors"]), gemma["read_errors"])
        self.assertTrue(any(e.startswith("nodes: kubectl get nodes rc=124: " + ur.BUDGET_READ_TEXT.format(seconds=1)) for e in gemma["read_errors"]), gemma["read_errors"])
        self.assertEqual((result["deferred"], result["budget_spent"]), ([SEEDED], True))
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        entry = next(c for c in manifest["clusters"] if c["name"] == GEMMA)
        self.assertEqual((entry["outcome"], [c["check"] for c in entry["commands"]], len(entry["checks_unevaluated"])), (ur.MANIFEST_OUTCOME_COLLECTED, [ur.CHECK_OPERATION_FAILED], 5))
        self.assertIn(ur.BUDGET_READ_TEXT.format(seconds=1), entry["limitations"])
        self.assertEqual(next(c for c in manifest["clusters"] if c["name"] == SEEDED)["outcome"], ur.MANIFEST_OUTCOME_GATE_FAILED)
        self.assertTrue(manifest["partial"])
        self.assertEqual(ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"][GEMMA]["partial_read"], "2026-10-08T18:00:00Z")
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_a_cut_last_read_counts_the_review_and_the_recheck_as_cut(self):
        # When the deadline falls inside the last read a review or a re-check makes, no later read is
        # refused outright; the cut read itself carries the budget's prefix, so the review (and on the
        # next run the re-check) is counted as cut mid-read and its limitation names the budget.
        last = ur.KUBECTL_READS[-1][0]
        fleet = FakeFleet(clusters=[cluster_doc("gemma-gpu-upgraded")], stall={("gemma-gpu-upgraded", last): 30.0})
        with mock.patch.object(ur, "RUN_DEADLINE_S", 1), redirect_stderr(io.StringIO()):
            result = ur.collect(args(manifest_file=str(self.home / "manifest.json")), run=fleet, now=NOW, max_workers=1, clock=fleet.clock)
        [review] = result["reviews"]
        # The last read is an Info-only one (endpoint slices), so the review stays complete on its core
        # reads; the cut is still counted and named under "Reads that failed".
        self.assertEqual((result["budget_spent"], result["budget"]["cut"], result["budget"]["cut_rechecks"], review["partial"]), (True, 1, 0, []))
        self.assertEqual([e for e in review["read_errors"] if ur.BUDGET_PREFIX_FORMAT.format(seconds=1) in e], [f"{last}: kubectl get {ur.KUBECTL_READS[-1][1][2]} rc=124: " + ur.BUDGET_CUT_TEXT.format(seconds=1, given=1) + "; timed out"])
        self.assertTrue(any(ur.BUDGET_CUT_TEXT.format(seconds=1, given=1) in line for line in result["failed_reads"]), result["failed_reads"])
        # The next run re-checks the cluster for its guards; the re-check's one read cut the same way
        # counts as a re-check cut mid-read, not as a hung read.
        fleet = FakeFleet(clusters=[cluster_doc("gemma-gpu-upgraded")], stall={("gemma-gpu-upgraded", "pods"): 30.0})
        with mock.patch.object(ur, "RUN_DEADLINE_S", 1), redirect_stderr(io.StringIO()):
            second = ur.collect(args(manifest_file=str(self.home / "manifest.json")), run=fleet, now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc), max_workers=1, clock=fleet.clock)
        [recheck] = second["rechecks"]
        self.assertEqual((second["budget_spent"], second["budget"]["cut"], second["budget"]["cut_rechecks"], recheck.get("deferred", False)), (True, 0, 1, False))
        self.assertTrue(any(ur.BUDGET_CUT_TEXT.format(seconds=1, given=1) in e for e in recheck["errors"]), recheck["errors"])
        self.assertIn(ur.INFO_BUDGET_SPENT.format(seconds=1, reviewed=0, rechecked=1, cut=0, cut_rechecks=1, deferred=0, deferred_rechecks=0), ur.render_report(second))

    def test_a_budget_spent_inside_the_last_review_is_said_on_the_report(self):
        # With nothing queued behind it, a read cut inside the last review deferred nothing, yet the
        # budget was spent: the flag comes from the cut reads and the clock, the budget dict counts
        # the cut review, and the Info line is printed.
        fleet = FakeFleet(clusters=[cluster_doc("gemma-gpu-upgraded")], stall={("gemma-gpu-upgraded", "pods"): 30.0})
        with mock.patch.object(ur, "RUN_DEADLINE_S", 1), redirect_stderr(io.StringIO()):
            result = ur.collect(args(manifest_file=str(self.home / "manifest.json")), run=fleet, now=NOW, max_workers=1, clock=fleet.clock)
        self.assertEqual((result["deferred"], result["deferred_rechecks"], result["budget_spent"], result["budget"]["cut"], result["budget"]["cut_rechecks"]), ([], 0, True, 1, 0))
        self.assertIn(ur.INFO_BUDGET_SPENT.format(seconds=1, reviewed=1, rechecked=0, cut=1, cut_rechecks=0, deferred=0, deferred_rechecks=0), ur.render_report(result))
        self.assertTrue(json.loads(Path(result["manifest_path"]).read_text())["partial"])

    def test_api_disabled_project_does_not_make_the_run_partial(self):
        # A project whose Kubernetes Engine API is off is an empty project, not a failed read, as the
        # sibling collectors count it: no gap, the run complete, and on a full run its ledger clusters
        # (from before the API was turned off) leave like any cluster a successful listing no longer names.
        result, _ = self.collect(FakeFleet(api_off=["no-gke-project"]), scope_projects=f"{PROJECT},no-gke-project")
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        self.assertFalse(manifest["partial"])
        self.assertEqual([c["name"] for c in manifest["clusters"]], [GEMMA, SEEDED])
        self.assertEqual(manifest["fleet"], [PROJECT, "no-gke-project"])
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)
        old = "no-gke-project/us-central1-a/retired"
        ledger = ur.load_json(self.home / ur.LEDGER_FILENAME, {})
        ledger["clusters"][old] = {"control_plane": "1.0.0", "node_pools": {}, "channel": "", "first_seen": "2026-10-01T00:00:00Z", "last_run": "2026-10-01T00:00:00Z"}
        ur.write_json_atomically(self.home / ur.LEDGER_FILENAME, ledger)
        guards = ur.load_json(self.home / ur.GUARDS_FILENAME, {})
        guard = ur.guard_id(old, 7, "apps/Deployment/x")
        guards["guards"].append({**guards["guards"][0], "id": guard, "cluster": old, "entry": 7, "object": "apps/Deployment/x"})
        ur.write_json_atomically(self.home / ur.GUARDS_FILENAME, guards)
        later, _ = self.collect(FakeFleet(api_off=["no-gke-project"]), scope_projects=f"{PROJECT},no-gke-project", now=datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual((later["removed_clusters"], later["failed_reads"], later["fleet"]), ([old], [], [PROJECT, "no-gke-project"]))
        self.assertNotIn(old, ur.load_json(self.home / ur.LEDGER_FILENAME, {})["clusters"])
        self.assertNotIn(guard, {g["id"] for g in later["guards"]})
        self.assertIn(f"- {old}: no longer listed by its project", ur.render_report(later))
        manifest = json.loads(Path(later["manifest_path"]).read_text())
        self.assertEqual((manifest["partial"], "limitations" in manifest, old in [c["name"] for c in manifest["clusters"]]), (False, False, False))
        self.audit_report.cross_check_manifest(self.document_from(manifest), manifest)

    def test_dry_run_and_no_flag_write_no_manifest(self):
        result, _ = self.collect(manifest_file=None)
        self.assertNotIn("manifest_path", result)
        result, _ = self.collect(dry_run=True)
        self.assertNotIn("manifest_path", result)
        self.assertFalse((self.home / "manifest.json").exists())

    def test_no_check_is_on_the_major_sweep(self):
        self.assertFalse({check for check, _ in ur.CHECK_READS} & self.audit_report.MAJOR_SWEEP_CHECKS)
        self.assertEqual(ur.AUDIT_ID, "upgrade-retrospective")


class SweepEntryTest(unittest.TestCase):
    def test_collect_fleet_is_the_pinned_pool_entry(self):
        # The sweep-pool test across the collectors pins every module that runs a ThreadPoolExecutor
        # to MAX_WORKERS (the credential proxy's admitted count at the default limit, four) through a
        # `collect_fleet` entry whose `max_workers` defaults to it.
        self.assertEqual(ur.MAX_WORKERS, 4)
        self.assertIs(ur.collect, ur.collect_fleet)
        self.assertEqual(inspect.signature(ur.collect_fleet).parameters["max_workers"].default, ur.MAX_WORKERS)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {ur.HERMES_HOME_ENV: tmp, ur.STORE_HOME_ENV: tmp}):
            with redirect_stderr(io.StringIO()):
                result = ur.collect_fleet(args(), run=FakeFleet(), now=NOW, max_workers=1)
        self.assertEqual([r["cluster"] for r in result["reviews"]], [GEMMA, SEEDED])


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
        # A child that outlives its timeout is rc 124 with str output, whatever the interpreter's
        # start-up costs on the host. The output a timed-out child had flushed is kept and decoded,
        # asserted on the exception `subprocess.run` raises rather than on a race between the
        # child's first write and the timeout.
        result = ur.default_run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
        self.assertEqual((result.rc, result.stdout), (ur.TIMEOUT_RC, ""))
        expired = subprocess.TimeoutExpired(["kubectl"], 1, output=b"partial", stderr=b"late")
        with mock.patch.object(ur.subprocess, "run", side_effect=expired):
            result = ur.default_run(["kubectl"], timeout=1)
        self.assertEqual((result.rc, result.stdout, result.stderr), (ur.TIMEOUT_RC, "partial", "late"))
        self.assertEqual(ur.default_run(["/nonexistent/binary"]).rc, -1)


if __name__ == "__main__":
    unittest.main()
