#!/usr/bin/env python3
"""Unit tests for readiness_rules/: the contract every registered rule meets, the helpers
the rules share, the `finding` helpers, the shape tables' sources, and the rules registered
here, on canned cluster records and kubectl objects."""

import copy
import json
import os
import re
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import fleet_upgrade_report as report  # noqa: E402
import readiness_rules as shared  # noqa: E402
import upgrade_readiness as r  # noqa: E402
import upgrade_shape_tables as tables  # noqa: E402
from fleet_upgrade_report import parse_version  # noqa: E402
from readiness_rules import cluster_status_age, control_plane_upgrade_history, finding, known_issues_matrix, lb_connection_draining, orphan_node_objects, target_patch_valid, zonal_control_plane  # noqa: E402

CATALOGUE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), *([os.pardir] * 5), "docs", "designs", "upgrade-failure-catalogue.md")
CATALOGUE_HEADING_RE = re.compile(r"^### (\d+)\. ", re.MULTILINE)
ZONAL_CASE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), *([os.pardir] * 5), "bench", "tasks", "upgrades-readiness-zonal-control-plane", "task.yaml")
ZONAL_CASE_CELL_OBJECTIVE = "the-declared-lines-quote-the-reports-cell"
# The case grades a declared line of this shape; the verifier lowercases the report and expands
# `{cluster:any}` to a recorded cluster name before matching.
ZONAL_CASE_LINE_FORMAT = "{cluster}: kind Cluster; control plane: zonal; readiness rule: {rule}; graded as: risk; cell: {cell}"
ZONAL_CASE_CLUSTER_PLACEHOLDER = "{cluster:any}"
TARGET = parse_version("1.35.1-gke.1000")
AT = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
LAGGING = "1.34.11-gke.1000"
CURRENT = "1.35.1-gke.1000"
POOL_LABEL = "cloud.google.com/gke-nodepool"
POD_RANGE = "10.0.0.0/14"  # every GKE record carries one; the surge-capacity rule counts its free blocks


def pool(name="default-pool", version="1.33.4-gke.1000", autoscaling=None, **config):
    return {"name": name, "version": version, "parsed": parse_version(version), "config": config, "autoscaling": autoscaling or {}}


def cluster(name="seeded-b", location="us-central1-a", pools=(), **extra):
    record = {"name": name, "location": location, "status": "RUNNING", "currentMasterVersion": LAGGING, "nodePools": list(pools), "locations": [location], "clusterIpv4Cidr": POD_RANGE}
    record.update(extra)
    return record


def read(items=None, error=None, read_errors=None):
    """A `read` dict as fleet_upgrade_report.read_cluster_objects returns it."""
    errors = {kind: None for kind in shared.READ_KINDS}
    errors.update(read_errors or {})
    return {"kubeconfig": "k", "items": items, "error": error, "read_errors": errors}


def node(name, pool_name, cpu="940m", memory="2Gi", labels=None, taints=None, ready=True, unschedulable=False, runtime="containerd://1.7.34"):
    record = {
        "kind": "Node",
        "metadata": {"name": name, "labels": {POOL_LABEL: pool_name, **(labels or {})}},
        "spec": {"taints": taints or []},
        "status": {
            "allocatable": {"cpu": cpu, "memory": memory},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "nodeInfo": {"containerRuntimeVersion": runtime},
        },
    }
    if unschedulable:
        record["spec"]["unschedulable"] = True
    return record


def pod(namespace, name, node_name, cpu="100m", memory="128Mi", owner=None, labels=None, volumes=None, phase="Running", annotations=None):
    meta = {"namespace": namespace, "name": name, "labels": labels or {}}
    if owner is not None:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1]}]
    if annotations:
        meta["annotations"] = annotations
    spec = {"nodeName": node_name, "containers": [{"name": "c", "resources": {"requests": {"cpu": cpu, "memory": memory}}}]}
    if volumes:
        spec["volumes"] = volumes
    return {"kind": "Pod", "metadata": meta, "spec": spec, "status": {"phase": phase}}


def deployment_pod(namespace, deployment, node_name, **kw):
    """A pod as a Deployment's ReplicaSet creates it: owner `<deployment>-<hash>`, hash label."""
    labels = {"app": deployment, "pod-template-hash": "7d9f"}
    return pod(namespace, f"{deployment}-7d9f-x1", node_name, owner=("ReplicaSet", f"{deployment}-7d9f"), labels=labels, **kw)


def template(kind, name, spec, namespace="seeded-shapes", workload_spec=None):
    pod_template = {"metadata": {"labels": {"app": name}}, "spec": spec}
    if kind == "CronJob":
        return {"kind": kind, "metadata": {"namespace": namespace, "name": name}, "spec": {"suspend": True, "jobTemplate": {"spec": {"template": pod_template}}}}
    return {"kind": kind, "metadata": {"namespace": namespace, "name": name}, "spec": {"replicas": 1, "template": pod_template, **(workload_spec or {})}}


