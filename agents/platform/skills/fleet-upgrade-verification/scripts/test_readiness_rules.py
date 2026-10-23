#!/usr/bin/env python3
"""Unit tests for readiness_rules/: the contract every registered rule meets, the helpers
the rules share, the `finding` helpers, the shape tables' sources, and the rules registered
here, on canned cluster records and kubectl objects."""

import os
import re
import sys
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import fleet_upgrade_report as report  # noqa: E402
import readiness_rules as shared  # noqa: E402
import upgrade_readiness as r  # noqa: E402
import upgrade_shape_tables as tables  # noqa: E402
from fleet_upgrade_report import parse_version  # noqa: E402
from readiness_rules import finding, zonal_control_plane  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
