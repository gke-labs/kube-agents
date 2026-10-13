#!/usr/bin/env python3
"""Unit tests for readiness_rules/capacity.py (catalogue entry 2) on canned cluster records
and kubectl objects, and one run of the rule through the report's split reads."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import fleet_upgrade_report as report  # noqa: E402
import readiness_rules as shared  # noqa: E402
from fleet_upgrade_report import parse_version  # noqa: E402
from readiness_rules import capacity  # noqa: E402
from test_fleet_upgrade_report import FakeReadinessCommands  # noqa: E402
from test_fleet_upgrade_report import cluster as report_cluster  # noqa: E402
from test_readiness_rules import CURRENT, LAGGING, TARGET, cluster, deployment_pod, node, pod, read  # noqa: E402

TARGET_TEXT = "1.35.1-gke.1000"
AT = "2026-09-14T15:00:00Z"
NO_SURGE = {"maxUnavailable": 1, "strategy": "SURGE"}  # the API omits a zero maxSurge
PIN = {"seeded-role": "no-surge"}
TAINT = {"key": "seeded-role", "value": "no-surge", "effect": "NoSchedule"}
TOLERATION = {"key": "seeded-role", "operator": "Equal", "value": "no-surge", "effect": "NoSchedule"}


def pool_record(name, version, upgrade=None, autoscaling=None, image="COS_CONTAINERD", locations=None):
    """A pool as `gcloud container clusters list` records it."""
    record = {"name": name, "version": version, "status": "RUNNING", "config": {"imageType": image}}
    if upgrade is not None:
        record["upgradeSettings"] = upgrade
    if autoscaling is not None:
        record["autoscaling"] = autoscaling
    if locations is not None:
        record["locations"] = locations
    return record


def context_for(record, clusters=None):
    """The per-member context as fleet_upgrade_report builds it, for the keys this rule reads."""
    pools = [{"name": p["name"], "version": p["version"], "parsed": parse_version(p["version"]), "config": p.get("config") or {}, "autoscaling": p.get("autoscaling") or {}} for p in record.get("nodePools") or []]
    return {shared.CONTEXT_CLUSTERS: clusters if clusters is not None else [record], shared.CONTEXT_POOLS: pools, shared.CONTEXT_AUTOPILOT: bool((record.get("autopilot") or {}).get("enabled"))}


def scheduled_pod(namespace, name, node_name, node_selector=None, tolerations=None, affinity=None, **kw):
    """The base builder's pod with the scheduling fields this rule reads."""
    record = deployment_pod(namespace, name, node_name, **kw) if kw.pop("deployment", True) else pod(namespace, name, node_name, **kw)
    if node_selector:
        record["spec"]["nodeSelector"] = node_selector
    if tolerations:
        record["spec"]["tolerations"] = tolerations
    if affinity:
        record["spec"]["affinity"] = affinity
    return record


def seeded(upgrade=NO_SURGE, autoscaling=None, pinned=True, default_room=True):
    """The seeded shape: no-surge-pool (one node, tainted) with pinned-batch-runner on it,
    beside a default pool with room; `pinned=False` drops the runner's selector and toleration."""
    record = cluster(pools=[pool_record("default-pool", LAGGING), pool_record("no-surge-pool", LAGGING, upgrade=upgrade, autoscaling=autoscaling)])
    runner = {"cpu": "250m", "memory": "256Mi"}
    if pinned:
        runner.update(node_selector=PIN, tolerations=[TOLERATION])
    items = [
        node("gke-b-default-1", "default-pool", cpu="940m", memory="2Gi"),
        node("gke-b-nosurge-1", "no-surge-pool", cpu="940m", memory="2Gi", labels=PIN, taints=[TAINT]),
        scheduled_pod("seeded-upgrade", "pinned-batch-runner", "gke-b-nosurge-1", **runner),
        deployment_pod("kube-system", "metrics-server", "gke-b-default-1", cpu="100m" if default_room else "800m", memory="100Mi" if default_room else "1900Mi"),
        pod("kube-system", "fluentbit-x", "gke-b-nosurge-1", cpu="100m", memory="100Mi", owner=("DaemonSet", "fluentbit")),
    ]
    return record, items