def affinity(key, operator, values=None, required=True):
    expr = {"key": key, "operator": operator, **({"values": values} if values else {})}
    if required:
        return {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [expr]}]}}}}
    return {"affinity": {"nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [{"weight": 1, "preference": {"matchExpressions": [expr]}}]}}}


def claim_volume(claim):
    return {"name": "data", "persistentVolumeClaim": {"claimName": claim}}


def socket_volume(path="/run/containerd/containerd.sock"):
    return {"name": "sock", "hostPath": {"path": path, "type": "Socket"}}


class ContractTest(unittest.TestCase):
    """Every registered module meets readiness_rules' contract, so the fold can rely on it."""

    def test_every_registered_module_meets_the_contract(self):
        ids = [m.RULE_ID for m in r.EXTRA_RULES]
        self.assertTrue(ids)
        self.assertEqual(len(ids), len(set(ids)))
        for module in r.EXTRA_RULES:
            self.assertIsInstance(module.RULE_ID, str, module)
            self.assertIsInstance(module.ENTRY, int, module.RULE_ID)
            self.assertIsInstance(module.CAN_BLOCK, bool, module.RULE_ID)
            self.assertTrue(callable(module.evaluate) and callable(module.describe), module.RULE_ID)
            # The inputs are the ones the contract promises, built as the report builds them: a
            # graded member and a context carrying every CONTEXT_KEYS entry, so a rule that
            # reads `context[CONTEXT_CLUSTERS]` or `context[CONTEXT_AUTOPILOT]` by key passes
            # here as it does in a run. The runner refuses every command, so a rule that reads
            # the audit log grades its error and the test runs nothing.
            record = cluster(location="us-central1", pools=[pool("p", CURRENT)])
            member = report.grade_member(record, "p1", CURRENT, report.ServerConfigCache())
            context = report.readiness_context(member, AT, [record], report._pool_records(record, member), parse_version(LAGGING), False)
            context[shared.CONTEXT_RUN_CMD] = lambda cmd, timeout=None, env=None: (1, "", "no runner in the contract test")
            self.assertEqual(set(context), set(shared.CONTEXT_KEYS), module.RULE_ID)
            result = module.evaluate(record, member, read(items=[]), TARGET, context)
            # The result shape the fold accepts: the contract's keys or a subset (a key left
            # out is an empty list to the fold), each present value a list.
            self.assertIsInstance(result, dict, module.RULE_ID)
            self.assertLessEqual(set(result), set(shared.RESULT_KEYS), module.RULE_ID)
            self.assertTrue(all(isinstance(value, list) for value in result.values()), module.RULE_ID)
            # The invariant the fold enforces: a module that declares it cannot block files no
            # blocker on the contract input.
            if not module.CAN_BLOCK:
                self.assertEqual(result.get("blocking", []), [], module.RULE_ID)
        self.assertIn(zonal_control_plane, r.EXTRA_RULES)

    def test_entries_are_catalogue_numbers_and_read_kinds_are_named_once(self):
        # The catalogue's scenario headings are the entry numbers a rule may claim.
        with open(CATALOGUE_PATH, encoding="utf-8") as f:
            catalogue_entries = {int(number) for number in CATALOGUE_HEADING_RE.findall(f.read())}
        self.assertGreaterEqual(len(catalogue_entries), 20)
        for module in r.EXTRA_RULES:
            self.assertIn(module.ENTRY, catalogue_entries, module.RULE_ID)
        self.assertEqual([m.ENTRY for m in r.EXTRA_RULES], sorted(m.ENTRY for m in r.EXTRA_RULES))
        self.assertEqual(shared.READ_KINDS, ("daemonset", "cronjob", "pvc", "networkpolicy", "namespace", "pods", "node", "storageclass", "pv"))
        self.assertEqual(len(shared.READ_KINDS), len(set(shared.READ_KINDS)))
        self.assertEqual(len(shared.CONTEXT_KEYS), len(set(shared.CONTEXT_KEYS)))


class ReadHelpersTest(unittest.TestCase):
    def test_read_items_and_errors(self):
        items = [node("n1", "p")]
        answered = read(items=items)
        self.assertEqual(shared.read_items(answered), items)
        self.assertIsNone(shared.read_error(answered, "node"))
        self.assertEqual(shared.unread_kinds(answered, shared.READ_KINDS), {})
        refused = read(items=items, read_errors={"node": "nodes is forbidden", "storageclass": "sc is forbidden", "pv": "sc is forbidden"})
        self.assertEqual(shared.read_error(refused, "node"), "nodes is forbidden")
        self.assertIsNone(shared.read_error(refused, "pods"))
        self.assertEqual(shared.unread_kinds(refused, ("pods", "node", "pv")), {"node": "nodes is forbidden", "pv": "sc is forbidden"})
        # Nothing read: every kind, including the PDB read's, answers with that failure.
        nothing = read(error="get-credentials failed (1): forbidden")
        self.assertEqual(shared.read_items(nothing), [])
        self.assertEqual(shared.read_error(nothing, "deploy"), "get-credentials failed (1): forbidden")
        self.assertEqual(shared.read_error(nothing, "pods"), "get-credentials failed (1): forbidden")
        self.assertEqual(shared.read_error({"items": None}, "pods"), shared.LATER_READS_SKIPPED_REASON)
        self.assertEqual(shared.read_error(None, "pods"), shared.LATER_READS_SKIPPED_REASON)
        self.assertEqual(shared.items_of_kind(items, "Node"), items)
        self.assertEqual(shared.items_of_kind(None, "Node"), [])


class SharedHelpersTest(unittest.TestCase):
    def test_quantities(self):
        self.assertEqual(shared.cpu_millis("250m"), 250)
        self.assertEqual(shared.cpu_millis("2"), 2000)
        self.assertEqual(shared.cpu_millis("0.5"), 500)
        self.assertEqual(shared.cpu_millis(1), 1000)
        self.assertEqual(shared.memory_bytes("256Mi"), 256 * 1024**2)
        self.assertEqual(shared.memory_bytes("1Gi"), 1024**3)
        self.assertEqual(shared.memory_bytes("1e3"), 1000)
        self.assertEqual(shared.memory_bytes("2868948Ki"), 2868948 * 1024)
        self.assertIsNone(shared.cpu_millis("lots"))
        self.assertIsNone(shared.cpu_millis(True))
        for too_wide in ("1e400", "1" + "0" * 400, "1e300E"):  # float() overflows to inf without raising
            self.assertIsNone(shared.parse_quantity(too_wide), too_wide)
            self.assertIsNone(shared.cpu_millis(too_wide), too_wide)
            self.assertIsNone(shared.memory_bytes(too_wide), too_wide)
        self.assertIsNone(shared.memory_bytes(10**400))  # float() of an int this wide raises instead
        self.assertIsNone(shared.cpu_millis(float("inf")))
        # A quantity that fits a float may not fit a thousand times over: the millicores are
        # checked after the scale, not only the parse.
        for cores_only in ("1e306", "2e305", "1.8e305"):
            self.assertIsNone(shared.cpu_millis(cores_only), cores_only)
            self.assertIsInstance(shared.memory_bytes(cores_only), int, cores_only)
        self.assertEqual(shared.cpu_millis("1e305"), round(1e308))
        self.assertEqual(shared.format_cpu(250), "250m")
        self.assertEqual(shared.format_memory(256 * 1024**2), "256Mi")

    def test_pod_requests_take_the_larger_of_containers_and_init(self):
        spec = {
            "containers": [{"resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}, {"resources": {"requests": {"cpu": "100m"}}}],
            "initContainers": [{"resources": {"requests": {"cpu": "500m", "memory": "32Mi"}}}],
        }
        self.assertEqual(shared.pod_requests(spec), (500, 64 * 1024**2))
        self.assertEqual(shared.pod_requests({"containers": [{"resources": {"requests": {"cpu": "bad"}}}]}), (None, 0))
        self.assertEqual(shared.pod_requests({"containers": [{}]}), (0, 0))
        # A native sidecar (an init container with restartPolicy Always) runs alongside the
        # app containers and alongside every init container started after it, not before it;
        # `overhead` is reserved on top of whichever is larger.
        app = {"resources": {"requests": {"cpu": "500m", "memory": "64Mi"}}}
        sidecar = {"name": "istio-proxy", "restartPolicy": "Always", "resources": {"requests": {"cpu": "100m", "memory": "32Mi"}}}
        init = {"resources": {"requests": {"cpu": "700m", "memory": "16Mi"}}}
        self.assertEqual(shared.pod_requests({"containers": [app], "initContainers": [sidecar]}), (600, 96 * 1024**2))
        self.assertEqual(shared.pod_requests({"containers": [app], "initContainers": [sidecar, init]}), (800, 96 * 1024**2))
        self.assertEqual(shared.pod_requests({"containers": [app], "initContainers": [init, sidecar]}), (700, 96 * 1024**2))
        self.assertEqual(shared.pod_requests({"containers": [app], "overhead": {"cpu": "250m", "memory": "64Mi"}}), (750, 128 * 1024**2))
        self.assertEqual(shared.pod_requests({"containers": [app], "overhead": {"cpu": "lots"}}), (None, 64 * 1024**2))

    def test_pod_owner_names_the_deployment_behind_a_replicaset(self):
        self.assertEqual(shared.pod_owner(deployment_pod("ns", "web", "n1")), {"kind": "Deployment", "namespace": "ns", "name": "web"})
        bare = pod("ns", "job-x", "n1", owner=("Job", "job"))
        self.assertEqual(shared.owner_label(shared.pod_owner(bare)), "Job ns/job")
        self.assertEqual(shared.owner_label(shared.pod_owner(pod("ns", "solo", "n1"))), "Pod ns/solo")
        orphan_rs = pod("ns", "p", "n1", owner=("ReplicaSet", "web-abc"))  # no hash label: stays a ReplicaSet
        self.assertEqual(shared.pod_owner(orphan_rs)["kind"], "ReplicaSet")

    def test_node_helpers(self):
        self.assertTrue(shared.node_schedulable(node("n", "p")))
        self.assertFalse(shared.node_schedulable(node("n", "p", ready=False)))
        self.assertFalse(shared.node_schedulable(node("n", "p", unschedulable=True)))
        self.assertEqual(shared.node_pool(node("n", "idle-pool")), "idle-pool")
        self.assertEqual(shared.node_allocatable(node("n", "p", cpu="940m", memory="1Gi")), (940, 1024**3))

    def test_active_pods_and_volumes(self):
        items = [pod("a", "done", "n", phase="Succeeded"), pod("a", "live", "n", volumes=[socket_volume(), claim_volume("data")])]
        active = shared.active_pods(items)
        self.assertEqual([p["metadata"]["name"] for p in active], ["live"])
        self.assertEqual(shared.hostpath_volumes(active[0]["spec"]), ["/run/containerd/containerd.sock"])
        self.assertEqual(shared.claim_names(active[0]["spec"]), ["data"])
        self.assertTrue(shared.is_mirror_pod(pod("kube-system", "etcd", "n", annotations={"kubernetes.io/config.mirror": "x"})))
        self.assertTrue(shared.is_daemonset_pod(pod("a", "ds-x", "n", owner=("DaemonSet", "ds"))))

    def test_templates_walk_every_kind_once_and_skip_system_namespaces_unless_asked(self):
        items = [
            template("Deployment", "web", {"containers": [{"name": "c", "image": "img"}]}),
            template("CronJob", "job", {"containers": [{"name": "c", "image": "img"}]}),
            template("DaemonSet", "agent", {"containers": [{"name": "c", "image": "img"}]}, namespace="kube-system"),
            {"kind": "Pod", "metadata": {"namespace": "x", "name": "p"}, "spec": {}},
            {"kind": "Deployment", "metadata": {"namespace": "x", "name": "no-template"}, "spec": {}},
        ]
        self.assertEqual([(obj["kind"], obj["object"]) for obj, _, _ in shared.templates(items)], [("Deployment", "seeded-shapes/web"), ("CronJob", "seeded-shapes/job")])
        self.assertEqual([obj["kind"] for obj, _ in shared.template_specs(items)], ["Deployment", "CronJob", "DaemonSet"])
        self.assertEqual([obj["name"] for obj, _, _ in shared.templates(items, ("CronJob",))], ["job"])
        self.assertEqual(shared.pod_template_spec(items[1])["containers"][0]["image"], "img")
        self.assertIsNone(shared.pod_template_spec(items[3]))
        self.assertIsNone(shared.pod_template_spec("not a dict"))
        self.assertEqual(shared.containers({"containers": [{"name": "a"}], "initContainers": [{"name": "b"}, "junk"]}), [{"name": "a"}, {"name": "b"}])

    def test_selectors_keep_operators_and_skip_preferred_terms(self):
        spec = {"nodeSelector": {"a": "1"}, **affinity("b", "NotIn", ["x", "y"])}
        self.assertEqual(shared.selectors(spec), [{"key": "a", "operator": "In", "values": ["1"], "where": "nodeSelector"}, {"key": "b", "operator": "NotIn", "values": ["x", "y"], "where": "nodeAffinity"}])
        self.assertEqual(shared.selectors(affinity("c", "In", ["z"], required=False)), [])
        self.assertEqual(shared.selected_values({**affinity("b", "NotIn", ["x"]), "nodeSelector": {"b": "w"}}, "b"), ["w"])

    def test_pools_for_template_follows_the_nodepool_selector(self):
        pools = [pool("a"), pool("b")]
        self.assertEqual([p["name"] for p in shared.pools_for_template({"nodeSelector": {tables.NODEPOOL_LABEL: "b"}}, pools)], ["b"])
        self.assertEqual([p["name"] for p in shared.pools_for_template(affinity(tables.NODEPOOL_LABEL, "In", ["a"]), pools)], ["a"])
        self.assertEqual(len(shared.pools_for_template({}, pools)), 2)

    def test_pool_helpers(self):
        self.assertTrue(shared.pool_scales_from_zero(pool(autoscaling={"enabled": True, "minNodeCount": 0, "maxNodeCount": 3})))
        self.assertFalse(shared.pool_scales_from_zero(pool(autoscaling={"enabled": True, "minNodeCount": 1})))
        self.assertFalse(shared.pool_scales_from_zero(pool(autoscaling={"enabled": True, "totalMinNodeCount": 2})))
        self.assertFalse(shared.pool_scales_from_zero(pool()))
        self.assertEqual(shared.pool_config({"config": "junk"}), {})
        self.assertEqual(shared.minor_text(TARGET), "1.35")
        self.assertIsNone(shared.minor_text(None))
        self.assertTrue(shared.pool_below_target(pool(), TARGET))
        self.assertFalse(shared.pool_below_target(pool(version=CURRENT), TARGET))
        self.assertFalse(shared.pool_below_target(pool(version="junk"), TARGET))
        self.assertEqual(shared.pools_below_target([pool("a"), pool("b", CURRENT)], TARGET), ["a"])

    def test_cgroup_transition(self):
        v1 = pool(effectiveCgroupMode=tables.CGROUP_V1_MODE)
        self.assertEqual(shared.cgroup_transition(v1, TARGET), ("moves", "GKE migrates cgroup v1 pools to v2 from 1.33"))
        self.assertEqual(shared.cgroup_transition(v1, parse_version("1.32.0-gke.1"))[0], "stays-v1")
        self.assertEqual(shared.cgroup_transition(v1, None), ("unknown", shared.CGROUP_REASON_NO_TARGET))
        pinned = pool(effectiveCgroupMode=tables.CGROUP_V1_MODE, linuxNodeConfig={"cgroupMode": tables.CGROUP_MODE_V1_PIN})
        self.assertEqual(shared.cgroup_transition(pinned, parse_version("1.34.0-gke.1"))[0], "stays-v1")
        self.assertEqual(shared.cgroup_transition(pinned, TARGET), ("moves", "the pool is pinned to cgroup v1 and GKE removes cgroup v1 at 1.35"))
        self.assertEqual(shared.cgroup_transition(pool(effectiveCgroupMode=tables.CGROUP_V2_MODE), TARGET), ("already-v2", shared.CGROUP_REASON_ALREADY))
        self.assertEqual(shared.cgroup_transition(pool(), TARGET), ("unknown", shared.CGROUP_REASON_UNREAD))

    def test_image_repository_and_tag(self):
        self.assertEqual(shared.image_repository_and_tag("docker.io/library/eclipse-temurin:8u302-b08-jre"), ("eclipse-temurin", "8u302-b08-jre"))
        self.assertEqual(shared.image_repository_and_tag("localhost:5000/openjdk"), ("openjdk", ""))
        self.assertEqual(shared.image_repository_and_tag("openjdk:11@sha256:abc"), ("openjdk", "11"))

    def test_system_namespaces_are_the_collectors_set_plus_the_two_prefixes(self):
        for namespace in ("kube-system", "kube-public", "gmp-system", "gke-managed-cim", "gke-gmp-system", "cnrm-system", "istio-system", "gatekeeper-system", "config-management-system"):
            self.assertTrue(shared.is_system_namespace(namespace), namespace)
        for namespace in ("seeded-shapes", "default", "istio-ingress", "gkeapp"):
            self.assertFalse(shared.is_system_namespace(namespace), namespace)

    def test_object_findings_and_their_description(self):
        obj = {"kind": "Deployment", "namespace": "shop", "name": "web", "object": "shop/web"}
        entry = shared.new_finding("x", 4, shared.GRADE_RISK, obj, "keeps data on the node", volumes=["data"])
        self.assertEqual(entry, {"rule": "x", "entry": 4, "grade": "risk", "kind": "Deployment", "namespace": "shop", "name": "web", "object": "shop/web", "detail": "keeps data on the node", "volumes": ["data"]})
        self.assertEqual(shared.describe(entry), "Deployment shop/web: keeps data on the node")
        self.assertEqual(shared.describe(shared.rule_unknown("x", None, "why")), "x: why")
        self.assertEqual(shared.new_result(), {"blocking": [], "risks": [], "unknown": [], "notes": []})


class WorkloadsUnreadDetailTest(unittest.TestCase):
    """The shared text for a failed workload read names the kinds read and the kinds not."""

    KINDS = ("daemonset", "cronjob")

    def test_every_kind_read_is_none(self):
        self.assertIsNone(shared.workloads_unread_detail(read(items=[]), self.KINDS))

    def test_the_namespaced_kinds_unread_leaves_the_pdb_reads_workloads_graded(self):
        detail = shared.workloads_unread_detail(read(items=[], read_errors={"daemonset": "daemonsets is forbidden", "cronjob": "daemonsets is forbidden"}), self.KINDS)
        self.assertEqual(detail, "DaemonSets and CronJobs not read (daemonsets is forbidden); Deployments and StatefulSets graded")
        detail = shared.workloads_unread_detail(read(items=[], read_errors={"cronjob": "cronjobs is forbidden"}), self.KINDS)
        self.assertEqual(detail, "CronJobs not read (cronjobs is forbidden); Deployments, StatefulSets and DaemonSets graded")

    def test_no_read_at_all_says_nothing_was_graded(self):
        for failed in (read(error="get-credentials failed"), None):
            detail = shared.workloads_unread_detail(failed, self.KINDS)
            self.assertTrue(detail.startswith("Deployments, StatefulSets, DaemonSets and CronJobs not read ("), detail)
            self.assertTrue(detail.endswith("; nothing graded"), detail)
        self.assertIn("(get-credentials failed)", shared.workloads_unread_detail(read(error="get-credentials failed"), self.KINDS))
        # Through a rule: the unknown it files says the same, with no object graded.
        from readiness_rules import retired_registry

        out = retired_registry.evaluate(cluster(), {}, read(error="get-credentials failed"), TARGET, {shared.CONTEXT_POOLS: []})
        self.assertEqual([u["detail"] for u in out["unknown"]], ["Deployments, StatefulSets, DaemonSets and CronJobs not read (get-credentials failed); nothing graded"])


class FindingHelpersTest(unittest.TestCase):
    def test_tiers_notes_and_labels(self):
        result = finding.empty_result()
        self.assertEqual(result, shared.new_result())
        finding.add_note(result, "")
        finding.add_note(result, "first")
        finding.add_note(result, "second")
        self.assertEqual(result["notes"], ["first", "second"])
        self.assertEqual(finding.unknown("x", "no runner"), {"rule": "x", "tier": "unknown", "reason": "no runner"})
        cron = template("CronJob", "job", {"containers": [{"name": "c", "image": "img"}]}, namespace="ns")
        self.assertEqual(finding.pod_spec(cron)["containers"][0]["image"], "img")
        self.assertIsNone(finding.pod_spec({"kind": "Pod", "spec": {}}))
        self.assertEqual(finding.workload_label(cron), "CronJob ns/job")
        self.assertEqual((finding.TIER_BLOCKING, finding.TIER_RISK, finding.TIER_UNKNOWN), ("blocking", "risk", "unknown"))
        self.assertEqual((finding.RESULT_BLOCKING, finding.RESULT_RISKS, finding.RESULT_UNKNOWN, finding.RESULT_NOTES), shared.RESULT_KEYS)


class ZonalControlPlaneRuleTest(unittest.TestCase):
    def test_zone_is_a_risk_region_is_clean_garbage_is_unknown(self):
        zonal = zonal_control_plane.evaluate(cluster(location="us-central1-a"), {}, None, None, {})
        self.assertEqual(zonal["blocking"], [])
        self.assertEqual(zonal["risks"][0], {"location": "us-central1-a", "node_locations": ["us-central1-a"]})
        text = zonal_control_plane.describe(zonal["risks"][0])
        self.assertIn("control plane is zonal (us-central1-a)", text)
        self.assertIn("retry with backoff", text)
        self.assertIn("running pods are unaffected", text)
        self.assertEqual(zonal_control_plane.evaluate(cluster(location="us-central1"), {}, None, None, {}), shared.new_result())
        self.assertEqual(zonal_control_plane.evaluate(cluster(location="northamerica-northeast1-b"), {}, None, None, {})["risks"][0]["location"], "northamerica-northeast1-b")
        odd = zonal_control_plane.evaluate(cluster(location="everywhere"), {}, None, None, {})
        self.assertIn("neither a zone nor a region", odd["unknown"][0])
        self.assertFalse(zonal_control_plane.CAN_BLOCK)

    def test_the_cases_cell_pattern_matches_the_cell_the_report_prints(self):
        # The bench case grades the cell the risks column prints for this rule, so its pattern,
        # the rule's finding text and the column's prefix are one fact: a reword of either side
        # fails here, not the first nightly after it merges. The objective's two patterns are
        # read from the case file as it ships (single-quoted YAML scalars, `''` an escaped quote).
        with open(ZONAL_CASE_PATH, encoding="utf-8") as f:
            case = f.read()
        block = case.split(f"- name: {ZONAL_CASE_CELL_OBJECTIVE}", 1)[1].split("- name: ", 1)[0]
        patterns = {kind: re.search(rf"{kind}:\n\s+- '((?:[^']|'')*)'", block).group(1).replace("''", "'") for kind in ("any_of_patterns", "forbidden_patterns")}
        risk = zonal_control_plane.evaluate(cluster(location="us-central1-a"), {}, None, None, {})["risks"][0]
        cell = r.RULE_PREFIX_FORMAT.format(rule=zonal_control_plane.RULE_ID, text=zonal_control_plane.describe(risk))
        self.assertTrue(cell.startswith("zonal-control-plane: control plane is zonal (us-central1-a): "), cell)
        for name in ("seeded-a", "seeded-d"):
            accepted = patterns["any_of_patterns"].replace(ZONAL_CASE_CLUSTER_PLACEHOLDER, re.escape(name))
            refused = patterns["forbidden_patterns"].replace(ZONAL_CASE_CLUSTER_PLACEHOLDER, re.escape(name))
            for quoted in (cell, cell.split(": ", 1)[1]):  # the column's cell, and the bare text `report.json` carries
                line = ZONAL_CASE_LINE_FORMAT.format(cluster=name, rule=zonal_control_plane.RULE_ID, cell=quoted).lower()
                self.assertIsNotNone(re.search(accepted, line), line)
                self.assertIsNone(re.search(refused, line), line)
            reworded = ZONAL_CASE_LINE_FORMAT.format(cluster=name, rule=zonal_control_plane.RULE_ID, cell=cell.replace("unavailable for minutes", "down for a while")).lower()
            self.assertIsNone(re.search(accepted, reworded), reworded)
            self.assertIsNotNone(re.search(refused, reworded), reworded)
        self.assertEqual((zonal_control_plane.RULE_ID, zonal_control_plane.ENTRY), ("zonal-control-plane", 11))

    def test_multi_zonal_reads_as_zonal_and_the_zone_key_is_a_fallback(self):
        record = cluster(location="us-central1-a", locations=["us-central1-a", "us-central1-b"])
        self.assertEqual(zonal_control_plane.evaluate(record, {}, None, None, {})["risks"][0]["node_locations"], ["us-central1-a", "us-central1-b"])
        record = cluster(location=None)
        del record["location"]
        record["zone"] = "europe-west4-b"
        self.assertEqual(zonal_control_plane.evaluate(record, {}, None, None, {})["risks"][0]["location"], "europe-west4-b")

    def test_the_read_does_not_matter(self):
        record = cluster(location="us-central1-a")
        self.assertEqual(zonal_control_plane.evaluate(record, {}, read(error="forbidden"), TARGET, {}), zonal_control_plane.evaluate(record, {}, read(items=[]), TARGET, {}))


class ShapeTablesTest(unittest.TestCase):
    def test_containerd_major_by_minor_and_image_family(self):
        self.assertEqual(tables.containerd_major_at((1, 32), tables.NODE_OS_LINUX), 1)
        self.assertEqual(tables.containerd_major_at((1, 33), tables.NODE_OS_LINUX), 2)
        self.assertEqual(tables.containerd_major_at((1, 34), tables.NODE_OS_WINDOWS), 1)
        self.assertEqual(tables.containerd_major_at((1, 35), tables.NODE_OS_WINDOWS), 2)
        self.assertEqual(tables.node_image_os_family("ubuntu_containerd"), tables.NODE_OS_LINUX)
        self.assertEqual(tables.node_image_os_family("WINDOWS_SAC_CONTAINERD"), tables.NODE_OS_WINDOWS)
        self.assertEqual(tables.node_image_os_family(""), tables.NODE_OS_LINUX)

    def test_cgroup_floors_are_the_sourced_values(self):
        self.assertEqual(tables.DOTNET_CGROUP_V2_VERSION, (5, 0))
        self.assertEqual((tables.JDK8_CGROUP_V2_UPDATE, tables.JDK11_CGROUP_V2_PATCH, tables.JDK_FIRST_MAJOR_WITH_CGROUP_V2), (372, (0, 16), 15))
        self.assertEqual((tables.CGROUP_V2_MIGRATION_MINOR, tables.CGROUP_V1_REMOVAL_MINOR, tables.GROUP_OOM_KILL_KUBELET_MINOR), (33, 35, 28))

    def test_every_default_change_row_names_a_source_and_a_minor_inside_as_of(self):
        for minor, rows in tables.DEFAULT_CHANGES_BY_MINOR.items():
            self.assertLessEqual(minor, tables.DEFAULT_CHANGES_AS_OF)
            for row in rows:
                self.assertTrue(row["source"].startswith("https://"), row)
                self.assertIn(row["detector"], (tables.DETECTOR_POD_SECURITY, tables.DETECTOR_GITREPO_VOLUME))
                self.assertIn(row["side"], (tables.SIDE_APISERVER, tables.SIDE_KUBELET))
                self.assertIsInstance(row["tightens"], bool)

    def test_addon_support_rows_name_a_source_and_a_date(self):
        for addon, support in tables.ADDON_SUPPORT.items():
            self.assertTrue(support["source"].startswith("https://"), addon)
            self.assertRegex(support["read_on"], r"^\d{4}-\d{2}-\d{2}$")
            for release, (low, high) in support["releases"].items():
                self.assertLessEqual(low, high, (addon, release))
        self.assertTrue(set(tables.ADDON_SUPPORT) <= set(tables.ADDON_IMAGES.values()))

    def test_driver_tables_and_dropped_labels_carry_their_shape(self):
        for image_type, by_minor in tables.GKE_GPU_DRIVERS_BY_IMAGE_TYPE.items():
            self.assertIn(image_type, tables.NODE_IMAGE_OS_FAMILY)
            for minor, (default, latest) in by_minor.items():
                self.assertLessEqual(default, latest, (image_type, minor))
        for major, floor in tables.CUDA_MAJOR_MIN_DRIVER.items():
            self.assertEqual(floor, min(d for (m, _), d in tables.CUDA_TOOLKIT_MIN_DRIVER.items() if m == major), major)
        for label, value, minor, source in tables.DROPPED_NODE_LABELS:
            self.assertTrue(label and isinstance(minor, tuple) and source, label)
        self.assertEqual(set(tables.RETIRED_IMAGE_HOSTS), set(tables.RETIRED_IMAGE_HOST_NOTES))

    def test_a_removal_table_of_another_shape_is_unreadable_not_empty(self):
        # A missing file and bad JSON were named; a JSON array at the top level raised past
        # the named message, and a dict under `removed` read as an empty table.
        import pkgutil
        from unittest import mock

        for raw in (b'[{"api_version": "x"}]', b'{"removed": {"a": 1}}', b'{"other": []}'):
            tables._REMOVED_APIS_CACHE = None
            try:
                with mock.patch.object(pkgutil, "get_data", return_value=raw):
                    with self.assertRaises(RuntimeError) as refused:
                        tables.replacement_for("extensions/v1beta1 networkpolicies")
                self.assertIn("removed_apis.json could not be read beside upgrade_shape_tables: not an object with a list under 'removed'", str(refused.exception), raw)
            finally:
                tables._REMOVED_APIS_CACHE = None
        # A row that is not an object with `api_version`, `kind` and `replacement` is unreadable
        # too, as the scan's own loader has it, rather than dropping out of the lookup.
        for raw in (b'{"removed": ["extensions/v1beta1 NetworkPolicy"]}', b'{"removed": [{"kind": "NetworkPolicy", "replacement": "networking.k8s.io/v1"}]}', b'{"removed": [{"api_version": "extensions/v1beta1", "kind": "NetworkPolicy", "replacement": ""}]}'):
            tables._REMOVED_APIS_CACHE = None
            try:
                with mock.patch.object(pkgutil, "get_data", return_value=raw):
                    with self.assertRaises(RuntimeError) as refused:
                        tables.replacement_for("extensions/v1beta1 networkpolicies")
                self.assertIn("row 0 under 'removed' is not an object with non-empty strings under 'api_version', 'kind', 'replacement'", str(refused.exception), raw)
            finally:
                tables._REMOVED_APIS_CACHE = None
        self.assertEqual(tables.replacement_for("extensions/v1beta1 networkpolicies"), "networking.k8s.io/v1")

    def test_replacement_comes_from_the_scan_table(self):
        self.assertEqual(tables.replacement_for("flowcontrol.apiserver.k8s.io/v1beta3 flowschemas"), "flowcontrol.apiserver.k8s.io/v1")
        self.assertEqual(tables.replacement_for("policy/v1beta1 podsecuritypolicies"), "none; Pod Security Admission or a policy controller")
        self.assertIsNone(tables.replacement_for("core/v1 endpoints"))
        self.assertIsNone(tables.replacement_for("nonsense"))
        # The methodName fallback of audit_log.api_of spells a `*.k8s.io` group without the
        # suffix; the lookup tries both spellings, so an entry with no resourceName still
        # finds its row, and a group that never had the suffix still matches as before.
        self.assertEqual(tables.replacement_for("flowcontrol.apiserver/v1beta3 flowschemas"), "flowcontrol.apiserver.k8s.io/v1")
        self.assertIsNotNone(tables.replacement_for("networking.k8s.io/v1beta1 ingresses"))
        self.assertEqual(tables.replacement_for("networking/v1beta1 ingresses"), tables.replacement_for("networking.k8s.io/v1beta1 ingresses"))
        self.assertEqual(tables.replacement_for("rbac.authorization/v1beta1 roles"), tables.replacement_for("rbac.authorization.k8s.io/v1beta1 roles"))
        self.assertIsNotNone(tables.replacement_for("apps/v1beta1 deployments"))
        self.assertIsNone(tables.replacement_for("/v1 pods"))

    def test_shell_forks_reads_a_background_job_and_nothing_else(self):
        # The round-1 strings keep their verdicts; what a regex guessed at (`|&`, a heredoc
        # body, a comment, an escaped quote, a quoted or escaped `&`, `;&`) is not a fork,
        # and a forked subshell (`)&`) or a fork after a redirection is.
        forks = (
            "a & b & wait",
            "nginx -p 8080& php-fpm",
            "./a 1& ./b 2& wait",
            "sleep 1 &",
            "cmd & wait",
            "(cd /srv && ./run)&",
            "cmd 2>&1 &",
            "cat <<EOF | grep x &\na & b\nEOF\n",
            "echo a#b&c",
            # A command substitution is the command list it runs, inside double quotes too.
            'echo "$(sleep 9 &)"',
            "x=$(sleep 1 &); echo $x",
            "x=`sleep 1 &`",
            'echo "$(a; (b) &)"',
            "echo $(date)& wait",
            # The constructs rounds 4 and 5 named: two parentheses that are not arithmetic, a
            # substitution in an unquoted heredoc body, in a parameter default, in arithmetic,
            # in backticks inside `${…}`, and a fork after an ANSI-C quoted word.
            "((sleep 10; kill 1) &); exec app",
            "cat <<EOF >/tmp/boot\n$(start-agent &)\nEOF\nexec app",
            "exec ${SIDECAR:-$(sleep 9 &)} app",
            "echo $(( $(sleep 9 &) ))",
            "echo $[ $(sleep 9 &) ]",
            "sleep 1 & echo $[1+1]",
            "exec ${X:-`run &`}",
            "echo $'it\\'s up'; sleep 1 &",
            'echo "${X:-"$(sleep 1 &)"}"',
            "$( ((a) &) )",
            # A `case` inside a substitution: a pattern's `)` closes no substitution.
            'echo "$(case $MODE in dev) sleep 9 & ;; esac)"',
            "exec ${X:-$(case $m in a) sleep 1 & ;; esac)} app",
            "cat <<EOF\n$(case $m in a) sleep 1 & ;; esac)\nEOF\n",
            # A process substitution and `coproc` run their command list asynchronously; a
            # `case` that is an argument is a word, so the fork after it is read.
            "exec > >(tee -a /var/log/app.log) 2>&1; ./app",
            "diff <(a) <(b)",
            "coproc tee -a log",
            'echo "$(grep case app.log)"; sleep 9 &',
            'cat <<E"O"F\nx\nEOF\nsleep 1 &',
            "if true; then coproc sleep 9; fi",
            # A `case` inside a subshell inside a quoted substitution: the pattern's `)` closes
            # neither the subshell nor the substitution.
            'echo "$( (case $MODE in dev) sleep 9 & ;; esac) )"',
            # A `function NAME { … }` body is a command list: the `{` word opens one wherever it stands.
            'x="$(function f { case $1 in a) sleep 9 & ;; esac; }; f)"',
            "coproc w { case $1 in a) :;; esac; }",
        )
        not_forks = (
            "a && b",
            "cmd 2>&1",
            "cmd &>/dev/null",
            "exec 3<&0",
            "cmd >&2",
            "echo done",
            "cmd 2>&1 |& tee /var/log/app.log",
            "cat <<EOF\nuser=a&b\nEOF\necho done",
            "cat <<-'EOF'\n\ta & b\n\tEOF\n",
            "cat << \"EOF\" <<X\na & b\nEOF\nc & d\nX\n",
            "echo start # a & b in a comment\necho end",
            'echo "a \\"&\\" b"',
            "echo '&'",
            'echo "&"',
            "echo \\&",
            "cmd <<<'a & b'",
            "case $1 in a) x;& b) y;; esac",
            "cmd &>>log",
            # Arithmetic and parameter expansions are words: `&` and `<<` in them are not operators.
            "flags=$((mode & 4)); exec app",
            "echo $((5 & 3))",
            "n=$((1<<2)); exec ./app",
            "(( x & 1 )) && echo odd",
            "curl ${URL:-http://h/p?a=1&b=2}",
            "echo ${PORT:-80&}",
            "echo ${s//&/and}",
            'echo "${X:-"a&b"}"',
            'echo "$(echo "a&b")"',
            "echo $(( $(nproc) & 1 ))",
            "x=$(cat <<EOF)\na & b\nEOF\necho $x",
            # Read as bash reads them: a quoted or backslash-quoted heredoc delimiter makes
            # the body text, an escaped `$` is text, `$'…'` is one quoted word, `((…))`
            # closed by `))` is arithmetic.
            "echo $'don\\'t' && exec app",
            "cat <<'EOF'\n$(start-agent &)\nEOF\n",
            "cat <<\\EOF\n$(start-agent &)\nEOF\n",
            "cat <<EOF\n\\$(start-agent &)\nEOF\n",
            "echo $(( (1 + 2) * 3 ))",
            "echo ${a:-$(nproc)} && exec app",
            'x="$(case $1 in a) echo "a & b";; esac)"',
            'echo "$(case $x in a|b) echo ok;; *) echo no;; esac)"',
            "x=$(case $1 in (a) echo a;; esac); echo $x",
            'echo "$(case $x in a) (b);; esac)"',
            # `case` is a word away from command position; a half-quoted delimiter is `EOF`,
            # quoted; three nested arithmetic levels are words.
            "x=$(grep -c case file); exec app",
            "$(find . -name case) && exec app",
            'echo "$(grep case app.log)"',
            "case $x in a) echo case;; esac",
            'cat <<E"O"F\n$(start-agent &)\nEOF\necho done',
            "echo $(($(($((1))))))",
            "cmd < file; cat > case",
            'x="$( (case $1 in a) echo "a & b";; esac) )"',
            "( case $x in a) :;; esac ) && exec app",
            "(case $x in (a) (b);; esac)",
            'x="$(function f { case $1 in a) echo "a & b";; esac; }; f)"',
            "f() { case $1 in a) :;; esac; }; f",
            "function g { :; }; g && exec app",
            "find . -exec cmd {} \\;",
            # Bash's older `$[…]` arithmetic is a word like `$((…))`, its brackets balanced,
            # in a word, in double quotes, in a parameter default and in a heredoc body.
            "echo $[mode & 4]",
            "flags=$[mode & 4]; exec app",
            "echo $[1<<2]",
            "echo $[a[1] & 2]",
            'echo "$[x & 1]"',
            "x=${a:-$[1&2]}",
            "echo $(($[1&2]))",
            "cat <<EOF\n$[a & b]\nEOF\n",
        )
        for text in forks:
            self.assertIs(shared.shell_forks(text), True, text)
        for text in not_forks:
            self.assertIs(shared.shell_forks(text), False, text)
        # What cannot be read this way is unread, never clean: an unterminated quote or
        # heredoc, a trailing backslash, no script at all.
        for text in ("echo 'a & b", 'echo "a & b', "cat <<EOF\na & b\n", "cat <<EOF", "echo a \\", "echo $(a", 'echo "$(a"', "echo `a", "x=${a", "echo $[1+", "y=$((1+", "((1", "echo $'abc", "cat <<EOF\n$(a &\nEOF\n", "((a) &", 'echo "$(case $x in a) echo', "echo " + "$((" * 70 + "1" + "))" * 70, None, 3):
            self.assertIsNone(shared.shell_forks(text), text)
        # A span nested forty deep is walked a bounded number of times, not once per level
        # above it; past the nesting cap the script is unread.
        started = time.perf_counter()
        self.assertIs(shared.shell_forks("echo " + "$((" * 40 + "1" + "))" * 40), False)
        self.assertIs(shared.shell_forks("echo " + "$(" * 40 + "sleep 1 &" + ")" * 40), True)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertFalse(hasattr(tables, "SHELL_FORK_RE") or hasattr(tables, "SHELL_QUOTED_SPAN_RE"))

    def test_stateful_volume_names_match_how_the_database_charts_spell_them(self):
        stateful = ("data", "pgdata", "datadir", "database", "mysqldata", "mongodata", "state", "pg-wal", "wal", "db", "journal", "queue", "store", "persist", "wal-e-bin", "redis_data", "data-0", "es-data-1", "persistent-storage", "DATA")
        # Listed words run together with nothing between them are the compound they spell,
        # and a chart's own run-together name is named whole.
        compounds = ("datastore", "dbdata", "statedb", "storagedb", "persistentstate", "DataStore", "wal-datastore", "datalogdir")
        # Each word on its own: a stem inside an unrelated word (`restore`, `metadata`,
        # `logdatadog`, `dequeue`, `firestore`, `statefulset`) is not the volume it names,
        # and neither is a listed word with a residue of other letters beside it (`esdata`,
        # `datalog`); a 61-letter run of listed words with one stray letter is refused
        # in one pass, not backtracked over.
        scratch = ("nginx-cache", "cache", "tmp", "dbus", "walrus", "config", "logs", "bin", "restore-scripts", "backup-restore", "pod-metadata", "logdatadog", "dequeue-worker", "firestore-creds", "statefulset-config", "esdata", "datalog", "logdir", "sdata", "data" * 15 + "x")
        for name in stateful + compounds:
            self.assertTrue(tables.STATEFUL_VOLUME_NAME_RE.search(name), name)
        for name in scratch:
            self.assertFalse(tables.STATEFUL_VOLUME_NAME_RE.search(name), name)

    def test_parse_and_format_minor(self):
        self.assertEqual(tables.parse_minor("1.35.1-gke.1000"), (1, 35))
        self.assertEqual(tables.parse_minor("v1.29.0"), (1, 29))
        self.assertEqual(tables.parse_minor("1.32"), (1, 32))
        self.assertIsNone(tables.parse_minor("latest"))
        self.assertIsNone(tables.parse_minor(None))
        self.assertEqual(tables.format_minor((1, 35)), "1.35")
        self.assertEqual(tables.format_minor(TARGET), "1.35")

    def test_every_table_section_names_a_source(self):
        # Each table block cites its source in the comment beside it: a page, an issue, or
        # the file in this repository it is read from. A section with none is a table a
        # reader cannot check.
        with open(tables.__file__, encoding="utf-8") as f:
            text = f.read()
        sections = text.split("# ----")[1:]
        self.assertGreater(len(sections), 10)
        for section in sections:
            title = section.splitlines()[0].strip("- ")
            self.assertTrue(any(marker in section for marker in ("Source:", "https://", ".com/", ".io/", "kubernetes/kubernetes#", "dotnet/runtime#", ".json")), title)


# ------------------------------------------------------------- the incident rules (#3524)
# Recorded inputs under testdata/: the dev project's backend services, instances, operations
# and server config as gcloud returned them on 2026-10-20, the seeded-b cluster record and
# nodes, and the kube-system and gmp-system add-ons, projected to the fields the rules read.


TESTDATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata")
# The instant the fixtures were recorded at, so the operations' ages and the table's age read as they did that day.
RECORDED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
DEV_PROJECT = "haoxuw-gke-dev"


def recorded(name):
    with open(os.path.join(TESTDATA, name), encoding="utf-8") as f:
        return json.load(f)


def project_reads(**overrides):
    """The context's project reads from the recorded files, each or any replaced by a test."""
    files = {
        shared.PROJECT_READ_BACKEND_SERVICES: "project_backend_services.json",
        shared.PROJECT_READ_INSTANCES: "project_instances.json",
        shared.PROJECT_READ_OPERATIONS: "project_operations.json",
        shared.LOCATION_READ_SERVER_CONFIG: "server_config_us_central1_a.json",
    }
    reads = {name: {shared.PROJECT_READ_DATA_KEY: recorded(path), shared.PROJECT_READ_ERROR_KEY: None} for name, path in files.items()}
    for name, value in overrides.items():
        reads[name] = value if isinstance(value, dict) and set(value) == {shared.PROJECT_READ_DATA_KEY, shared.PROJECT_READ_ERROR_KEY} else {shared.PROJECT_READ_DATA_KEY: value, shared.PROJECT_READ_ERROR_KEY: None}
    return reads


def failed_read(reason="permission denied"):
    return {shared.PROJECT_READ_DATA_KEY: None, shared.PROJECT_READ_ERROR_KEY: reason}


def seeded_b():
    return recorded("cluster_seeded_b.json")


def dev_host():
    """The dev project's platform-agent-host as the clusters list spells it, without a maintenance window: the one cluster whose recorded operations carry a capacity failure."""
    return {"name": "platform-agent-host", "location": "us-central1", "status": "RUNNING", "currentMasterVersion": "1.35.8-gke.1439001", "releaseChannel": {"channel": "REGULAR"}, "nodePools": []}


def context_for(record, target_text=None, at=RECORDED_AT, reads=None):
    member = {"project": DEV_PROJECT, "location": record["location"], "cluster": record["name"], "target_version": target_text}
    context = report.readiness_context(member, at, [record], [], parse_version(record.get("currentMasterVersion")), False, project_reads() if reads is None else reads)
    context[shared.CONTEXT_RUN_CMD] = lambda cmd, timeout=None, env=None: (1, "", "no runner in the rule tests")
    return context


def seeded_b_read(extra_items=(), **kw):
    return read(items=list(recorded("nodes_seeded_b.json")) + list(recorded("addons_seeded_b.json")) + list(extra_items), **kw)


class SharedReadHelpersTest(unittest.TestCase):
    def test_project_read_answers_data_an_error_or_a_missing_read(self):
        reads = project_reads(**{shared.PROJECT_READ_INSTANCES: failed_read("timed out")})
        context = {shared.CONTEXT_PROJECT_READS: reads}
        data, error = shared.project_read(context, shared.PROJECT_READ_OPERATIONS)
        self.assertIsNone(error)
        self.assertEqual(len(data), 31)
        self.assertEqual(shared.project_read(context, shared.PROJECT_READ_INSTANCES), (None, "timed out"))
        self.assertEqual(shared.project_read({}, shared.PROJECT_READ_INSTANCES), (None, shared.PROJECT_READ_NOT_MADE_REASON.format(read="instances")))
        self.assertEqual(shared.project_read({shared.CONTEXT_PROJECT_READS: "junk"}, shared.PROJECT_READ_INSTANCES)[0], None)
        self.assertIn(shared.CONTEXT_PROJECT_READS, shared.CONTEXT_KEYS)
        self.assertEqual(set(reads), set(shared.PROJECT_READS))

    def test_rfc3339_durations_and_node_instances(self):
        parsed = shared.parse_rfc3339("2026-10-09T16:04:01.300210096Z")
        self.assertEqual(parsed, datetime(2026, 10, 9, 16, 4, 1, 300210, tzinfo=timezone.utc))
        self.assertEqual(shared.parse_rfc3339("2026-10-09T16:04:01+02:00"), datetime(2026, 10, 9, 14, 4, 1, tzinfo=timezone.utc))
        self.assertIsNone(shared.parse_rfc3339("yesterday"))
        self.assertIsNone(shared.parse_rfc3339(None))
        self.assertEqual(shared.format_duration(6 * 86400 + 3 * 3600 + 7), "6d 3h")
        self.assertEqual(shared.format_duration(3 * 3600 + 12 * 60), "3h 12m")
        self.assertEqual(shared.format_duration(12 * 60 + 59), "12m")
        self.assertEqual(shared.format_duration(30), "under a minute")
        self.assertEqual(shared.age_seconds(RECORDED_AT, RECORDED_AT + timedelta(hours=1)), 0)
        first = recorded("nodes_seeded_b.json")[0]
        self.assertEqual(shared.node_instance(first), ("us-central1-a", "gke-seeded-b-default-pool-4176890d-q0q0"))
        self.assertEqual(shared.node_provider_id(first), "gce://haoxuw-gke-dev/us-central1-a/gke-seeded-b-default-pool-4176890d-q0q0")
        self.assertEqual(shared.node_ready(first), "True")
        bare = node("gke-x-default-pool-abc-1234", "default-pool", labels={shared.NODE_ZONE_LABEL: "us-east4-b"})
        self.assertEqual(shared.node_instance(bare), ("us-east4-b", "gke-x-default-pool-abc-1234"))
        self.assertEqual(shared.node_instance(node("n", "p")), ("", "n"))
        self.assertEqual(shared.node_ready({"status": {}}), "")
        # An operation's target: the cluster itself, one of its node pools, or neither.
        cluster_link = "https://container.googleapis.com/v1/projects/196584622296/zones/us-central1-a/clusters/seeded-b"
        self.assertEqual(shared.operation_target({"targetLink": cluster_link}, "seeded-b", "us-central1-a"), (True, None))
        self.assertEqual(shared.operation_target({"targetLink": cluster_link + "/nodePools/default-pool"}, "seeded-b", "us-central1-a"), (True, "default-pool"))
        self.assertEqual(shared.operation_target({"targetLink": cluster_link + "/nodePools/default-pool"}, "seeded-a", "us-central1-a"), (False, None))
        self.assertEqual(shared.operation_target({"targetLink": cluster_link}, "seeded-b", "us-central1"), (False, None))
        self.assertEqual(shared.operation_target({"targetLink": cluster_link.replace("zones/us-central1-a", "locations/us-central1")}, "seeded-b", "us-central1"), (True, None))
        self.assertEqual(shared.operation_target("junk", "seeded-b", "us-central1-a"), (False, None))


class LbConnectionDrainingRuleTest(unittest.TestCase):
    def planted(self, timeout=3600, region=None, group=None):
        """A backend service as the fixture plants it: the seeded-b default pool's instance group, draining 3600 s."""
        record = seeded_b()
        group = group or record["nodePools"][0]["instanceGroupUrls"][0].replace("instanceGroupManagers", "instanceGroups")
        service = {"name": "drain-timeout-seeded-b", "selfLink": "https://www.googleapis.com/compute/v1/projects/haoxuw-gke-dev/global/backendServices/drain-timeout-seeded-b", "connectionDraining": {"drainingTimeoutSec": timeout}, "backends": [{"balancingMode": "UTILIZATION", "group": group}]}
        if region:
            service["region"] = f"https://www.googleapis.com/compute/v1/projects/haoxuw-gke-dev/regions/{region}"
        return service

    def test_the_dev_projects_backend_services_match_no_pool_and_the_note_counts_them(self):
        record = seeded_b()
        result = lb_connection_draining.evaluate(record, {}, seeded_b_read(), None, context_for(record))
        self.assertEqual((result["blocking"], result["risks"], result["unknown"]), ([], [], []))
        # The two ILB services sit on `k8s-ig--` groups, which the rule does not match: counted in the total, not as matched.
        self.assertEqual(result["notes"], ["no backend service on this cluster's pools or NEGs drains for more than 600 s (0 matched of 2 in the project)"])

    def test_a_backend_service_on_the_pool_above_the_wait_is_a_blocker_with_the_command(self):
        record = seeded_b()
        reads = project_reads(**{shared.PROJECT_READ_BACKEND_SERVICES: recorded("project_backend_services.json") + [self.planted()]})
        result = lb_connection_draining.evaluate(record, {}, seeded_b_read(), None, context_for(record, reads=reads))
        self.assertEqual(len(result["blocking"]), 1)
        finding = result["blocking"][0]
        self.assertEqual((finding["backend_service"], finding["timeout_seconds"], finding["pools"], finding["negs"], finding["scope"]), ("drain-timeout-seeded-b", 3600, ["default-pool"], [], "--global"))
        text = lb_connection_draining.describe(finding)
        self.assertTrue(text.startswith("BackendService drain-timeout-seeded-b drains for 3600 s on pool(s) default-pool; GKE waits 600 s per node during an upgrade, so each surge node's drain times out"), text)
        self.assertIn("`gcloud compute backend-services update drain-timeout-seeded-b --global --connection-draining-timeout=600`", text)
        self.assertEqual(result["notes"], [])
        # A regional service names its region in the command; one at the wait is clean.
        regional = project_reads(**{shared.PROJECT_READ_BACKEND_SERVICES: [self.planted(region="us-central1")]})
        finding = lb_connection_draining.evaluate(record, {}, seeded_b_read(), None, context_for(record, reads=regional))["blocking"][0]
        self.assertIn("--region us-central1 --connection-draining-timeout=600", finding["action"])
        at_wait = project_reads(**{shared.PROJECT_READ_BACKEND_SERVICES: [self.planted(timeout=600)]})
        result = lb_connection_draining.evaluate(record, {}, seeded_b_read(), None, context_for(record, reads=at_wait))
        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["notes"], ["no backend service on this cluster's pools or NEGs drains for more than 600 s (1 matched of 1 in the project)"])
        # The pool's manager URL spells the group the same way: matched either way.
        manager_url = record["nodePools"][0]["instanceGroupUrls"][0]
        as_manager = project_reads(**{shared.PROJECT_READ_BACKEND_SERVICES: [self.planted(group=manager_url)]})
        self.assertEqual(len(lb_connection_draining.evaluate(record, {}, seeded_b_read(), None, context_for(record, reads=as_manager))["blocking"]), 1)
        # Another cluster's pool is not this cluster's finding.
        other = project_reads(**{shared.PROJECT_READ_BACKEND_SERVICES: [self.planted(group="https://www.googleapis.com/compute/v1/projects/haoxuw-gke-dev/zones/us-central1-a/instanceGroups/gke-seeded-a-default-pool-0000-grp")]})
        self.assertEqual(lb_connection_draining.evaluate(record, {}, seeded_b_read(), None, context_for(record, reads=other))["blocking"], [])

    def test_a_neg_the_cluster_annotates_is_matched_to_its_service(self):
        record = seeded_b()
        neg_service = {"kind": "Service", "metadata": {"namespace": "shop", "name": "web", "annotations": {"cloud.google.com/neg-status": json.dumps({"network_endpoint_groups": {"80": "k8s1-abc-shop-web-80-def"}, "zones": ["us-central1-a"]})}}}
        neg_url = "https://www.googleapis.com/compute/v1/projects/haoxuw-gke-dev/zones/us-central1-a/networkEndpointGroups/k8s1-abc-shop-web-80-def"
        reads = project_reads(**{shared.PROJECT_READ_BACKEND_SERVICES: [self.planted(group=neg_url)]})
        with_services = seeded_b_read()
        with_services["webhook_items"] = [neg_service]
        result = lb_connection_draining.evaluate(record, {}, with_services, None, context_for(record, reads=reads))
        self.assertEqual((result["blocking"][0]["negs"], result["blocking"][0]["pools"]), ([{"group": "k8s1-abc-shop-web-80-def", "service": "shop/web"}], []))
        self.assertIn("drains for 3600 s on NEG k8s1-abc-shop-web-80-def of Service shop/web; GKE waits", lb_connection_draining.describe(result["blocking"][0]))
        # Services not read: the NEG half is unknown only when a NEG backend was seen.
        unread = seeded_b_read()
        unread["webhook_items"], unread["webhook_error"] = None, "timed out"
        result = lb_connection_draining.evaluate(record, {}, unread, None, context_for(record, reads=reads))
        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["unknown"], ["the Services were not read (timed out), so a NEG-backed backend service was not matched"])
        self.assertEqual(lb_connection_draining.evaluate(record, {}, unread, None, context_for(record))["unknown"], [])

    def test_one_blocker_per_backend_service_names_every_matched_pool_once(self):
        # One service with two backends on two pools: one blocker naming both pools, one service counted.
        record = seeded_b()
        default_group, surge_group = (pool["instanceGroupUrls"][0].replace("instanceGroupManagers", "instanceGroups") for pool in record["nodePools"])
        service = self.planted()
        service["backends"] = [{"balancingMode": "UTILIZATION", "group": default_group}, {"balancingMode": "UTILIZATION", "group": surge_group}]
        reads = project_reads(**{shared.PROJECT_READ_BACKEND_SERVICES: [service]})
        result = lb_connection_draining.evaluate(record, {}, seeded_b_read(), None, context_for(record, reads=reads))
        self.assertEqual([(f["backend_service"], f["pools"]) for f in result["blocking"]], [("drain-timeout-seeded-b", ["default-pool", "no-surge-pool"])])
        self.assertIn("drains for 3600 s on pool(s) default-pool, no-surge-pool; GKE waits 600 s", lb_connection_draining.describe(result["blocking"][0]))
        # A multi-zone pool: one instance group per zone, both backends of the service, the pool named once; at the wait, one service matched of one.
        regional = copy.deepcopy(record)
        regional["nodePools"][0]["instanceGroupUrls"].append(regional["nodePools"][0]["instanceGroupUrls"][0].replace("us-central1-a", "us-central1-b"))
        service["backends"] = [{"balancingMode": "UTILIZATION", "group": default_group}, {"balancingMode": "UTILIZATION", "group": default_group.replace("us-central1-a", "us-central1-b")}]
        result = lb_connection_draining.evaluate(regional, {}, seeded_b_read(), None, context_for(regional, reads=reads))
        self.assertEqual([f["pools"] for f in result["blocking"]], [["default-pool"]])
        service["connectionDraining"]["drainingTimeoutSec"] = 600
        result = lb_connection_draining.evaluate(regional, {}, seeded_b_read(), None, context_for(regional, reads=reads))
        self.assertEqual((result["blocking"], result["notes"]), ([], ["no backend service on this cluster's pools or NEGs drains for more than 600 s (1 matched of 1 in the project)"]))

    def test_a_failed_or_missing_read_is_unknown(self):
        record = seeded_b()
        failed = project_reads(**{shared.PROJECT_READ_BACKEND_SERVICES: failed_read("gcloud compute backend-services list failed (1): forbidden")})
        self.assertEqual(lb_connection_draining.evaluate(record, {}, seeded_b_read(), None, context_for(record, reads=failed))["unknown"], ["gcloud compute backend-services list failed (1): forbidden"])
        self.assertEqual(lb_connection_draining.evaluate(record, {}, seeded_b_read(), None, context_for(record, reads={}))["unknown"], [shared.PROJECT_READ_NOT_MADE_REASON.format(read="backend_services")])
        self.assertTrue(lb_connection_draining.CAN_BLOCK)
        self.assertEqual((lb_connection_draining.RULE_ID, lb_connection_draining.ENTRY), ("lb-connection-draining", 21))


class OrphanNodeObjectsRuleTest(unittest.TestCase):
    def test_the_recorded_nodes_are_live_and_the_note_counts_them(self):
        record = seeded_b()
        result = orphan_node_objects.evaluate(record, {}, seeded_b_read(), None, context_for(record))
        self.assertEqual((result["blocking"], result["unknown"]), ([], []))
        self.assertEqual(result["notes"], ["0 orphan Node(s) (2 nodes, 19 instances in the project)"])

    def test_a_node_with_no_instance_behind_it_is_a_blocker_naming_its_state(self):
        record = seeded_b()
        ghost = copy.deepcopy(recorded("nodes_seeded_b.json")[0])
        ghost["metadata"]["name"] = "gke-seeded-b-default-pool-4176890d-ghost"
        ghost["spec"]["providerID"] = "gce://haoxuw-gke-dev/us-central1-a/gke-seeded-b-default-pool-4176890d-ghost"
        ghost["spec"]["unschedulable"] = True
        ghost["status"]["conditions"] = [{"type": "Ready", "status": "Unknown"}]
        result = orphan_node_objects.evaluate(record, {}, seeded_b_read([ghost]), None, context_for(record))
        self.assertEqual(len(result["blocking"]), 1)
        finding = result["blocking"][0]
        self.assertEqual((finding["node"], finding["ready"], finding["unschedulable"], finding["action"]), ("gke-seeded-b-default-pool-4176890d-ghost", "Unknown", True, "kubectl delete node gke-seeded-b-default-pool-4176890d-ghost"))
        self.assertEqual(orphan_node_objects.describe(finding), "Node gke-seeded-b-default-pool-4176890d-ghost names no instance in the project (providerID gce://haoxuw-gke-dev/us-central1-a/gke-seeded-b-default-pool-4176890d-ghost; Ready=Unknown; unschedulable=true): a pool upgrade waits on it; delete the Node object: `kubectl delete node gke-seeded-b-default-pool-4176890d-ghost`")
        self.assertEqual(result["notes"], ["1 orphan Node(s) (3 nodes, 19 instances in the project)"])
        # The join is on the zone too: the same instance name in another zone is not this node's instance
        # (cordoned, so it has the orphan shape rather than a newcomer's).
        moved = copy.deepcopy(recorded("nodes_seeded_b.json")[0])
        moved["spec"]["providerID"] = moved["spec"]["providerID"].replace("us-central1-a", "us-central1-b")
        moved["spec"]["unschedulable"] = True
        result = orphan_node_objects.evaluate(record, {}, read(items=[moved]), None, context_for(record))
        self.assertEqual([f["node"] for f in result["blocking"]], [moved["metadata"]["name"]])
        # A node with no providerID is joined by its name alone, as the report's older node records are.
        bare = node("gke-seeded-b-no-surge-pool-235d7913-2jpr", "no-surge-pool")
        self.assertEqual(orphan_node_objects.evaluate(record, {}, read(items=[bare]), None, context_for(record))["blocking"], [])
        self.assertEqual(len(orphan_node_objects.evaluate(record, {}, read(items=[node("gke-nowhere-1", "p", ready=False)]), None, context_for(record))["blocking"]), 1)

    def test_a_ready_uncordoned_node_the_instance_list_does_not_carry_is_a_note_not_a_blocker(self):
        # The instance list is read once per project at its first graded member, minutes from any member's
        # node read at most: a node that joined or left in between (a scale-up, a surge node) is Ready and
        # uncordoned with no instance in the list.
        record = seeded_b()
        newcomer = copy.deepcopy(recorded("nodes_seeded_b.json")[0])
        newcomer["metadata"]["name"] = "gke-seeded-b-default-pool-4176890d-new1"
        newcomer["spec"]["providerID"] = "gce://haoxuw-gke-dev/us-central1-a/gke-seeded-b-default-pool-4176890d-new1"
        reads = project_reads()
        reads[shared.PROJECT_READ_INSTANCES][shared.PROJECT_READ_AT_KEY] = "2026-10-20T11:58:00Z"
        result = orphan_node_objects.evaluate(record, {}, seeded_b_read([newcomer]), None, context_for(record, reads=reads))
        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["notes"], ["0 orphan Node(s) (3 nodes, 19 instances in the project)", "1 Ready, uncordoned Node(s) not in the instance list read at 2026-10-20T11:58:00Z (gke-seeded-b-default-pool-4176890d-new1): a node that joined or left between that list and this node read is not an orphan; the next run grades it"])
        # A context that does not say when the list was read says so.
        self.assertIn("read at an unrecorded time", orphan_node_objects.evaluate(record, {}, seeded_b_read([newcomer]), None, context_for(record))["notes"][1])
        # Cordoned, or not Ready True: the orphan shape, a blocker whatever the list's age.
        cordoned = copy.deepcopy(newcomer)
        cordoned["spec"]["unschedulable"] = True
        not_ready = copy.deepcopy(newcomer)
        not_ready["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        for ghost in (cordoned, not_ready):
            result = orphan_node_objects.evaluate(record, {}, seeded_b_read([ghost]), None, context_for(record, reads=reads))
            self.assertEqual([f["node"] for f in result["blocking"]], ["gke-seeded-b-default-pool-4176890d-new1"])
            self.assertEqual(result["notes"], ["1 orphan Node(s) (3 nodes, 19 instances in the project)"])

    def test_a_node_created_after_the_instance_list_is_a_note_whatever_its_readiness(self):
        # A surge node registers Ready False for its first seconds: created after the list's read time, it is
        # a newcomer, not an orphan; the same node created before the list with no instance is the orphan.
        record = seeded_b()
        reads = project_reads()
        reads[shared.PROJECT_READ_INSTANCES][shared.PROJECT_READ_AT_KEY] = "2026-10-20T11:58:00Z"
        booting = copy.deepcopy(recorded("nodes_seeded_b.json")[0])
        booting["metadata"]["name"] = "gke-seeded-b-default-pool-4176890d-boot"
        booting["metadata"]["creationTimestamp"] = "2026-10-20T11:59:30Z"
        booting["spec"]["providerID"] = "gce://haoxuw-gke-dev/us-central1-a/gke-seeded-b-default-pool-4176890d-boot"
        booting["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        result = orphan_node_objects.evaluate(record, {}, seeded_b_read([booting]), None, context_for(record, reads=reads))
        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["notes"], ["0 orphan Node(s) (3 nodes, 19 instances in the project)", "1 Node(s) created after the instance list was read at 2026-10-20T11:58:00Z (gke-seeded-b-default-pool-4176890d-boot (created 2026-10-20T11:59:30Z)): a node that registered after the list is not an orphan, whatever its readiness; the next run grades it"])
        ghost = copy.deepcopy(booting)
        ghost["metadata"]["creationTimestamp"] = "2026-10-20T11:40:00Z"
        result = orphan_node_objects.evaluate(record, {}, seeded_b_read([ghost]), None, context_for(record, reads=reads))
        self.assertEqual([(f["node"], f["ready"]) for f in result["blocking"]], [("gke-seeded-b-default-pool-4176890d-boot", "False")])
        self.assertEqual(result["notes"], ["1 orphan Node(s) (3 nodes, 19 instances in the project)"])
        # A context that does not say when the list was read, or a node without the timestamp, falls back to the readiness shape.
        self.assertEqual(len(orphan_node_objects.evaluate(record, {}, seeded_b_read([booting]), None, context_for(record))["blocking"]), 1)
        del booting["metadata"]["creationTimestamp"]
        self.assertEqual(len(orphan_node_objects.evaluate(record, {}, seeded_b_read([booting]), None, context_for(record, reads=reads))["blocking"]), 1)
        self.assertEqual(shared.node_created_at(recorded("nodes_seeded_b.json")[0]), datetime(2026, 10, 7, 18, 35, 15, tzinfo=timezone.utc))

    def test_an_unread_node_list_or_instance_list_is_unknown(self):
        record = seeded_b()
        result = orphan_node_objects.evaluate(record, {}, read(items=[], read_errors={"node": "nodes is forbidden"}), None, context_for(record))
        self.assertEqual(result["unknown"], ["the Nodes were not read (nodes is forbidden), so no Node could be checked against the instances"])
        failed = project_reads(**{shared.PROJECT_READ_INSTANCES: failed_read("instances list cut at the cap")})
        self.assertEqual(orphan_node_objects.evaluate(record, {}, seeded_b_read(), None, context_for(record, reads=failed))["unknown"], ["instances list cut at the cap"])
        self.assertEqual((orphan_node_objects.RULE_ID, orphan_node_objects.ENTRY, orphan_node_objects.CAN_BLOCK), ("orphan-node-objects", 22, True))


class ClusterStatusAgeRuleTest(unittest.TestCase):
    # seeded-b's newest recorded operation: the UPGRADE_NODES on default-pool (DONE, 2026-10-07T18:34:18Z), eleven seconds after its UPDATE_CLUSTER.
    NEWEST_OPERATION = "operation-1791398058861-79f9472d-6079-48fc-838f-2e47f8b578dc"
    NEWEST_TEXT = f"{NEWEST_OPERATION} (UPGRADE_NODES on default-pool, DONE, started 2026-10-07T18:34:18.86189527Z, 12d 17h ago)"
    POOL_TARGET = "https://container.googleapis.com/v1/projects/196584622296/zones/us-central1-a/clusters/seeded-b/nodePools/default-pool"
    CLUSTER_TARGET = "https://container.googleapis.com/v1/projects/196584622296/zones/us-central1-a/clusters/seeded-b"

    def in_flight(self, age, target=POOL_TARGET, kind="UPGRADE_NODES", name="operation-in-flight", status="RUNNING"):
        """An operation started `age` before the recorded instant, RUNNING unless said otherwise."""
        return {"name": name, "operationType": kind, "status": status, "startTime": (RECORDED_AT - age).strftime("%Y-%m-%dT%H:%M:%S.000000Z"), "targetLink": target}

    def reads_with(self, *operations):
        return project_reads(**{shared.PROJECT_READ_OPERATIONS: recorded("project_operations.json") + list(operations)})

    DESCRIBE_COMMAND = ["gcloud", "container", "clusters", "describe", "seeded-b", "--location", "us-central1-a", "--project", DEV_PROJECT, "--format=json(status,statusMessage)"]

    RELIST_COMMAND = ["gcloud", "container", "operations", "list", f"--project={DEV_PROJECT}", "--location=us-central1-a", "--filter=targetLink~/clusters/seeded-b($|/nodePools/)", "--format=json(name,operationType,status,startTime,endTime,targetLink)"]

    def describing(self, context, status, message=None, rc=0, stdout=None, operations=None, list_rc=0):
        """A runner that answers the rule's `clusters describe` with `status` and its `operations list`
        for the cluster with `operations` (the project's recorded list by default), recording the calls."""
        calls = []

        def run(cmd, timeout=None, env=None):
            calls.append(cmd)
            if cmd[:4] == ["gcloud", "container", "operations", "list"]:
                if list_rc:
                    return list_rc, "", "ERROR: (gcloud.container.operations.list) forbidden"
                return 0, json.dumps(recorded("project_operations.json") if operations is None else operations), ""
            if rc:
                return rc, "", "ERROR: (gcloud.container.clusters.describe) forbidden"
            return 0, json.dumps({"status": status, **({"statusMessage": message} if message else {})}) if stdout is None else stdout, ""

        context[shared.CONTEXT_RUN_CMD] = run
        return calls

    def test_a_running_cluster_notes_its_newest_operation_node_pool_ones_included(self):
        record = seeded_b()
        result = cluster_status_age.evaluate(record, {}, None, None, context_for(record))
        self.assertEqual((result["blocking"], result["unknown"]), ([], []))
        self.assertEqual(result["notes"], [f"status RUNNING; newest operation {self.NEWEST_TEXT}"])

    def test_a_running_node_pool_operation_is_a_note_under_the_threshold_and_the_holder_past_it(self):
        record = {**seeded_b(), "status": "RECONCILING"}
        young = self.reads_with(self.in_flight(timedelta(minutes=10)))
        result = cluster_status_age.evaluate(record, {}, None, None, context_for(record, reads=young))
        self.assertEqual((result["blocking"], result["unknown"]), ([], []))
        self.assertEqual(result["notes"], ["status RECONCILING for 10m on operation-in-flight (UPGRADE_NODES on default-pool, RUNNING since 2026-10-20T11:50:00.000000Z); under the 4h threshold a reconcile in progress is not a blocker"])
        old = self.reads_with(self.in_flight(timedelta(hours=5)))
        result = cluster_status_age.evaluate(record, {}, None, None, context_for(record, reads=old))
        self.assertEqual(len(result["blocking"]), 1)
        finding = result["blocking"][0]
        self.assertEqual((finding["status"], finding["operation"], finding["operation_type"], finding["pool"], finding["age_seconds"], finding["threshold_seconds"]), ("RECONCILING", "operation-in-flight", "UPGRADE_NODES on default-pool", "default-pool", 5 * 3600, 4 * 3600))
        text = cluster_status_age.describe(finding)
        self.assertTrue(text.startswith("seeded-b has been RECONCILING for 5h on operation-in-flight (UPGRADE_NODES on default-pool, RUNNING since 2026-10-20T07:00:00.000000Z), past the 4h threshold; freeze changes; past a few hours open a support case quoting the operation id"), text)
        self.assertIn("Stuck operation section", text)
        # The holder is the newest RUNNING operation, a cluster one included; a DONE operation
        # newer than it holds nothing.
        both = self.reads_with(self.in_flight(timedelta(hours=6), target=self.CLUSTER_TARGET, kind="UPDATE_CLUSTER", name="operation-cluster"), self.in_flight(timedelta(hours=5)))
        finding = cluster_status_age.evaluate(record, {}, None, None, context_for(record, reads=both))["blocking"][0]
        self.assertEqual((finding["operation"], finding["pool"]), ("operation-in-flight", "default-pool"))
        cluster_only = self.reads_with(self.in_flight(timedelta(hours=6), target=self.CLUSTER_TARGET, kind="UPDATE_CLUSTER", name="operation-cluster"))
        finding = cluster_status_age.evaluate(record, {}, None, None, context_for(record, reads=cluster_only))["blocking"][0]
        self.assertEqual((finding["operation"], finding["operation_type"], finding["pool"]), ("operation-cluster", "UPDATE_CLUSTER", None))
        done_newer = self.reads_with(self.in_flight(timedelta(hours=5)), self.in_flight(timedelta(minutes=1), name="operation-done", status="DONE"))
        finding = cluster_status_age.evaluate(record, {}, None, None, context_for(record, reads=done_newer))["blocking"][0]
        self.assertEqual((finding["operation"], finding["age_seconds"]), ("operation-in-flight", 5 * 3600))
        # Another cluster's pool operation is not this cluster's holder: with nothing of its own RUNNING, the describe decides.
        other = self.reads_with(self.in_flight(timedelta(hours=5), target=self.POOL_TARGET.replace("clusters/seeded-b/", "clusters/seeded-a/")))
        context = context_for(record, reads=other)
        self.describing(context, "RECONCILING", operations=[])
        self.assertIsNone(cluster_status_age.evaluate(record, {}, None, None, context)["blocking"][0]["operation"])

    def test_a_degraded_cluster_with_only_done_operations_blocks_quoting_its_status_message(self):
        # The list said DEGRADED and nothing RUNNING holds it: the rule describes the cluster once and grades the fresh status.
        record = {**seeded_b(), "status": "DEGRADED", "statusMessage": "Cluster certificate authority expired; rotate credentials"}
        context = context_for(record)
        calls = self.describing(context, "DEGRADED", "Cluster certificate authority expired; rotate credentials")
        result = cluster_status_age.evaluate(record, {}, None, None, context)
        self.assertEqual(calls, [self.DESCRIBE_COMMAND, self.RELIST_COMMAND])  # the status confirmed, then this cluster's operations now
        self.assertEqual(len(result["blocking"]), 1)
        finding = result["blocking"][0]
        self.assertEqual((finding["operation"], finding["status"], finding["listed_status"], finding["status_message"], finding["newest"]["operation"], finding["newest"]["status"]), (None, "DEGRADED", "DEGRADED", "Cluster certificate authority expired; rotate credentials", self.NEWEST_OPERATION, "DONE"))
        self.assertEqual(cluster_status_age.describe(finding), f"seeded-b is DEGRADED with no operation RUNNING on it or its node pools (statusMessage: Cluster certificate authority expired; rotate credentials; newest operation: {self.NEWEST_TEXT}); {cluster_status_age.NO_HOLDER_ACTION}")
        # No statusMessage and no operation listed at all: both said, from the fresh status.
        bare = {**seeded_b(), "status": "RECONCILING"}
        none_listed = project_reads(**{shared.PROJECT_READ_OPERATIONS: []})
        context = context_for(bare, reads=none_listed)
        self.describing(context, "RECONCILING", operations=[])
        finding = cluster_status_age.evaluate(bare, {}, None, None, context)["blocking"][0]
        self.assertEqual(cluster_status_age.describe(finding), "seeded-b is RECONCILING with no operation RUNNING on it or its node pools (statusMessage: none; newest operation: none listed); " + cluster_status_age.NO_HOLDER_ACTION)
        context = context_for(seeded_b(), reads=none_listed)
        calls = self.describing(context, "RUNNING")
        self.assertEqual(cluster_status_age.evaluate(seeded_b(), {}, None, None, context)["notes"], ["status RUNNING; no operation of this cluster or its node pools is listed"])
        self.assertEqual(calls, [])  # a RUNNING cluster is not described

    def test_an_operation_that_started_after_the_projects_list_is_found_by_the_relist(self):
        # RECONCILING at list time, the holder DONE before the project's list, and the next operation (a chained pool
        # upgrade) started after it: the describe confirms RECONCILING, and this cluster's own list names the holder.
        record = {**seeded_b(), "status": "RECONCILING"}
        snapshot = self.reads_with(self.in_flight(timedelta(minutes=20), status="DONE", name="operation-done-before-the-list"))
        young = recorded("project_operations.json") + [self.in_flight(timedelta(minutes=6), name="operation-next-pool", target=self.POOL_TARGET.replace("default-pool", "no-surge-pool"))]
        context = context_for(record, reads=snapshot)
        calls = self.describing(context, "RECONCILING", operations=young)
        result = cluster_status_age.evaluate(record, {}, None, None, context)
        self.assertEqual(calls, [self.DESCRIBE_COMMAND, self.RELIST_COMMAND])
        self.assertEqual((result["blocking"], result["unknown"]), ([], []))
        self.assertEqual(result["notes"], ["status RECONCILING for 6m on operation-next-pool (UPGRADE_NODES on no-surge-pool, RUNNING since 2026-10-20T11:54:00.000000Z); under the 4h threshold a reconcile in progress is not a blocker"])
        # The same holder past the threshold: the blocker names it, with the list status beside the described one.
        old = recorded("project_operations.json") + [self.in_flight(timedelta(hours=5), name="operation-next-pool", target=self.POOL_TARGET.replace("default-pool", "no-surge-pool"))]
        context = context_for(record, reads=snapshot)
        self.describing(context, "RECONCILING", operations=old)
        finding = cluster_status_age.evaluate(record, {}, None, None, context)["blocking"][0]
        self.assertEqual((finding["operation"], finding["pool"], finding["age_seconds"], finding["listed_status"]), ("operation-next-pool", "no-surge-pool", 5 * 3600, "RECONCILING"))
        self.assertTrue(cluster_status_age.describe(finding).startswith("seeded-b has been RECONCILING for 5h on operation-next-pool (UPGRADE_NODES on no-surge-pool, RUNNING since"))
        # Nothing RUNNING in the fresh list either: the statusMessage blocker, as before.
        context = context_for(record, reads=snapshot)
        self.describing(context, "RECONCILING", operations=recorded("project_operations.json"))
        self.assertIsNone(cluster_status_age.evaluate(record, {}, None, None, context)["blocking"][0]["operation"])
        # The re-list failing: unknown, naming the described status and why.
        context = context_for(record, reads=snapshot)
        self.describing(context, "RECONCILING", list_rc=1)
        self.assertEqual(cluster_status_age.evaluate(record, {}, None, None, context)["unknown"], ["status RECONCILING now with no RUNNING operation in the project's list; `gcloud container operations list` for the cluster could not say what holds it (ERROR: (gcloud.container.operations.list) forbidden), so that is unknown"])
        # A described RUNNING needs no re-list.
        context = context_for(record, reads=snapshot)
        calls = self.describing(context, "RUNNING", list_rc=1)
        self.assertEqual(cluster_status_age.evaluate(record, {}, None, None, context)["unknown"], [])
        self.assertEqual(calls, [self.DESCRIBE_COMMAND])

    def test_a_list_status_the_operations_list_postdates_is_confirmed_before_it_blocks(self):
        # RECONCILING at clusters-list time, the holding operation DONE by the time the operations list was made (the
        # list is made at the project's first member): describe says RUNNING, and the rule files a note, not a blocker.
        record = {**seeded_b(), "status": "RECONCILING"}
        just_done = self.reads_with(self.in_flight(timedelta(minutes=9), status="DONE", name="operation-just-done"))
        context = context_for(record, reads=just_done)
        calls = self.describing(context, "RUNNING")
        result = cluster_status_age.evaluate(record, {}, None, None, context)
        self.assertEqual((result["blocking"], result["unknown"]), ([], []))
        self.assertEqual(result["notes"], ["status RUNNING now (RECONCILING at list time; newest operation operation-just-done (UPGRADE_NODES on default-pool, DONE, started 2026-10-20T11:51:00.000000Z, 9m ago))"])
        self.assertEqual(calls, [self.DESCRIBE_COMMAND])
        # Still not RUNNING when described, and nothing RUNNING in this cluster's own list: the blocker, from the described status and message.
        context = context_for(record, reads=just_done)
        self.describing(context, "DEGRADED", "Node pool default-pool needs repair", operations=[])
        finding = cluster_status_age.evaluate(record, {}, None, None, context)["blocking"][0]
        self.assertEqual((finding["status"], finding["listed_status"], finding["status_message"]), ("DEGRADED", "RECONCILING", "Node pool default-pool needs repair"))
        self.assertTrue(cluster_status_age.describe(finding).startswith("seeded-b is DEGRADED with no operation RUNNING on it or its node pools (statusMessage: Node pool default-pool needs repair; newest operation: operation-just-done"))
        # The describe failing, or answering without a status: unknown, naming the list status and why.
        context = context_for(record, reads=just_done)
        self.describing(context, "RUNNING", rc=1)
        self.assertEqual(cluster_status_age.evaluate(record, {}, None, None, context)["unknown"], ["status RECONCILING at list time with no RUNNING operation on it or its node pools; `gcloud container clusters describe` could not confirm it (ERROR: (gcloud.container.clusters.describe) forbidden), so whether it still stands is unknown"])
        context = context_for(record, reads=just_done)
        self.describing(context, "RUNNING", stdout="{}")
        self.assertIn("could not confirm it (the response carries no status)", cluster_status_age.evaluate(record, {}, None, None, context)["unknown"][0])
        context = context_for(record, reads=just_done)
        context.pop(shared.CONTEXT_RUN_CMD)
        self.assertIn("could not confirm it (the context carries no command runner)", cluster_status_age.evaluate(record, {}, None, None, context)["unknown"][0])
        # A holder RUNNING past the threshold is graded from the operations list alone: no describe.
        context = context_for(record, reads=self.reads_with(self.in_flight(timedelta(hours=5))))
        calls = self.describing(context, "RUNNING")
        self.assertEqual(len(cluster_status_age.evaluate(record, {}, None, None, context)["blocking"]), 1)
        self.assertEqual(calls, [])

    def test_an_unread_operations_list_is_unknown_only_when_the_status_needs_it(self):
        record = seeded_b()
        failed = project_reads(**{shared.PROJECT_READ_OPERATIONS: failed_read("operations list failed (1): forbidden")})
        self.assertEqual(cluster_status_age.evaluate(record, {}, None, None, context_for(record, reads=failed))["notes"], ["status RUNNING; the operations were not read (operations list failed (1): forbidden)"])
        result = cluster_status_age.evaluate({**record, "status": "ERROR"}, {}, None, None, context_for(record, reads=failed))
        self.assertEqual(result["unknown"], ["status ERROR; the operations were not read (operations list failed (1): forbidden), so what holds it and for how long is unknown"])
        # A RUNNING operation with no readable start: unknown, not a guess.
        unreadable = self.reads_with({**self.in_flight(timedelta(hours=5)), "startTime": "yesterday"})
        self.assertEqual(cluster_status_age.evaluate({**record, "status": "RECONCILING"}, {}, None, None, context_for(record, reads=unreadable))["unknown"], ["status RECONCILING; the RUNNING operation operation-in-flight carries no readable startTime ('yesterday'), so its age is unknown"])
        self.assertEqual((cluster_status_age.RULE_ID, cluster_status_age.ENTRY, cluster_status_age.CAN_BLOCK), ("cluster-status-age", 24, True))


class TargetPatchValidRuleTest(unittest.TestCase):
    def test_a_valid_target_and_a_listed_control_plane_are_a_note(self):
        record = {**seeded_b(), "currentMasterVersion": "1.35.8-gke.1626001"}
        result = target_patch_valid.evaluate(record, {}, None, None, context_for(record, target_text="1.36.4-gke.1391002"))
        self.assertEqual((result["blocking"], result["risks"], result["unknown"]), ([], [], []))
        self.assertEqual(result["notes"], ["channel REGULAR; target 1.36.4-gke.1391002 valid; control plane 1.35.8-gke.1626001 listed in validVersions at us-central1-a; the target's move: control-plane move"])

    def test_a_withdrawn_target_is_a_blocker_and_a_dropped_running_patch_a_risk(self):
        # seeded-b as recorded: its master, 1.34.12-gke.1011000, is no longer among REGULAR's
        # validVersions, though validMasterVersions still carries it, which is the exemption
        # this rule refuses.
        record = seeded_b()
        config = recorded("server_config_us_central1_a.json")
        self.assertIn(record["currentMasterVersion"], config["validMasterVersions"])
        result = target_patch_valid.evaluate(record, {}, None, None, context_for(record, target_text="1.36.4-gke.1391002"))
        self.assertEqual(result["blocking"], [])
        # REGULAR still lists an older 1.34 patch (1.34.11-gke.1209000), which is no target for a
        # cluster on 1.34.12: the action names the next active version above it instead.
        regular = sorted((v for v in next(c for c in config["channels"] if c["channel"] == "REGULAR")["validVersions"] if target_patch_valid.parse_version(v) > target_patch_valid.parse_version("1.34.12-gke.1011000")), key=target_patch_valid.parse_version)
        self.assertEqual([(f["kind"], f["version"], f["newest"], f["next_version"]) for f in result["risks"]], [("control-plane", "1.34.12-gke.1011000", None, regular[0])])
        self.assertEqual(target_patch_valid.describe(result["risks"][0]), f"control plane 1.34.12-gke.1011000 is not among the REGULAR channel's validVersions at us-central1-a (the channel has moved past it, whatever another channel still lists); no active patch of 1.34 at or above it on the REGULAR channel; the next active version is {regular[0]}")
        self.assertEqual(result["notes"], ["channel REGULAR; target 1.36.4-gke.1391002 valid; control plane 1.34.12-gke.1011000 not listed in validVersions at us-central1-a; the target's move: control-plane move"])
        # A target the channel does not list, and no node version either (a 1.35 patch pulled
        # before the window): a blocker naming the newest active patch of its minor.
        self.assertNotIn("1.35.8-gke.1000000", config["validNodeVersions"])
        result = target_patch_valid.evaluate(record, {}, None, None, context_for(record, target_text="1.35.8-gke.1000000"))
        self.assertEqual([(f["kind"], f["version"], f["newest"]) for f in result["blocking"]], [("target", "1.35.8-gke.1000000", "1.35.8-gke.1626001")])
        self.assertEqual(target_patch_valid.describe(result["blocking"][0]), "target 1.35.8-gke.1000000 is not in the REGULAR channel's validVersions at us-central1-a, which the control plane's move to it needs: the channel refuses the move, and a withdrawn patch stays selectable; target the newest active patch of the minor on the REGULAR channel: 1.35.8-gke.1626001")
        self.assertIn("target 1.35.8-gke.1000000 withdrawn", result["notes"][0])
        # A target the channel does not list but validNodeVersions serves: valid for a pool move alone, so a blocker while the
        # control plane (1.34.12) has to move to it, naming the node list; valid once the control plane is at or above it.
        node_only = next(v for v in config["validNodeVersions"] if v not in next(c for c in config["channels"] if c["channel"] == "REGULAR")["validVersions"])
        result = target_patch_valid.evaluate(record, {}, None, None, context_for(record, target_text=node_only))
        self.assertEqual([(f["kind"], f["move"], f["in_node_list"]) for f in result["blocking"]], [("target", "control-plane move", True)])
        self.assertIn(f"target {node_only} is not in the REGULAR channel's validVersions at us-central1-a, which the control plane's move to it needs (validNodeVersions lists it, which serves a pool move alone)", target_patch_valid.describe(result["blocking"][0]))
        self.assertIn("the target's move: control-plane move", result["notes"][0])
        pools_only = {**record, "currentMasterVersion": node_only}
        result = target_patch_valid.evaluate(pools_only, {}, None, None, context_for(pools_only, target_text=node_only))
        self.assertEqual(result["blocking"], [])
        self.assertIn(f"target {node_only} valid", result["notes"][0])
        self.assertIn("the target's move: pool move", result["notes"][0])
        # A pool-only move to a target in neither list is the pool-move blocker, as before.
        result = target_patch_valid.evaluate({**record, "currentMasterVersion": "1.36.4-gke.9999999"}, {}, None, None, context_for(record, target_text="1.35.8-gke.1000000"))
        self.assertTrue(target_patch_valid.describe(result["blocking"][0]).startswith("target 1.35.8-gke.1000000 is in neither the REGULAR channel's validVersions nor validNodeVersions at us-central1-a"))

    def test_a_target_without_its_build_is_valid_when_the_channel_lists_a_build_of_it(self):
        # `--target-version 1.36.4` passes the report's validator (`MAJOR.MINOR.PATCH[-gke.BUILD]`) and the API
        # reads it as the patch's newest listed build: valid when REGULAR lists one, a blocker when it lists none.
        record = seeded_b()
        config = recorded("server_config_us_central1_a.json")
        regular = next(c for c in config["channels"] if c["channel"] == "REGULAR")["validVersions"]
        full = regular[0]
        short = full.rsplit("-gke.", 1)[0]
        self.assertNotIn(short, regular)
        result = target_patch_valid.evaluate(record, {}, None, None, context_for(record, target_text=short))
        self.assertEqual(result["blocking"], [])
        self.assertTrue(result["notes"][0].startswith(f"channel REGULAR; target {short} valid;"), result["notes"])
        self.assertEqual(target_patch_valid.evaluate(record, {}, None, None, context_for(record, target_text=full))["blocking"], [])
        # A full version is matched exactly: an unlisted build of a listed patch is withdrawn.
        self.assertEqual([f["version"] for f in target_patch_valid.evaluate(record, {}, None, None, context_for(record, target_text=short + "-gke.1"))["blocking"]], [short + "-gke.1"])
        # A patch of which no build is listed anywhere: withdrawn, naming the newest active patch of the minor.
        minor = ".".join(short.split(".")[:2])
        unlisted = f"{minor}.999"
        self.assertFalse(any(v.startswith(unlisted + "-gke.") for v in regular + config["validNodeVersions"]))
        result = target_patch_valid.evaluate(record, {}, None, None, context_for(record, target_text=unlisted))
        self.assertEqual([(f["kind"], f["version"], f["newest"]) for f in result["blocking"]], [("target", unlisted, None)])
        self.assertIn(f"target {unlisted} withdrawn", result["notes"][0])
        # A build the node list alone carries makes its patch valid too: a patch no channel lists, with one node build.
        node_only_config = copy.deepcopy(config)
        node_only_config["validNodeVersions"].insert(0, f"{minor}.998-gke.1")
        self.assertFalse(any(v.startswith(f"{minor}.998-gke.") for c in node_only_config["channels"] for v in c["validVersions"]))
        node_only_reads = project_reads(**{shared.LOCATION_READ_SERVER_CONFIG: node_only_config})
        # The node list serves a pool move alone: valid when the control plane is at the build already, a blocker while it has to move there.
        at_the_build = {**record, "currentMasterVersion": f"{minor}.998-gke.1"}
        self.assertEqual(target_patch_valid.evaluate(at_the_build, {}, None, None, context_for(at_the_build, target_text=f"{minor}.998", reads=node_only_reads))["blocking"], [])
        self.assertEqual([f["move"] for f in target_patch_valid.evaluate(record, {}, None, None, context_for(record, target_text=f"{minor}.998", reads=node_only_reads))["blocking"]], ["control-plane move"])
        # A build-less target counts as the newest build of its patch the roster lists: above a control plane at build 0 of
        # the patch (the control plane moves), not above one at that newest build (pools alone move).
        self.assertTrue(target_patch_valid.control_plane_moves(f"{minor}.998", f"{minor}.998-gke.0", node_only_config["validNodeVersions"]))
        self.assertFalse(target_patch_valid.control_plane_moves(f"{minor}.998", f"{minor}.998-gke.1", node_only_config["validNodeVersions"]))
        self.assertTrue(target_patch_valid.control_plane_moves(f"{minor}.998-gke.2", f"{minor}.998-gke.1", node_only_config["validNodeVersions"]))  # a full target is itself
        self.assertEqual([f["version"] for f in target_patch_valid.evaluate(record, {}, None, None, context_for(record, target_text=f"{minor}.998"))["blocking"]], [f"{minor}.998"])
        self.assertTrue(target_patch_valid.version_listed("1.36.4", ["1.36.4-gke.1495002"]))
        self.assertFalse(target_patch_valid.version_listed("1.36.4", ["1.36.40-gke.1"]))
        self.assertFalse(target_patch_valid.version_listed("1.36.4-gke.2", ["1.36.4-gke.1495002"]))
        self.assertFalse(target_patch_valid.version_listed(None, ["1.36.4-gke.1495002"]))
        # The one reading of a target's builds both rules grade through: the channel's list first, validNodeVersions
        # for a pool move alone, never for a control-plane move.
        roster = ["1.36.4-gke.1495002", "1.36.40-gke.1", "1.36.4-gke.1391002"]
        self.assertEqual(target_patch_valid.listed_builds("1.36.4", roster), ["1.36.4-gke.1495002", "1.36.4-gke.1391002"])
        self.assertEqual(target_patch_valid.listed_builds("1.36.4-gke.1391002", roster), ["1.36.4-gke.1391002"])
        self.assertEqual(target_patch_valid.listed_builds("1.36.4-gke.2", roster), [])
        self.assertEqual(target_patch_valid.listed_builds(None, roster), [])
        self.assertEqual(target_patch_valid.target_builds("1.36.4", roster, ["1.36.4-gke.1"], False), ["1.36.4-gke.1495002", "1.36.4-gke.1391002"])
        self.assertEqual(target_patch_valid.target_builds("1.36.998", roster, ["1.36.998-gke.1"], False), ["1.36.998-gke.1"])
        self.assertEqual(target_patch_valid.target_builds("1.36.998", roster, ["1.36.998-gke.1"], True), [])
        self.assertEqual(target_patch_valid.target_builds("1.36.997", roster, ["1.36.998-gke.1"], False), [])

    def test_no_channel_unknown_channel_and_a_failed_read(self):
        record = {**seeded_b(), "releaseChannel": {"channel": "UNSPECIFIED"}}
        result = target_patch_valid.evaluate(record, {}, None, None, context_for(record, target_text="1.36.4-gke.1391002"))
        self.assertEqual(result["risks"], [])  # validMasterVersions still carries the running patch
        self.assertTrue(result["notes"][0].startswith("channel no channel; target 1.36.4-gke.1391002 valid; control plane 1.34.12-gke.1011000 listed"))
        record = {**seeded_b(), "releaseChannel": {"channel": "WEEKLY"}}
        self.assertEqual(target_patch_valid.evaluate(record, {}, None, None, context_for(record))["unknown"], ["channel WEEKLY is not in get-server-config for us-central1-a, so the valid versions are unknown"])
        # A static cluster is graded against validMasterVersions, and its findings say so rather than naming a channel.
        static = {**seeded_b(), "releaseChannel": {"channel": "UNSPECIFIED"}}
        result = target_patch_valid.evaluate(static, {}, None, None, context_for(static, target_text="1.35.8-gke.1000000"))
        self.assertEqual(target_patch_valid.describe(result["blocking"][0]), "target 1.35.8-gke.1000000 is not in validMasterVersions (no channel) at us-central1-a, which the control plane's move to it needs: the channel refuses the move, and a withdrawn patch stays selectable; target the newest active patch of the minor on validMasterVersions (no channel): 1.35.8-gke.1919000")
        self.assertIn("control plane 1.34.12-gke.1011000 listed in validMasterVersions at us-central1-a", result["notes"][0])
        dropped = {**static, "currentMasterVersion": "1.34.1-gke.1"}
        risk = target_patch_valid.evaluate(dropped, {}, None, None, context_for(dropped))["risks"][0]
        self.assertTrue(target_patch_valid.describe(risk).startswith("control plane 1.34.1-gke.1 is not among validMasterVersions (no channel) at us-central1-a (the list no longer serves it); "), target_patch_valid.describe(risk))
        failed = project_reads(**{shared.LOCATION_READ_SERVER_CONFIG: failed_read("get-server-config failed for us-central1-a")})
        self.assertEqual(target_patch_valid.evaluate(seeded_b(), {}, None, None, context_for(seeded_b(), reads=failed))["unknown"], ["get-server-config failed for us-central1-a"])
        self.assertEqual(target_patch_valid.active_versions_above("1.35.0-gke.1", ["1.35.8-gke.1626001", "1.35.8-gke.1439001", "1.36.4-gke.1391002"]), ("1.35.8-gke.1626001", "1.35.8-gke.1439001"))
        self.assertEqual(target_patch_valid.active_versions_above("1.37.0-gke.1", ["1.36.4-gke.1391002"]), (None, None))
        self.assertEqual(target_patch_valid.action_text({"version": "1.37.0-gke.1", "channel": "REGULAR", "newest": None, "next_version": None}), "no active version at or above it on the REGULAR channel")
        self.assertEqual((target_patch_valid.RULE_ID, target_patch_valid.ENTRY, target_patch_valid.CAN_BLOCK), ("target-patch-valid", 25, True))


class ControlPlaneUpgradeHistoryRuleTest(unittest.TestCase):
    def host(self):
        return dev_host()

    def capacity_failure_at(self, location, name="capacity-test"):
        """A cluster at `location` with no window and one UPGRADE_MASTER on it that failed on capacity three days before RECORDED_AT, as the operations list spells them."""
        record = {**dev_host(), "name": name, "location": location}
        scope = "locations" if location.count("-") == 1 else "zones"
        operation = {"name": f"operation-{name}", "operationType": "UPGRADE_MASTER", "status": "DONE", "startTime": (RECORDED_AT - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000000Z"), "targetLink": f"https://container.googleapis.com/v1/projects/196584622296/{scope}/{location}/clusters/{name}", "error": {"message": f"Google Compute Engine does not have enough resources available to fulfill request: {location}."}}
        return record, project_reads(**{shared.PROJECT_READ_OPERATIONS: [operation]})

    def with_window(self, record, window):
        return {**record, "maintenancePolicy": {"window": window}}

    def test_the_recorded_capacity_failure_is_a_risk_with_the_window(self):
        record = self.host()
        result = control_plane_upgrade_history.evaluate(record, {}, None, None, context_for(record))
        self.assertEqual(len(result["risks"]), 1)
        finding = result["risks"][0]
        self.assertEqual((finding["operation_type"], finding["date"], finding["window"], finding["location"]), ("UPGRADE_MASTER", "2026-10-09", "none", "us-central1"))
        self.assertEqual(control_plane_upgrade_history.describe(finding), "UPGRADE_MASTER on 2026-10-09 failed on compute capacity in us-central1 (Try a different location, or try again later: Google Compute Engine does not have enough resources available to fulfill request: us-central1.); maintenance window: none; set a maintenance window in an off-peak hour for the region and retry the upgrade in it; keep upgrade notifications consumed so a failed attempt is a message the same day")
        # Nine operations: the host's control-plane upgrades and the UPGRADE_NODES on its pools, which name the pool.
        self.assertEqual(result["notes"], ["1 capacity failure(s) among 9 upgrade or repair operation(s) in the last 90 days; maintenance window none"])
        self.assertEqual(result["blocking"], [])
        self.assertEqual((finding["placement"], finding["window_opens"], finding["placement_text"]), ("none", None, None))
        # A window set in the region's busy hours (01:00 UTC is 19:00 in Iowa's standard time): still a risk, the window named and placed. Outside the lookback: not counted.
        windowed = self.with_window(record, {"recurringWindow": {"window": {"startTime": "2026-01-03T01:00:00Z", "endTime": "2026-01-03T05:00:00Z"}, "recurrence": "FREQ=WEEKLY;BYDAY=SA,SU"}})
        result = control_plane_upgrade_history.evaluate(windowed, {}, None, None, context_for(windowed))
        self.assertEqual((result["risks"][0]["window"], result["risks"][0]["placement"], result["risks"][0]["window_opens"]), ("recurring FREQ=WEEKLY;BYDAY=SA,SU from 2026-01-03T01:00:00Z", "busy", "19:00"))
        self.assertIn("; maintenance window: recurring FREQ=WEEKLY;BYDAY=SA,SU from 2026-01-03T01:00:00Z, which opens at 19:00 in us-central1 standard time, inside the busy hours 06:00 to 21:00; set a maintenance window in an off-peak hour for the region and retry the upgrade in it; keep upgrade notifications consumed so a failed attempt is a message the same day", control_plane_upgrade_history.describe(result["risks"][0]))
        self.assertEqual(result["unknown"], [])
        self.assertEqual(result["notes"], ["1 capacity failure(s) among 9 upgrade or repair operation(s) in the last 90 days; maintenance window recurring FREQ=WEEKLY;BYDAY=SA,SU from 2026-01-03T01:00:00Z"])
        later = control_plane_upgrade_history.evaluate(record, {}, None, None, context_for(record, at=RECORDED_AT + timedelta(days=91)))
        self.assertEqual(later["notes"], ["0 capacity failure(s) among 0 upgrade or repair operation(s) in the last 90 days; maintenance window none"])

    def test_seeded_b_has_no_capacity_failure_and_a_daily_window(self):
        record = seeded_b()
        result = control_plane_upgrade_history.evaluate(record, {}, None, None, context_for(record))
        self.assertEqual(result["risks"], [])
        # Two operations: the UPGRADE_MASTER on the cluster and the UPGRADE_NODES on default-pool.
        self.assertEqual(result["notes"], ["0 capacity failure(s) among 2 upgrade or repair operation(s) in the last 90 days; maintenance window daily at 03:00 UTC for PT4H0M0S"])
        # The recorded capacity message on a node-pool upgrade: a surge node needs the capacity too, and the finding names the pool. seeded-b's
        # daily window opens at 03:00 UTC, 21:00 in Iowa's standard time, which is off-peak: the failure is a note asking for the retry in that
        # window, not a risk asking for a window the cluster has; the count line still counts it.
        stockout = {"name": "operation-pool-stockout", "operationType": "UPGRADE_NODES", "status": "DONE", "startTime": (RECORDED_AT - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000000Z"), "targetLink": "https://container.googleapis.com/v1/projects/196584622296/zones/us-central1-a/clusters/seeded-b/nodePools/default-pool", "error": {"message": "Try a different location, or try again later: Google Compute Engine does not have enough resources available to fulfill request: us-central1-a."}}
        reads = project_reads(**{shared.PROJECT_READ_OPERATIONS: recorded("project_operations.json") + [stockout]})
        result = control_plane_upgrade_history.evaluate(record, {}, None, None, context_for(record, reads=reads))
        self.assertEqual(result["risks"], [])
        self.assertEqual(result["unknown"], [])
        self.assertEqual(result["notes"], [
            "1 capacity failure(s) among 3 upgrade or repair operation(s) in the last 90 days; maintenance window daily at 03:00 UTC for PT4H0M0S",
            "UPGRADE_NODES on default-pool on 2026-10-17 failed on compute capacity in us-central1-a (Try a different location, or try again later: Google Compute Engine does not have enough resources available to fulfill request: us-central1-a.); retry the upgrade in the maintenance window (daily at 03:00 UTC for PT4H0M0S, which opens at 21:00 in us-central1 standard time, off-peak); keep upgrade notifications consumed so a failed attempt is a message the same day",
        ])
        # The same stockout under a window that opens in the busy hours (14:00 UTC is 08:00 in Iowa's standard time) is the risk, placed.
        busy = self.with_window(record, {"dailyMaintenanceWindow": {"startTime": "14:00", "duration": "PT4H0M0S"}})
        result = control_plane_upgrade_history.evaluate(busy, {}, None, None, context_for(busy, reads=reads))
        self.assertEqual([(f["operation"], f["operation_type"], f["pool"], f["date"], f["placement"], f["window_opens"]) for f in result["risks"]], [("operation-pool-stockout", "UPGRADE_NODES", "default-pool", "2026-10-17", "busy", "08:00")])
        self.assertTrue(control_plane_upgrade_history.describe(result["risks"][0]).startswith("UPGRADE_NODES on default-pool on 2026-10-17 failed on compute capacity in us-central1-a (Try a different location"))
        self.assertTrue(control_plane_upgrade_history.describe(result["risks"][0]).endswith("; maintenance window: daily at 14:00 UTC for PT4H0M0S, which opens at 08:00 in us-central1 standard time, inside the busy hours 06:00 to 21:00; set a maintenance window in an off-peak hour for the region and retry the upgrade in it; keep upgrade notifications consumed so a failed attempt is a message the same day"))
        self.assertEqual(result["notes"], ["1 capacity failure(s) among 3 upgrade or repair operation(s) in the last 90 days; maintenance window daily at 14:00 UTC for PT4H0M0S"])
        for message in ("ZONE_RESOURCE_POOL_EXHAUSTED", "Stockout in zone", "Insufficient quota"):
            self.assertTrue(control_plane_upgrade_history.names_capacity(message), message)
        self.assertFalse(control_plane_upgrade_history.names_capacity("operation aborted by the user"))
        failed = project_reads(**{shared.PROJECT_READ_OPERATIONS: failed_read("operations list failed (1): forbidden")})
        self.assertEqual(control_plane_upgrade_history.evaluate(record, {}, None, None, context_for(record, reads=failed))["unknown"], ["the operations were not read (operations list failed (1): forbidden), so the upgrade history is unknown"])
        self.assertEqual((control_plane_upgrade_history.RULE_ID, control_plane_upgrade_history.ENTRY, control_plane_upgrade_history.CAN_BLOCK), ("control-plane-upgrade-history", 23, False))

    def test_the_window_is_placed_in_the_region_s_standard_time(self):
        rule = control_plane_upgrade_history
        self.assertEqual((rule.region_of("us-central1-a"), rule.region_of("us-central1"), rule.region_of("europe-west1-b")), ("us-central1", "us-central1", "europe-west1"))
        self.assertEqual((rule.BUSY_HOURS_START, rule.BUSY_HOURS_END, rule.BUSY_HOURS_TEXT), (6, 21, "06:00 to 21:00"))
        # Every table row is a region (no zone letter) with a plausible standard offset.
        for region, offset in rule.REGION_UTC_OFFSET_HOURS.items():
            self.assertEqual(rule.region_of(region), region, region)
            self.assertTrue(-12 <= offset <= 14 and (offset * 2) == int(offset * 2), (region, offset))

        def grade(location, window):
            record, reads = self.capacity_failure_at(location)
            record = self.with_window(record, window) if window is not None else record
            result = rule.evaluate(record, {}, None, None, context_for(record, reads=reads))
            self.assertEqual(result["blocking"], [])
            finding = result["risks"][0] if result["risks"] else None
            return (finding["placement"], finding["window_opens"]) if finding else ("note", result["notes"][1]), result

        daily = lambda start: {"dailyMaintenanceWindow": {"startTime": start, "duration": "PT4H0M0S"}}  # noqa: E731
        # Tokyo: 03:00 UTC is noon, a busy hour; 15:00 UTC is midnight, off-peak. The same window is graded by the region it serves.
        self.assertEqual(grade("asia-northeast1-a", daily("03:00"))[0], ("busy", "12:00"))
        self.assertEqual(grade("asia-northeast1", daily("15:00"))[0][0], "note")
        # A half-hour offset (Mumbai, +5:30) and the band's edges: 20:30 is busy, 21:00 is not, 05:59 is not, 06:00 is.
        self.assertEqual(grade("asia-south1", daily("15:00"))[0], ("busy", "20:30"))
        self.assertEqual(grade("asia-south1", daily("15:30"))[0][0], "note")
        self.assertEqual(grade("europe-west2", daily("05:59"))[0][0], "note")
        self.assertEqual(grade("europe-west2", daily("06:00"))[0], ("busy", "06:00"))
        # Oregon (-8) wraps past midnight: 06:00 UTC is 22:00 the evening before, off-peak; 03:00 UTC is 19:00, busy.
        self.assertEqual(grade("us-west1-b", daily("06:00"))[0][0], "note")
        self.assertEqual(grade("us-west1-b", daily("03:00"))[0], ("busy", "19:00"))
        # A recurring window's start in another offset is read as the instant it is: 22:00 at -05:00 is 03:00 UTC, 21:00 in Iowa, off-peak; the window prints as the record spells it.
        placement, result = grade("us-central1", {"recurringWindow": {"window": {"startTime": "2026-10-22T22:00:00-05:00", "endTime": "2026-10-23T02:00:00-05:00"}, "recurrence": "FREQ=DAILY"}})
        self.assertEqual(placement[0], "note")
        self.assertIn("retry the upgrade in the maintenance window (recurring FREQ=DAILY from 2026-10-22T22:00:00-05:00, which opens at 21:00 in us-central1 standard time, off-peak); keep upgrade notifications", placement[1])
        # A region the table does not list: the window is not placed, the failure stays a risk with the set-window action, and one unknown names the gap.
        placement, result = grade("nowhere-east1-c", daily("03:00"))
        self.assertEqual(placement, ("unplaced", None))
        self.assertTrue(rule.describe(result["risks"][0]).endswith("; maintenance window: daily at 03:00 UTC for PT4H0M0S, which is not placed against the busy hours 06:00 to 21:00 (no standard-time offset is known for region nowhere-east1); set a maintenance window in an off-peak hour for the region and retry the upgrade in it; keep upgrade notifications consumed so a failed attempt is a message the same day"), rule.describe(result["risks"][0]))
        self.assertEqual(result["unknown"], ["the maintenance window (daily at 03:00 UTC for PT4H0M0S) is not placed against the busy hours 06:00 to 21:00: no standard-time offset is known for region nowhere-east1; its capacity failure(s) stay risks with the set-window action"])
        # A start the rule cannot read is the same gap with its own reason; a window with no failure to grade costs no unknown.
        placement, result = grade("us-central1", daily("soon"))
        self.assertEqual(placement, ("unplaced", None))
        self.assertIn("which is not placed against the busy hours 06:00 to 21:00 (its start time 'soon' could not be read); set a maintenance window", rule.describe(result["risks"][0]))
        self.assertEqual(len(result["unknown"]), 1)
        clean = self.with_window({**dev_host(), "location": "nowhere-east1"}, daily("03:00"))
        result = rule.evaluate(clean, {}, None, None, context_for(clean))
        self.assertEqual((result["risks"], result["unknown"], result["notes"]), ([], [], ["0 capacity failure(s) among 0 upgrade or repair operation(s) in the last 90 days; maintenance window daily at 03:00 UTC for PT4H0M0S"]))
        # No window: no placement, the set-window action.
        placement, result = grade("us-central1", None)
        self.assertEqual(placement, ("none", None))
        self.assertIn("; maintenance window: none; set a maintenance window in an off-peak hour", rule.describe(result["risks"][0]))


class KnownIssuesMatrixRuleTest(unittest.TestCase):
    PAGE = "https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/known-issues"
    SPEC_KEYS = {"from", "to", "to_exclusive", "minor", "through_minor", "from_minor", "all_versions"}

    def test_the_inventory_lists_the_recorded_addons_by_image_and_tag(self):
        addons = known_issues_matrix.inventory(seeded_b_read())
        by_image = {(row["namespace"], row["kind"], row["name"], row["container"]): (row["image"], row["tag"]) for row in addons}
        self.assertEqual(by_image[("kube-system", "DaemonSet", "gke-metadata-server", "gke-metadata-server")], ("gke-metadata-server", "gke_metadata_server_20260105.01_p0"))
        self.assertEqual(by_image[("gmp-system", "Deployment", "gmp-operator", "operator")], ("operator", "v0.17.6-gke.0"))
        self.assertEqual(by_image[("kube-system", "DaemonSet", "pdcsi-node", "gce-pd-driver")], ("gcp-compute-persistent-disk-csi-driver", "v1.23.1-gke.37"))
        self.assertTrue(all(row["namespace"] in ("kube-system", "gmp-system") for row in addons))
        self.assertEqual(len(addons), len({tuple(sorted(row.items())) for row in addons}))
        self.assertEqual(known_issues_matrix.inventory(read(items=[])), [])
        self.assertIn("fluent-bit v0.0.422-gke.21", known_issues_matrix.inventory_cell(addons))
        self.assertEqual(known_issues_matrix.inventory_cell([]), "none")

    def test_the_shipped_table_loads_under_its_stated_grammar(self):
        table, error = known_issues_matrix.load_table()
        self.assertIsNone(error)
        self.assertEqual(set(table) >= {"generated", "source", "source_note", "newest_gke_version", "issues"}, True)
        self.assertEqual(table["source"], self.PAGE)
        self.assertIn("Inclusion rule", table["source_note"])
        self.assertIsNotNone(target_patch_valid.parse_version(table["newest_gke_version"]))
        ids = [issue["id"] for issue in table["issues"]]
        self.assertEqual(len(ids), len(set(ids)))
        for issue in table["issues"]:
            self.assertEqual(set(issue) >= {"id", "category", "summary", "identified", "fixed_in", "gke_versions", "addon", "addon_versions", "url"}, True, issue["id"])
            self.assertTrue(issue["url"].startswith(self.PAGE + "#"), issue["id"])
            self.assertTrue(issue["identified"] and issue["fixed_in"] and issue["summary"], issue["id"])
            self.assertTrue(issue["gke_versions"], issue["id"])
            for spec in issue["gke_versions"]:
                if isinstance(spec, str):
                    self.assertIsNotNone(target_patch_valid.parse_version(spec), (issue["id"], spec))
                    continue
                self.assertTrue(set(spec) <= self.SPEC_KEYS and set(spec) - {"to_exclusive"}, (issue["id"], spec))
                for key in ("from", "to"):
                    if key in spec:
                        self.assertIsNotNone(target_patch_valid.parse_version(spec[key]), (issue["id"], spec))
                for key in ("minor", "through_minor", "from_minor"):
                    if key in spec:
                        self.assertIsNotNone(known_issues_matrix._minor(spec[key]), (issue["id"], spec))
        # The two rows the first table carried, under the page's own anchors and cells.
        oom = next(i for i in table["issues"] if i["id"] == "gke-metadata-server-oomkilled")
        self.assertEqual((oom["addon"], oom["fixed_in"], len(oom["gke_versions"])), ("gke-metadata-server", "none listed", 9))
        self.assertTrue(all(v.startswith("1.32.3-gke.") for v in oom["gke_versions"]))
        pdcsi = next(i for i in table["issues"] if i["id"] == "excessive-volume-group-logging")
        self.assertEqual((pdcsi["addon"], pdcsi["gke_versions"], pdcsi["fixed_in"]), ("gcp-compute-persistent-disk-csi-driver", ["1.32.4-gke.1236007", "1.32.4-gke.1353001"], "1.32.4-gke.1353003 and later"))
        # The open specs, which a match grades as a note: the five rows with no fix and no upper bound, and tcpx-perf's 1.34-and-later spec beside its two bounded ones.
        open_rows = sorted(i["id"] for i in table["issues"] if any(known_issues_matrix.spec_is_open(s) for s in i["gke_versions"]))
        self.assertEqual(open_rows, ["gke-metrics-agent-ds-race-condition", "gke-ui-blocked-autorepair", "lustre-multinic-throughput", "metadata-server-connection-timeouts", "tcpx-perf", "unscheduled-pods-and-exhausted-quota-during-two-step-minor-upgrades-for-the-control-plane"])
        tcpx = next(i for i in table["issues"] if i["id"] == "tcpx-perf")
        self.assertEqual([known_issues_matrix.spec_is_open(s) for s in tcpx["gke_versions"]], [False, False, True])

    def test_the_recorded_cluster_matches_the_rows_the_page_lists_for_its_versions(self):
        # seeded-b as recorded: 1.34.12-gke.1011000, with gke-metadata-server, gke-metrics-agent and the PD CSI driver in its inventory.
        record = seeded_b()
        result = known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, context_for(record, target_text="1.36.4-gke.1391002"))
        self.assertEqual((result["blocking"], result["unknown"]), ([], []))
        running = {f["issue"] for f in result["risks"] if f["kind"] == "running"}
        target = {f["issue"] for f in result["risks"] if f["kind"] == "target"}
        # "1.34 and earlier" with fixes in 1.35 and 1.36: a risk, add-on-less, matched by version alone.
        self.assertIn("image-screaming-too-many-links", running)
        self.assertNotIn("excessive-volume-group-logging", running)  # 1.32.4 builds only
        # The open specs ("All versions" with the add-on present or no add-on named, "1.33 and later", tcpx-perf's "1.34 and later", "1.35 and later" on the target): notes, not risks.
        open_rows = {"gke-metrics-agent-ds-race-condition", "gke-ui-blocked-autorepair", "unscheduled-pods-and-exhausted-quota-during-two-step-minor-upgrades-for-the-control-plane", "tcpx-perf"}
        self.assertEqual(open_rows & (running | target), set())
        self.assertNotIn("metadata-server-connection-timeouts", target)
        # A 1.36 range that ends at this very build (its fix) does not match the target.
        self.assertNotIn("workload-identity-post-token-bad-request", target)
        self.assertNotIn("gb300-bare-metal-workload-failures", target)
        table, _ = known_issues_matrix.load_table()
        self.assertEqual(result["notes"][0], f"{len(known_issues_matrix.inventory(seeded_b_read()))} add-on(s) inventoried in kube-system, gmp-system; {len(result['risks'])} known issue(s) match GKE 1.34.12-gke.1011000 or target 1.36.4-gke.1391002 (10 open issue(s) with no published fix noted); table generated {table['generated']} ({len(table['issues'])} issue(s), newest known version {table['newest_gke_version']})")
        self.assertEqual(result["notes"][1], f"4 open known issue(s) with no published fix on GKE 1.34.12-gke.1011000, noted and not graded as risks: unscheduled-pods-and-exhausted-quota-during-two-step-minor-upgrades-for-the-control-plane, tcpx-perf, gke-ui-blocked-autorepair, gke-metrics-agent-ds-race-condition; each is on {self.PAGE}")
        # An open row that matches both versions is noted under each, as a risk-graded row is counted under each.
        self.assertEqual(result["notes"][2], f"6 open known issue(s) with no published fix on GKE 1.36.4-gke.1391002, noted and not graded as risks: unscheduled-pods-and-exhausted-quota-during-two-step-minor-upgrades-for-the-control-plane, lustre-multinic-throughput, metadata-server-connection-timeouts, tcpx-perf, gke-ui-blocked-autorepair, gke-metrics-agent-ds-race-condition; each is on {self.PAGE}")
        # A risk-graded issue naming no add-on matches by version alone, and its text says so.
        streaming = next(f for f in result["risks"] if f["issue"] == "image-screaming-too-many-links" and f["kind"] == "running")
        self.assertEqual(known_issues_matrix.describe(streaming), f"known issue image-screaming-too-many-links (Image streaming fails because of symbolic links) applies to GKE 1.34.12-gke.1011000 (the page names no add-on); fixed in 1.35.6-gke.1005000 and later; 1.36.0-gke.2459000 and later: {self.PAGE}#image-screaming-too-many-links")

    def test_a_match_through_an_open_spec_is_a_note_and_through_a_bounded_spec_a_risk(self):
        # One row of each shape, both matching the running version: the open one is noted, the bounded one is a risk.
        table = {"generated": "2026-10-20", "source": self.PAGE, "newest_gke_version": "1.37.1-gke.1", "issues": [
            {"id": "open-row", "summary": "an open row", "gke_versions": [{"from_minor": "1.33"}], "addon": None, "addon_versions": [], "fixed_in": "TBD", "url": self.PAGE + "#open-row"},
            {"id": "bounded-row", "summary": "a bounded row", "gke_versions": [{"minor": "1.34", "to": "1.34.13-gke.1", "to_exclusive": True}], "addon": None, "addon_versions": [], "fixed_in": "1.34.13-gke.1 and later", "url": self.PAGE + "#bounded-row"},
            # tcpx-perf's shape: bounded specs with fixes for two minors, an open spec for the minors after them.
            {"id": "mixed-row", "summary": "a mixed row", "gke_versions": [{"minor": "1.33", "to": "1.33.5-gke.1", "to_exclusive": True}, {"from_minor": "1.34"}], "addon": None, "addon_versions": [], "fixed_in": "1.33.5-gke.1 and later for GKE 1.33", "url": self.PAGE + "#mixed-row"},
        ]}
        record = seeded_b()  # 1.34.12-gke.1011000
        with patch.object(known_issues_matrix, "load_table", return_value=(table, None)):
            result = known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, context_for(record))
        self.assertEqual([(f["kind"], f["issue"]) for f in result["risks"]], [("running", "bounded-row")])
        self.assertIn("1 known issue(s) match GKE 1.34.12-gke.1011000 or target none (2 open issue(s) with no published fix noted)", result["notes"][0])
        self.assertEqual(result["notes"][1], f"2 open known issue(s) with no published fix on GKE 1.34.12-gke.1011000, noted and not graded as risks: open-row, mixed-row; each is on {self.PAGE}")
        # On a version the mixed row's bounded spec covers, the row is a risk naming its fix.
        earlier = {**seeded_b(), "currentMasterVersion": "1.33.4-gke.1"}
        with patch.object(known_issues_matrix, "load_table", return_value=(table, None)):
            result = known_issues_matrix.evaluate(earlier, {}, seeded_b_read(), None, context_for(earlier))
        self.assertEqual([(f["kind"], f["issue"]) for f in result["risks"]], [("running", "mixed-row")])
        self.assertEqual(result["notes"][1], f"1 open known issue(s) with no published fix on GKE 1.33.4-gke.1, noted and not graded as risks: open-row; each is on {self.PAGE}")  # the open row still matches 1.33; the mixed row is the risk
        self.assertEqual([known_issues_matrix.spec_is_open(s) for s in ("1.33.4-gke.1", {"all_versions": True}, {"from_minor": "1.34"}, {"from": "1.35.3-gke.1"}, {"from": "1.35.3-gke.1", "to": "1.36.0-gke.1"}, {"minor": "1.32", "from": "1.32.4-gke.1"}, {"minor": "1.37"}, {"through_minor": "1.34"})], [False, True, True, True, False, False, False, False])

    def test_a_running_or_target_version_on_a_published_issue_is_a_risk_naming_the_fix(self):
        record = {**seeded_b(), "currentMasterVersion": "1.32.4-gke.1236007"}
        result = known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, context_for(record, target_text="1.32.4-gke.1353003"))
        running = {f["issue"]: f for f in result["risks"] if f["kind"] == "running"}
        self.assertTrue({"excessive-volume-group-logging", "dangling-status-nodependingresize-1-32"} <= set(running), set(running))
        self.assertEqual(known_issues_matrix.describe(running["excessive-volume-group-logging"]), f"known issue excessive-volume-group-logging (The PDCSI driver might log excessively) applies to GKE 1.32.4-gke.1236007 with gcp-compute-persistent-disk-csi-driver v1.23.1-gke.37; fixed in 1.32.4-gke.1353003 and later: {self.PAGE}#excessive-volume-group-logging")
        self.assertNotIn("excessive-volume-group-logging", {f["issue"] for f in result["risks"] if f["kind"] == "target"})  # the fixed build is not a match

        def targets(text):
            return {f["issue"] for f in known_issues_matrix.evaluate(seeded_b(), {}, seeded_b_read(), None, context_for(seeded_b(), target_text=text))["risks"] if f["kind"] == "target"}

        # A target inside the 1.33.0 range is a target risk; the range's exclusive end, the fixed build, is not.
        self.assertIn("dangling-status-nodependingresize-1-32", targets("1.33.0-gke.1000000"))
        self.assertNotIn("dangling-status-nodependingresize-1-32", targets("1.33.0-gke.1552000"))
        landing = next(f for f in known_issues_matrix.evaluate(seeded_b(), {}, seeded_b_read(), None, context_for(seeded_b(), target_text="1.33.0-gke.1000000"))["risks"] if f["issue"] == "dangling-status-nodependingresize-1-32")
        self.assertTrue(known_issues_matrix.describe(landing).startswith("target 1.33.0-gke.1000000 lands on known issue dangling-status-nodependingresize-1-32"))
        self.assertTrue(known_issues_matrix.describe(landing).endswith(f"; fixed in 1.32.4-gke.1353003 and later; 1.33.0-gke.1552000 and later; target a fixed patch or hold: {self.PAGE}#dangling-status-nodependingresize-1-32"))
        # A bounded row the page lists no fix for says so, rather than printing the placeholder.
        tpu_build = {**seeded_b(), "currentMasterVersion": "1.32.3-gke.1927000"}
        risks = known_issues_matrix.evaluate(tpu_build, {}, seeded_b_read(), None, context_for(tpu_build, target_text="1.32.3-gke.1785000"))["risks"]
        oom = next(f for f in risks if f["issue"] == "gke-metadata-server-oomkilled" and f["kind"] == "running")
        self.assertEqual(known_issues_matrix.describe(oom), f"known issue gke-metadata-server-oomkilled (gke-metadata-server is OOMKilled on TPU/GPU nodes) applies to GKE 1.32.3-gke.1927000 with gke-metadata-server gke_metadata_server_20260105.01_p0; no fix published: {self.PAGE}#gke-metadata-server-oomkilled")
        oom_target = next(f for f in risks if f["issue"] == "gke-metadata-server-oomkilled" and f["kind"] == "target")
        self.assertTrue(known_issues_matrix.describe(oom_target).endswith(f"; no fix published; hold: {self.PAGE}#gke-metadata-server-oomkilled"), known_issues_matrix.describe(oom_target))
        # Without the add-on in the inventory an add-on issue does not apply.
        without = read(items=[row for row in recorded("addons_seeded_b.json") if row["metadata"]["name"] not in ("pdcsi-node", "pdcsi-node-windows")])
        self.assertNotIn("excessive-volume-group-logging", {f["issue"] for f in known_issues_matrix.evaluate(record, {}, without, None, context_for(record))["risks"]})

    def test_a_target_without_its_build_is_graded_against_the_builds_the_channel_lists(self):
        # REGULAR lists 1.36.4-gke.1495002 and 1.36.4-gke.1391002 (recorded): `--target-version 1.36.4` is one of them.
        record = seeded_b()
        result = known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, context_for(record, target_text="1.36.4"))
        target_risks = {f["issue"] for f in result["risks"] if f["kind"] == "target"}
        # The row whose 1.36 spec ends at 1.36.4-gke.1391002 (its fix) affects neither build: no risk, no partial note (build 0 would have matched).
        self.assertNotIn("workload-identity-post-token-bad-request", target_risks)
        self.assertFalse(any("workload-identity-post-token-bad-request" in n for n in result["notes"][1:]))
        self.assertIn("or target 1.36.4 (", result["notes"][0])
        table = {"generated": "2026-10-20", "source": self.PAGE, "newest_gke_version": "1.37.1-gke.1", "issues": [
            {"id": "one-build", "summary": "one build of the patch", "gke_versions": ["1.36.4-gke.1391002"], "addon": None, "addon_versions": [], "fixed_in": "1.36.4-gke.1495002 and later", "url": self.PAGE + "#one-build"},
            {"id": "whole-minor", "summary": "the whole minor", "gke_versions": [{"minor": "1.36"}], "addon": None, "addon_versions": [], "fixed_in": "1.37.0-gke.1 and later", "url": self.PAGE + "#whole-minor"},
            {"id": "open-from", "summary": "open from the minor", "gke_versions": [{"from_minor": "1.36"}], "addon": None, "addon_versions": [], "fixed_in": "TBD", "url": self.PAGE + "#open-from"},
        ]}
        with patch.object(known_issues_matrix, "load_table", return_value=(table, None)):
            result = known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, context_for(record, target_text="1.36.4"))
        self.assertEqual([(f["kind"], f["issue"], f["version"]) for f in result["risks"]], [("target", "whole-minor", "1.36.4")])  # every listed build affected: the target's risk
        self.assertIn("1 known issue(s) match GKE 1.34.12-gke.1011000 or target 1.36.4 (1 open issue(s) with no published fix noted)", result["notes"][0])
        self.assertEqual(result["notes"][1], f"1 open known issue(s) with no published fix on GKE 1.36.4, noted and not graded as risks: open-from; each is on {self.PAGE}")
        self.assertEqual(result["notes"][2], f"target 1.36.4 is graded against the 2 listed build(s) of its patch (1.36.4-gke.1495002, 1.36.4-gke.1391002); known issue one-build (one build of the patch) affects 1.36.4-gke.1391002 of them, so whether the upgrade lands on it depends on the build picked; a target with its build says which: {self.PAGE}#one-build")
        # A target with its build stays exact: the one-build row is its risk.
        with patch.object(known_issues_matrix, "load_table", return_value=(table, None)):
            exact = known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, context_for(record, target_text="1.36.4-gke.1391002"))
        self.assertEqual(sorted((f["kind"], f["issue"]) for f in exact["risks"]), [("target", "one-build"), ("target", "whole-minor")])
        # A patch the channel lists no build of: a note, no target grade.
        with patch.object(known_issues_matrix, "load_table", return_value=(table, None)):
            none = known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, context_for(record, target_text="1.36.9"))
        self.assertEqual([f for f in none["risks"] if f["kind"] == "target"], [])
        self.assertEqual(none["notes"][-1], "target 1.36.9 names no build the REGULAR channel lists at us-central1-a, so the table was not read for it; a target with its build is graded")

    def test_a_short_target_reads_the_server_config_as_target_patch_valid_does(self):
        # A failed get-server-config read: an unknown naming the read, never the "names no build" note; the running
        # version is still graded, and a target with its build reads no server config.
        record = seeded_b()
        failed = project_reads(**{shared.LOCATION_READ_SERVER_CONFIG: failed_read("get-server-config failed for us-central1-a")})
        result = known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, context_for(record, target_text="1.36.4", reads=failed))
        self.assertEqual(result["unknown"], ["server config at us-central1-a not read (get-server-config failed for us-central1-a), so whether the REGULAR channel still lists a build of target 1.36.4 is unknown; a target with its build is graded"])
        self.assertFalse(any("names no build" in note for note in result["notes"]), result["notes"])
        self.assertEqual([f for f in result["risks"] if f["kind"] == "target"], [])
        self.assertTrue(any(f["kind"] == "running" for f in result["risks"]))
        self.assertEqual(known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, context_for(record, target_text="1.36.4-gke.1391002", reads=failed))["unknown"], [])
        # A read that returned no dict, and a channel the config does not carry: each an unknown, never the note.
        not_a_dict = project_reads(**{shared.LOCATION_READ_SERVER_CONFIG: ["not", "a", "config"]})
        self.assertEqual(known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, context_for(record, target_text="1.36.4", reads=not_a_dict))["unknown"], ["server config at us-central1-a not read (the server-config read returned list, not a dict), so whether the REGULAR channel still lists a build of target 1.36.4 is unknown; a target with its build is graded"])
        weekly = {**record, "releaseChannel": {"channel": "WEEKLY"}}
        result = known_issues_matrix.evaluate(weekly, {}, seeded_b_read(), None, context_for(weekly, target_text="1.36.4"))
        self.assertEqual(result["unknown"], ["channel WEEKLY is not in the server config at us-central1-a, so whether it lists a build of target 1.36.4 is unknown; a target with its build is graded"])
        self.assertFalse(any("names no build" in note for note in result["notes"]), result["notes"])
        # A build the node list alone carries: target-patch-valid admits it for a pool move alone, so the matrix grades
        # the target against it on a pool move and names both lists when neither carries one; on a control-plane move
        # the channel's list alone counts, as the sibling's blocker says.
        table = {"generated": "2026-10-20", "source": self.PAGE, "newest_gke_version": "1.37.1-gke.1", "issues": [
            {"id": "node-build", "summary": "the node build", "gke_versions": ["1.36.998-gke.1"], "addon": None, "addon_versions": [], "fixed_in": "1.36.998-gke.2 and later", "url": self.PAGE + "#node-build"},
        ]}
        node_only = copy.deepcopy(recorded("server_config_us_central1_a.json"))
        node_only["validNodeVersions"].insert(0, "1.36.998-gke.1")
        reads = project_reads(**{shared.LOCATION_READ_SERVER_CONFIG: node_only})
        at_the_build = {**record, "currentMasterVersion": "1.36.998-gke.1"}
        with patch.object(known_issues_matrix, "load_table", return_value=(table, None)):
            pools = known_issues_matrix.evaluate(at_the_build, {}, seeded_b_read(), None, context_for(at_the_build, target_text="1.36.998", reads=reads))
            neither = known_issues_matrix.evaluate(at_the_build, {}, seeded_b_read(), None, context_for(at_the_build, target_text="1.36.997", reads=reads))
            moving = known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, context_for(record, target_text="1.36.998", reads=reads))
        self.assertEqual([(f["kind"], f["issue"], f["version"]) for f in pools["risks"]], [("running", "node-build", "1.36.998-gke.1"), ("target", "node-build", "1.36.998")])
        self.assertEqual(pools["unknown"], [])
        self.assertEqual(target_patch_valid.evaluate(at_the_build, {}, None, None, context_for(at_the_build, target_text="1.36.998", reads=reads))["blocking"], [])  # the sibling agrees: valid
        self.assertEqual(neither["notes"][-1], "target 1.36.997 names no build the REGULAR channel or validNodeVersions lists at us-central1-a for its pool move, so the table was not read for it; a target with its build is graded")
        self.assertEqual([f for f in moving["risks"] if f["kind"] == "target"], [])
        self.assertEqual(moving["notes"][-1], "target 1.36.998 names no build the REGULAR channel lists at us-central1-a, so the table was not read for it; a target with its build is graded")
        self.assertEqual([f["move"] for f in target_patch_valid.evaluate(record, {}, None, None, context_for(record, target_text="1.36.998", reads=reads))["blocking"]], ["control-plane move"])  # and the sibling blocks the same move

    def test_the_version_grammar(self):
        vm = known_issues_matrix.version_matches
        self.assertTrue(vm("1.32.4-gke.1236007", "1.32.4-gke.1236007"))
        self.assertFalse(vm("1.32.4-gke.1236008", "1.32.4-gke.1236007"))
        inclusive = {"from": "1.32.0-gke.1358000", "to": "1.32.4-gke.1106006"}
        self.assertTrue(vm("1.32.4-gke.1000000", inclusive))
        self.assertTrue(vm("1.32.4-gke.1106006", inclusive))
        self.assertFalse(vm("1.32.4-gke.1106007", inclusive))
        self.assertFalse(vm("1.32.0-gke.1357999", inclusive))
        self.assertFalse(vm("1.33.0-gke.1552000", {"from": "1.33.0-gke.0", "to": "1.33.0-gke.1552000", "to_exclusive": True}))
        self.assertTrue(vm("1.34.0-gke.1", {"to": "1.34.0-gke.2285000", "to_exclusive": True}))
        self.assertFalse(vm("1.34.0-gke.2285000", {"to": "1.34.0-gke.2285000", "to_exclusive": True}))
        self.assertTrue(vm("1.37.9-gke.1", {"from": "1.35.3-gke.1234000"}))
        bounded_minor = {"minor": "1.35", "to": "1.35.6-gke.1250001", "to_exclusive": True}
        self.assertTrue(vm("1.35.5-gke.9", bounded_minor))
        self.assertFalse(vm("1.35.6-gke.1250001", bounded_minor))
        self.assertFalse(vm("1.34.9-gke.1", bounded_minor))
        self.assertFalse(vm("1.36.0-gke.1", bounded_minor))
        self.assertTrue(vm("1.35.9-gke.1", {"minor": "1.35"}))
        self.assertFalse(vm("1.36.0-gke.1", {"minor": "1.35"}))
        self.assertTrue(vm("1.32.9-gke.1", {"minor": "1.32", "from": "1.32.4-gke.1198000"}))
        self.assertFalse(vm("1.32.4-gke.1197999", {"minor": "1.32", "from": "1.32.4-gke.1198000"}))
        self.assertTrue(vm("1.34.12-gke.1011000", {"through_minor": "1.34"}))
        self.assertTrue(vm("1.27.1-gke.1", {"through_minor": "1.34"}))
        self.assertFalse(vm("1.35.0-gke.1", {"through_minor": "1.34"}))
        self.assertTrue(vm("1.33.0-gke.1", {"from_minor": "1.33"}))
        self.assertFalse(vm("1.32.99-gke.1", {"from_minor": "1.33"}))
        self.assertTrue(vm("1.20.0-gke.1", {"all_versions": True}))
        self.assertFalse(vm("1.20.0-gke.1", {"all_versions": False}))
        for junk in ("junk", {"minor": "1"}, {}, {"from": "x"}, {"to_exclusive": True}, 7, None):
            self.assertFalse(vm("1.34.12-gke.1011000", junk), junk)
        self.assertFalse(vm("junk", "junk-too"))
        self.assertFalse(vm(None, {"minor": "1.34"}))

    def test_a_stale_table_is_a_risk_by_age_alone(self):
        record = seeded_b()
        later = context_for(record, at=RECORDED_AT + timedelta(days=45))
        result = known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, later)
        stale = [f for f in result["risks"] if f["kind"].startswith("stale")]
        self.assertEqual([f["kind"] for f in stale], ["stale-age"])
        self.assertIn("known-issues table is stale: generated 2026-10-20, 45 days ago, past 30 days", known_issues_matrix.describe(stale[0]))
        # A server config listing a version newer than the table knows grades nothing; the note names what the table knows.
        newer = copy.deepcopy(recorded("server_config_us_central1_a.json"))
        newer["channels"][0]["validVersions"].insert(0, "1.99.0-gke.1")
        result = known_issues_matrix.evaluate(record, {}, seeded_b_read(), None, context_for(record, reads=project_reads(**{shared.LOCATION_READ_SERVER_CONFIG: newer})))
        self.assertEqual([f for f in result["risks"] if f["kind"].startswith("stale")], [])
        table, _ = known_issues_matrix.load_table()
        self.assertIn(f"newest known version {table['newest_gke_version']}", result["notes"][0])
        # A failed DaemonSet read: the inventory lists Deployments only, said as unknown (and the rule cannot block).
        result = known_issues_matrix.evaluate(record, {}, read(items=recorded("addons_seeded_b.json"), read_errors={"daemonset": "forbidden"}), None, context_for(record))
        self.assertEqual(result["unknown"], ["the DaemonSets were not read (forbidden), so the add-on inventory lists Deployments only"])
        self.assertFalse(known_issues_matrix.CAN_BLOCK)
        self.assertEqual((known_issues_matrix.RULE_ID, known_issues_matrix.ENTRY), ("known-issues-matrix", 26))
        # The table is read through the module loader, as the removal table is: a loader that serves nothing is unknown, naming the table.
        known_issues_matrix._TABLE_CACHE.clear()  # the table is read once per process; this probe needs the read
        with patch.object(known_issues_matrix.pkgutil, "get_data", return_value=None):
            table, error = known_issues_matrix.load_table()
        self.assertEqual((table, error), (None, "known-issues table known_issues.json could not be read beside upgrade_shape_tables (the module loader served no data)"))