def evaluate(record, items, target=TARGET, **read_kw):
    return capacity.evaluate(record, {}, read(items=items, **read_kw), target, context_for(record))


class CapacityRuleTest(unittest.TestCase):
    def test_contract_attributes(self):
        self.assertEqual((capacity.RULE_ID, capacity.ENTRY, capacity.CAN_BLOCK), ("surge-capacity", 2, True))

    def test_pinned_pod_with_no_room_on_the_pool_blocks(self):
        record, items = seeded()
        out = evaluate(record, items)
        self.assertEqual((out["risks"], out["unknown"]), ([], []))
        self.assertEqual(len(out["blocking"]), 1)
        finding = out["blocking"][0]
        self.assertEqual((finding["pool"], finding["max_surge"], finding["max_unavailable"], finding["autoscaled"], finding["at_ceiling"]), ("no-surge-pool", 0, 1, False, True))
        self.assertEqual(finding["node"], "gke-b-nosurge-1")
        # The DaemonSet pod is not displaced; the runner is, and no node admits it.
        self.assertEqual(finding["displaced"], {"cpu_m": 250, "memory_bytes": 256 * 1024**2})
        self.assertEqual(finding["stranded"], {"cpu_m": 250, "memory_bytes": 256 * 1024**2})
        self.assertEqual(finding["room"], {"cpu_m": 0, "memory_bytes": 0})
        self.assertEqual(finding["workloads"], [{"owner": "Deployment seeded-upgrade/pinned-batch-runner", "cpu_m": 250, "memory_bytes": 256 * 1024**2, "pinned": True}])
        self.assertEqual(finding["other_short_nodes"], [])
        text = capacity.describe(finding)
        self.assertIn("pool no-surge-pool (maxSurge 0, maxUnavailable 1; 1 node(s), not autoscaled) removes a node before its replacement exists", text)
        self.assertIn("draining its node gke-b-nosurge-1 leaves 250m CPU / 256Mi of pods with no node to take them (0m / 0Mi free on the nodes they may schedule on)", text)
        self.assertIn("Deployment seeded-upgrade/pinned-batch-runner (250m / 256Mi, pinned to the pool)", text)

    def test_free_pod_with_room_elsewhere_is_a_note_not_a_finding(self):
        record, items = seeded(pinned=False)
        out = evaluate(record, items)
        self.assertEqual((out["blocking"], out["risks"], out["unknown"]), ([], [], []))
        self.assertEqual(len(out["notes"]), 1)
        self.assertIn("the nodes its pods may schedule on have room for every node's pods", out["notes"][0])

    def test_free_pod_with_no_room_anywhere_blocks(self):
        record, items = seeded(pinned=False, default_room=False)
        out = evaluate(record, items)
        self.assertEqual(len(out["blocking"]), 1)
        self.assertEqual(out["blocking"][0]["workloads"][0]["pinned"], False)
        self.assertNotIn("pinned to the pool", capacity.describe(out["blocking"][0]))

    def test_a_tainted_nodes_spare_capacity_never_counts_for_a_pod_without_the_toleration(self):
        gpu_taint = {"key": "nvidia.com/gpu", "value": "present", "effect": "NoSchedule"}
        record = cluster(pools=[pool_record("default-pool", LAGGING), pool_record("batch", LAGGING, upgrade=NO_SURGE), pool_record("gpu", LAGGING)])
        items = [
            node("batch-1", "batch", cpu="4000m", memory="16Gi"),
            node("batch-2", "batch", cpu="1000m", memory="4Gi"),
            node("default-1", "default-pool", cpu="200m", memory="1Gi"),
            node("gpu-1", "gpu", cpu="32", memory="128Gi", taints=[gpu_taint]),
            deployment_pod("jobs", "big-batch", "batch-1", cpu="3800m", memory="15Gi"),
        ]
        out = evaluate(record, items)
        self.assertEqual(len(out["blocking"]), 1, out)
        finding = out["blocking"][0]
        self.assertEqual((finding["node"], finding["stranded"], finding["room"]), ("batch-1", {"cpu_m": 3800, "memory_bytes": 15 * 1024**3}, {"cpu_m": 1200, "memory_bytes": 5 * 1024**3}))
        self.assertFalse(finding["workloads"][0]["pinned"])
        # With the toleration the GPU node is eligible and takes the pod.
        items[-1]["spec"]["tolerations"] = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
        out = evaluate(record, items)
        self.assertEqual((out["blocking"], len(out["notes"])), ([], 1))

    def test_a_cordoned_node_is_out_through_unschedulable_and_through_its_taint(self):
        # The projected node read carries spec.unschedulable, and a cordon also adds the
        # unschedulable taint; either alone keeps the pod off the node.
        cordon = {"key": "node.kubernetes.io/unschedulable", "effect": "NoSchedule"}
        record = cluster(pools=[pool_record("default-pool", LAGGING), pool_record("batch", LAGGING, upgrade=NO_SURGE)])
        items = [
            node("batch-1", "batch", cpu="1000m", memory="4Gi"),
            node("default-1", "default-pool", cpu="4000m", memory="16Gi", unschedulable=True),
            deployment_pod("jobs", "job", "batch-1", cpu="500m", memory="1Gi"),
        ]
        self.assertEqual(len(evaluate(record, items)["blocking"]), 1)
        items[1] = node("default-1", "default-pool", cpu="4000m", memory="16Gi", taints=[cordon])
        self.assertEqual(len(evaluate(record, items)["blocking"]), 1)
        items[1] = node("default-1", "default-pool", cpu="4000m", memory="16Gi")
        self.assertEqual(evaluate(record, items)["blocking"], [])

    def test_a_memory_bound_node_is_measured_too(self):
        record = cluster(pools=[pool_record("default-pool", LAGGING), pool_record("batch", LAGGING, upgrade=NO_SURGE)])
        items = [
            node("batch-a", "batch", cpu="4000m", memory="4Gi"),
            node("batch-b", "batch", cpu="4000m", memory="64Gi"),
            node("default-1", "default-pool", cpu="1000m", memory="5Gi"),
            deployment_pod("jobs", "cpu-job", "batch-a", cpu="200m", memory="1Gi"),
            deployment_pod("jobs", "memory-job", "batch-b", cpu="100m", memory="60Gi"),
        ]
        out = evaluate(record, items)
        self.assertEqual(len(out["blocking"]), 1, out)
        self.assertEqual((out["blocking"][0]["node"], out["blocking"][0]["workloads"][0]["owner"], out["blocking"][0]["other_short_nodes"]), ("batch-b", "Deployment jobs/memory-job", []))
        items[-2]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "4000m"
        out = evaluate(record, items)
        self.assertEqual(sorted([out["blocking"][0]["node"]] + out["blocking"][0]["other_short_nodes"]), ["batch-a", "batch-b"])
        self.assertIn("strand pods as well", capacity.describe(out["blocking"][0]))

    def test_placement_is_per_node_not_in_aggregate(self):
        record = cluster(pools=[pool_record("default-pool", LAGGING), pool_record("batch", LAGGING, upgrade=NO_SURGE)])
        items = [
            node("batch-1", "batch", cpu="1000m", memory="4Gi"),
            node("default-1", "default-pool", cpu="400m", memory="4Gi"),
            node("default-2", "default-pool", cpu="400m", memory="4Gi"),
            deployment_pod("jobs", "wide", "batch-1", cpu="600m", memory="1Gi"),
        ]
        out = evaluate(record, items)
        self.assertEqual(len(out["blocking"]), 1)
        self.assertEqual(out["blocking"][0]["room"], {"cpu_m": 800, "memory_bytes": 8 * 1024**3})
        items[-1]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "300m"
        items.append(deployment_pod("jobs", "tall", "batch-1", cpu="400m", memory="1Gi"))
        out = evaluate(record, items)
        self.assertEqual((out["blocking"], len(out["notes"])), ([], 1))

    def test_surge_left_or_a_growable_autoscaler_is_a_risk_and_a_ceiling_blocks(self):
        record, items = seeded(upgrade={"maxSurge": 1, "maxUnavailable": 1, "strategy": "SURGE"})
        out = evaluate(record, items)
        self.assertEqual((out["blocking"], out["risks"][0]["max_surge"]), ([], 1))
        record, items = seeded(autoscaling={"enabled": True, "minNodeCount": 1, "maxNodeCount": 3})
        out = evaluate(record, items)
        self.assertEqual((out["blocking"], out["risks"][0]["ceiling"], out["risks"][0]["at_ceiling"]), ([], 3, False))
        self.assertIn("autoscaler can grow to 3", capacity.describe(out["risks"][0]))
        record, items = seeded(autoscaling={"enabled": True, "minNodeCount": 1, "maxNodeCount": 1})
        out = evaluate(record, items)
        self.assertEqual(len(out["blocking"]), 1)
        self.assertIn("autoscaler at its ceiling of 1", out["blocking"][0]["summary"])

    def test_autoscaled_blue_green_is_a_risk_never_a_block(self):
        record, items = seeded(upgrade={"strategy": "BLUE_GREEN", "blueGreenSettings": {"autoscaledRolloutPolicy": {}}})
        out = evaluate(record, items)
        self.assertEqual((out["blocking"], out["risks"][0]["strategy"], out["risks"][0]["stranded_count"]), ([], "BLUE_GREEN", 1))
        self.assertIn("autoscaled blue-green, whose green pool starts empty", capacity.describe(out["risks"][0]))
        record, items = seeded(upgrade={"strategy": "BLUE_GREEN", "blueGreenSettings": {"standardRolloutPolicy": {"batchNodeCount": 1}}})
        self.assertEqual(evaluate(record, items), shared.new_result())

    def test_default_settings_pools_at_the_target_and_autopilot_are_clean(self):
        record, items = seeded(upgrade=None)
        self.assertEqual(evaluate(record, items), shared.new_result())
        record, items = seeded()
        record["nodePools"][1]["version"] = CURRENT
        self.assertEqual(evaluate(record, items), shared.new_result())
        record, items = seeded()
        record["autopilot"] = {"enabled": True}
        self.assertEqual(evaluate(record, items), shared.new_result())
        # The context's autopilot flag wins over the record when present.
        record["autopilot"] = {}
        self.assertEqual(capacity.evaluate(record, {}, read(items=items), TARGET, {**context_for(record), shared.CONTEXT_AUTOPILOT: True}), shared.new_result())

    def test_unknown_paths(self):
        record, items = seeded()
        self.assertIn("no target", evaluate(record, items, target=None)["unknown"][0])
        nodes_unread = evaluate(record, items, read_errors={"node": "nodes is forbidden"})
        self.assertIn("node not read (nodes is forbidden), so its headroom was not measured", nodes_unread["unknown"][0])
        self.assertIn("? node(s)", nodes_unread["unknown"][0])
        pods_unread = evaluate(record, items, read_errors={"pods": "pods is forbidden"})
        self.assertIn("pods not read (pods is forbidden)", pods_unread["unknown"][0])
        nothing = capacity.evaluate(record, {}, read(error="get-credentials failed"), TARGET, context_for(record))
        self.assertIn("node not read (get-credentials failed)", nothing["unknown"][0])
        record["nodePools"][1]["version"] = "latest"
        self.assertIn("version unparsable", evaluate(record, items)["unknown"][0])
        record, items = seeded()
        items[0]["status"]["allocatable"]["cpu"] = "many"
        self.assertIn("the allocatable of node gke-b-default-1 did not parse", evaluate(record, items)["unknown"][0])
        # No pools in the context (the contract input): a removes-first pool's version is unknown.
        self.assertIn("version unparsable", capacity.evaluate(record, {}, read(items=items), TARGET, {})["unknown"][0])

    def test_empty_pool_is_a_note(self):
        record, items = seeded()
        items = [i for i in items if not (i["kind"] == "Node" and shared.node_pool(i) == "no-surge-pool")]
        self.assertIn("has no node", evaluate(record, items)["notes"][0])

    def test_autoscaler_ceiling_total_beats_per_zone(self):
        record = cluster(locations=["us-central1-a", "us-central1-b"])
        self.assertEqual(capacity.autoscaler_ceiling(pool_record("p", LAGGING, autoscaling={"enabled": True, "maxNodeCount": 2}), record), (True, 4))
        self.assertEqual(capacity.autoscaler_ceiling(pool_record("p", LAGGING, autoscaling={"enabled": True, "maxNodeCount": 2, "totalMaxNodeCount": 3}), record), (True, 3))
        self.assertEqual(capacity.autoscaler_ceiling(pool_record("p", LAGGING, autoscaling={"enabled": True, "maxNodeCount": 2}, locations=["us-central1-a"]), record), (True, 2))
        self.assertEqual(capacity.autoscaler_ceiling(pool_record("p", LAGGING), record), (False, None))

    def test_node_fits_pod_reads_selector_affinity_and_taints(self):
        plain = node("n", "default-pool")
        tainted = node("t", "no-surge-pool", labels=PIN, taints=[TAINT])
        pinned = scheduled_pod("ns", "p", "t", node_selector=PIN, tolerations=[TOLERATION])["spec"]
        self.assertFalse(capacity.node_fits_pod(plain, pinned))
        self.assertTrue(capacity.node_fits_pod(tainted, pinned))
        self.assertFalse(capacity.node_fits_pod(tainted, scheduled_pod("ns", "p", "t", node_selector=PIN)["spec"]))
        affinity = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "seeded-role", "operator": "In", "values": ["no-surge"]}]}, {"matchExpressions": [{"key": "other", "operator": "Exists"}]}]}}}
        affine = scheduled_pod("ns", "p", "t", tolerations=[TOLERATION], affinity=affinity)["spec"]
        self.assertFalse(capacity.node_fits_pod(plain, affine))
        self.assertTrue(capacity.node_fits_pod(tainted, affine))  # the first of two ORed terms
        self.assertTrue(capacity.node_fits_pod(node("o", "p", labels={"other": "x"}), {"affinity": affinity, "containers": []}))
        self.assertTrue(capacity.node_fits_pod(tainted, {"tolerations": [{"operator": "Exists"}], "containers": []}))
        unknown_op = {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "x", "operator": "Gt", "values": ["1"]}]}]}}}}
        self.assertFalse(capacity.node_fits_pod(plain, unknown_op))