class RegistryOrderTest(unittest.TestCase):
    def test_the_incident_rules_are_registered_by_entry(self):
        self.assertEqual([m.RULE_ID for m in r.EXTRA_RULES], ["surge-capacity", "data-on-the-node", "removed-api-callers", "changed-defaults", "deprecated-api-callers", "client-addon-skew", "zonal-control-plane", "removed-node-label", "container-runtime", "cgroup-v2-runtime", "group-oom-kill", "network-dataplane", "node-image-coupled-agent", "gpu-driver", "in-tree-volumes", "retired-registry", "lb-connection-draining", "orphan-node-objects", "control-plane-upgrade-history", "cluster-status-age", "target-patch-valid", "known-issues-matrix"])
        # Every blocking rule says what its mechanism holds; the node-removal rules hold the pool move alone.
        self.assertEqual({m.RULE_ID: m.HOLDS for m in r.EXTRA_RULES if m.CAN_BLOCK and hasattr(m, r.HOLDS_ATTRIBUTE)}, {"cluster-status-age": shared.HOLDS_BOTH, "target-patch-valid": shared.HOLDS_BOTH, "lb-connection-draining": (shared.HOLDS_NODE_POOL,), "orphan-node-objects": (shared.HOLDS_NODE_POOL,)})


if __name__ == "__main__":
    unittest.main()