class CapacityThroughTheReportTest(unittest.TestCase):
    """The seeded blocker reaches the `other blockers` column through the split reads: the
    pods from the namespaced read, the nodes from the projected node read."""

    def test_the_seeded_blocker_is_in_the_table_and_the_json(self):
        state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state_dir, True)
        kubeconfig_dir = os.path.join(tempfile.mkdtemp(), "kubeconfigs")
        self.addCleanup(shutil.rmtree, os.path.dirname(kubeconfig_dir), True)
        record = report_cluster("seeded-b", "us-central1-a", LAGGING, [("default-pool", LAGGING), ("no-surge-pool", LAGGING)])
        record["nodePools"][1]["upgradeSettings"] = NO_SURGE
        _, items = seeded()
        nodes = [i for i in items if i["kind"] == "Node"]
        pods = [i for i in items if i["kind"] == "Pod"]
        fake = FakeReadinessCommands({"p1": [record]}, {}, {"seeded-b": []}, pods_by_cluster={"seeded-b": pods}, nodes_by_cluster={"seeded-b": nodes})
        out_path = os.path.join(state_dir, "report.json")
        with patch.object(report, "run_cmd", fake), redirect_stdout(io.StringIO()) as stdout:
            rc = report.main(["--state-dir", state_dir, "--project", "p1", "--target-version", TARGET_TEXT, "--readiness", "--at", AT, "--kubeconfig-dir", kubeconfig_dir, "--output", out_path])
        self.assertEqual(rc, report.EXIT_OK)
        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
        readiness = data["members"][0]["readiness"]
        self.assertEqual(readiness["status"], "blocked")
        self.assertEqual([f["rule"] for f in readiness["rule_blocking"]], ["surge-capacity"])
        self.assertEqual(readiness["rule_blocking"][0]["workloads"][0]["owner"], "Deployment seeded-upgrade/pinned-batch-runner")
        self.assertEqual(readiness["rules"]["surge-capacity"], {"entry": 2, "can_block": True, "blocking": 1, "risks": 0, "unknown": 0, "notes": 0})
        row = next(line for line in stdout.getvalue().splitlines() if line.startswith("| p1 | seeded-b |") and "| blocked |" in line)
        self.assertIn("pool no-surge-pool (maxSurge 0, maxUnavailable 1; 1 node(s), not autoscaled) removes a node before its replacement exists", row)
        self.assertIn("Deployment seeded-upgrade/pinned-batch-runner (250m / 256Mi, pinned to the pool)", row)


if __name__ == "__main__":
    unittest.main()
