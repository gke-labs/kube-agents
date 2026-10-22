#!/usr/bin/env python3
"""Unit tests for readiness_rules/capacity.py (catalogue entry 2) on canned cluster records
and kubectl objects, on a recorded cluster record and node list, and runs of the rule through
the report's split reads."""

import copy
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
GPU_TARGET = parse_version("1.32.13-gke.2427000")
# A cluster record (`gcloud container clusters describe`) and node list (`kubectl get nodes`)
# recorded on 2026-10-20 from a dev project's GPU cluster, cut to the fields the report and
# the rule read and with the project's names shortened: a default pool that surges by one
# and an L4 pool that upgrades in place, both on the cluster's /14 Pod range with a /24 block
# per node (the `maxPodsConstraint` of 110).
RECORDED = {'cluster': {'name': 'gpu-canary',
             'location': 'us-central1-a',
             'status': 'RUNNING',
             'currentMasterVersion': '1.31.14-gke.2704000',
             'locations': ['us-central1-a'],
             'clusterIpv4Cidr': '10.4.0.0/14',
             'ipAllocationPolicy': {'useIpAliases': True, 'clusterIpv4CidrBlock': '10.4.0.0/14', 'servicesIpv4CidrBlock': '34.118.224.0/20'},
             'defaultMaxPodsConstraint': {'maxPodsPerNode': '110'},
             'releaseChannel': {'channel': 'EXTENDED'},
             'nodePools': [{'name': 'default-pool',
                            'version': '1.31.14-gke.2704000',
                            'status': 'RUNNING',
                            'locations': ['us-central1-a'],
                            'initialNodeCount': 1,
                            'config': {'machineType': 'e2-small', 'imageType': 'COS_CONTAINERD'},
                            'upgradeSettings': {'maxSurge': 1, 'strategy': 'SURGE'},
                            'maxPodsConstraint': {'maxPodsPerNode': '110'},
                            'networkConfig': {'podIpv4CidrBlock': '10.4.0.0/14', 'podRange': 'gke-gpu-canary-pods'}},
                           {'name': 'gpu-pool',
                            'version': '1.31.14-gke.2704000',
                            'status': 'RUNNING',
                            'locations': ['us-central1-a'],
                            'initialNodeCount': 1,
                            'config': {'machineType': 'g2-standard-4',
                                       'imageType': 'COS_CONTAINERD',
                                       'accelerators': [{'acceleratorCount': '1',
                                                         'acceleratorType': 'nvidia-l4',
                                                         'gpuDriverInstallationConfig': {'gpuDriverVersion': 'LATEST'}}]},
                            'upgradeSettings': {'maxUnavailable': 1, 'strategy': 'SURGE'},
                            'maxPodsConstraint': {'maxPodsPerNode': '110'},
                            'networkConfig': {'podIpv4CidrBlock': '10.4.0.0/14', 'podRange': 'gke-gpu-canary-pods'}}]},
 'nodes': [{'kind': 'Node',
            'metadata': {'name': 'gke-gpu-canary-default-pool-cbd06cf1-7xtr',
                         'labels': {'cloud.google.com/gke-nodepool': 'default-pool',
                                    'cloud.google.com/machine-family': 'e2',
                                    'kubernetes.io/arch': 'amd64',
                                    'topology.kubernetes.io/zone': 'us-central1-a'}},
            'spec': {'podCIDR': '10.4.0.0/24', 'taints': []},
            'status': {'allocatable': {'cpu': '940m', 'memory': '1393776Ki', 'pods': '110'},
                       'conditions': [{'type': 'Ready', 'status': 'True'}],
                       'nodeInfo': {'containerRuntimeVersion': 'containerd://1.7.34', 'kubeletVersion': 'v1.31.14-gke.2704000'}}},
           {'kind': 'Node',
            'metadata': {'name': 'gke-gpu-canary-gpu-pool-6e1b0306-tglz',
                         'labels': {'cloud.google.com/gke-accelerator': 'nvidia-l4',
                                    'cloud.google.com/gke-nodepool': 'gpu-pool',
                                    'cloud.google.com/machine-family': 'g2',
                                    'kubernetes.io/arch': 'amd64',
                                    'topology.kubernetes.io/zone': 'us-central1-a'}},
            'spec': {'podCIDR': '10.4.1.0/24', 'taints': [{'effect': 'NoSchedule', 'key': 'nvidia.com/gpu', 'value': 'present'}]},
            'status': {'allocatable': {'cpu': '3920m', 'memory': '13594488Ki', 'nvidia.com/gpu': '1', 'pods': '110'},
                       'conditions': [{'type': 'Ready', 'status': 'True'}],
                       'nodeInfo': {'containerRuntimeVersion': 'containerd://1.7.34', 'kubeletVersion': 'v1.31.14-gke.2704000'}}}]}


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


def recorded():
    """A copy of the recorded cluster record and its node list."""
    return copy.deepcopy(RECORDED["cluster"]), copy.deepcopy(RECORDED["nodes"])


def node_with(pod_cidr, name="n", pool_name="default-pool"):
    """A node carrying `pod_cidr` as its block, or none when None."""
    record = node(name, pool_name)
    if pod_cidr is not None:
        record["spec"]["podCIDR"] = pod_cidr
    return record


def surge(record, index, max_surge=1):
    """`record`'s pool at `index` set to surge by `max_surge`."""
    record["nodePools"][index]["upgradeSettings"] = {"maxSurge": max_surge, "strategy": "SURGE"}


def pod_range(record, cidr):
    """`record`'s Pod range, under both spellings and on every pool, set to `cidr`."""
    record["clusterIpv4Cidr"] = cidr
    record["ipAllocationPolicy"]["clusterIpv4CidrBlock"] = cidr
    for pool in record["nodePools"]:
        pool["networkConfig"]["podIpv4CidrBlock"] = cidr


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
        self.assertEqual(finding["workloads"], [{"owner": "Deployment seeded-upgrade/pinned-batch-runner", "cpu_m": 250, "memory_bytes": 256 * 1024**2, "extended": {}, "pinned": True}])
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

    def test_a_gpu_pod_lands_only_on_a_node_with_a_free_gpu(self):
        # Two GPU nodes with CPU and memory to spare, one 1-GPU pod each: draining one finds the
        # other's GPU taken and the CPU-only default node without one, so the pod is stranded and
        # the non-autoscaled maxSurge-0 pool blocks; a free GPU on the other node places it.
        gpu_taint = {"key": "nvidia.com/gpu", "value": "present", "effect": "NoSchedule"}
        gpu_label = {"cloud.google.com/gke-accelerator": "nvidia-l4"}
        record = cluster(pools=[pool_record("default-pool", LAGGING), pool_record("gpu", LAGGING, upgrade=NO_SURGE)])
        nodes = [node("gpu-1", "gpu", cpu="8", memory="32Gi", labels=gpu_label, taints=[gpu_taint]), node("gpu-2", "gpu", cpu="8", memory="32Gi", labels=gpu_label, taints=[gpu_taint]), node("default-1", "default-pool", cpu="8", memory="32Gi")]
        for n in nodes[:2]:
            n["status"]["allocatable"]["nvidia.com/gpu"] = "1"
        pods = []
        for i, node_name in enumerate(("gpu-1", "gpu-2")):
            trainer = scheduled_pod("ml", f"trainer-{i}", node_name, node_selector=gpu_label, tolerations=[{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}], cpu="1", memory="4Gi")
            trainer["spec"]["containers"][0]["resources"]["requests"]["nvidia.com/gpu"] = "1"
            pods.append(trainer)
        out = evaluate(record, nodes + pods)
        self.assertEqual((out["risks"], out["unknown"], out["notes"]), ([], [], []))
        finding = out["blocking"][0]
        self.assertEqual((finding["stranded_count"], finding["workloads"][0]["extended"], finding["workloads"][0]["pinned"]), (1, {"nvidia.com/gpu": 1.0}, True))
        self.assertIn("(1000m / 4096Mi, 1 nvidia.com/gpu, pinned to the pool)", capacity.describe(finding))
        # The second node's GPU freed: the displaced pod lands there, and the pool is a note.
        out = evaluate(record, nodes + pods[:1])
        self.assertEqual((out["blocking"], out["risks"], len(out["notes"])), ([], [], 1))
        # An unparsable extended quantity is the rule's unknown, like a CPU one.
        nodes[1]["status"]["allocatable"]["nvidia.com/gpu"] = "one"
        self.assertIn("the allocatable of node gpu-2 did not parse", evaluate(record, nodes + pods)["unknown"][0])

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


class SurgeHalvesTest(unittest.TestCase):
    """The accelerator and Pod range halves on the recorded cluster."""

    def test_the_recorded_cluster_is_clean_on_both_halves(self):
        # The L4 pool upgrades in place (maxSurge 0, maxUnavailable 1) and the default pool surges by
        # one into a /14 whose 1,024 blocks hold two nodes; the placement half notes the in-place pool.
        record, nodes = recorded()
        out = evaluate(record, nodes, target=GPU_TARGET)
        self.assertEqual((out["blocking"], out["risks"], out["unknown"]), ([], [], []))
        self.assertEqual(len(out["notes"]), 1)
        self.assertIn("pool gpu-pool (maxSurge 0, maxUnavailable 1; 1 node(s), not autoscaled) removes a node before its replacement exists", out["notes"][0])
        self.assertEqual(capacity.free_blocks(capacity.pod_network(record["clusterIpv4Cidr"]), 24, nodes), (1022, 2, 1024))

    def test_an_accelerator_pool_that_surges_with_no_reservation_is_a_risk(self):
        record, nodes = recorded()
        surge(record, 1)
        out = evaluate(record, nodes, target=GPU_TARGET)
        # A risk, not a blocker: the surge upgrade waits for the accelerator with the old node
        # serving (catalogue entry 2); only the Pod-range shortfall blocks.
        self.assertEqual((out["blocking"], out["unknown"], out["notes"]), ([], [], []))
        self.assertEqual(len(out["risks"]), 1)
        finding = out["risks"][0]
        self.assertEqual((finding["finding"], finding["pool"], finding["accelerators"], finding["max_surge"], finding["reservation"]), ("accelerator", "gpu-pool", "1 x nvidia-l4", 1, None))
        text = capacity.describe(finding)
        self.assertIn("pool gpu-pool (1 x nvidia-l4) surges (maxSurge 1) with no reservation affinity", text)
        # The reservation comes first: in-place on a pool whose accelerators are all requested
        # strands the drained pods, the placement half's blocker.
        self.assertIn("; hold a reservation for the surge node, or, on a pool with a spare accelerator node for the drained pods, set the in-place strategy (max-surge 0, max-unavailable 1)", text)
        # NO_RESERVATION holds none either; a reservation the pool consumes is a note.
        record["nodePools"][1]["config"]["reservationAffinity"] = {"consumeReservationType": "NO_RESERVATION"}
        self.assertEqual(len(evaluate(record, nodes, target=GPU_TARGET)["risks"]), 1)
        record["nodePools"][1]["config"]["reservationAffinity"] = {"consumeReservationType": "SPECIFIC_RESERVATION", "key": "compute.googleapis.com/reservation-name", "values": ["l4-reservation"]}
        out = evaluate(record, nodes, target=GPU_TARGET)
        self.assertEqual((out["risks"], out["notes"]), ([], ["pool gpu-pool (1 x nvidia-l4) surges (maxSurge 1) under reservation affinity SPECIFIC_RESERVATION"]))
        # Two accelerators, and a pool at the target, which is not graded.
        del record["nodePools"][1]["config"]["reservationAffinity"]
        record["nodePools"][1]["config"]["accelerators"].append({"acceleratorCount": "2", "acceleratorType": "nvidia-tesla-t4"})
        self.assertEqual(evaluate(record, nodes, target=GPU_TARGET)["risks"][0]["accelerators"], "1 x nvidia-l4, 2 x nvidia-tesla-t4")
        record["nodePools"][1]["version"] = "1.32.13-gke.2427000"
        self.assertEqual(evaluate(record, nodes, target=GPU_TARGET)["risks"], [])

    def test_a_pod_range_with_fewer_free_blocks_than_the_surge_need_blocks(self):
        record, nodes = recorded()
        pod_range(record, "10.4.0.0/23")  # two /24 blocks, both held by the recorded nodes
        out = evaluate(record, nodes, target=GPU_TARGET)
        self.assertEqual(len(out["blocking"]), 1, out)
        finding = out["blocking"][0]
        self.assertEqual({key: finding[key] for key in ("finding", "range", "prefix", "free", "held", "total", "need")}, {"finding": "pod-range", "range": "10.4.0.0/23", "prefix": 24, "free": 0, "held": 2, "total": 2, "need": 1})
        self.assertEqual(finding["pools"], [{"pool": "default-pool", "max_surge": 1}])
        text = capacity.describe(finding)
        self.assertIn("pod range 10.4.0.0/23 has 0 free /24 block(s) (2 of 2 hold a node) and the 1 surge node(s) of pool(s) default-pool (maxSurge 1) each need one", text)
        self.assertIn("lower max-surge or add a secondary Pod range", text)
        # With the L4 pool surging too the need is two, and the accelerator half files its own risk.
        surge(record, 1)
        out = evaluate(record, nodes, target=GPU_TARGET)
        self.assertEqual([f["finding"] for f in out["blocking"]], ["pod-range"])
        self.assertEqual([f["finding"] for f in out["risks"]], ["accelerator"])
        self.assertEqual((out["blocking"][0]["need"], out["blocking"][0]["pools"]), (2, [{"pool": "default-pool", "max_surge": 1}, {"pool": "gpu-pool", "max_surge": 1}]))
        # A /22 has two free blocks, enough for both; a maxSurge of 3 is not.
        pod_range(record, "10.4.0.0/22")
        self.assertEqual([f["finding"] for f in evaluate(record, nodes, target=GPU_TARGET)["blocking"]], [])
        surge(record, 0, max_surge=3)
        self.assertEqual([(f["finding"], f.get("need"), f.get("free")) for f in evaluate(record, nodes, target=GPU_TARGET)["blocking"]], [("pod-range", 4, 2)])
        # No surge node, no need: both pools in place, or on a blue-green rollout.
        pod_range(record, "10.4.0.0/23")
        record["nodePools"][0]["upgradeSettings"] = NO_SURGE
        record["nodePools"][1]["upgradeSettings"] = {"strategy": "BLUE_GREEN", "blueGreenSettings": {"standardRolloutPolicy": {"batchNodeCount": 1}}}
        out = evaluate(record, nodes, target=GPU_TARGET)
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))

    def test_a_pool_with_its_own_pod_range_is_counted_in_that_range(self):
        record, nodes = recorded()
        surge(record, 1)
        record["nodePools"][1]["config"]["reservationAffinity"] = {"consumeReservationType": "ANY_RESERVATION"}
        record["nodePools"][1]["networkConfig"] = {"podIpv4CidrBlock": "10.8.0.0/24", "podRange": "gpu-pods"}
        nodes[1]["spec"]["podCIDR"] = "10.8.0.0/24"
        out = evaluate(record, nodes, target=GPU_TARGET)
        self.assertEqual([(f["finding"], f["range"], f["need"], f["free"], f["pools"]) for f in out["blocking"]], [("pod-range", "10.8.0.0/24", 1, 0, [{"pool": "gpu-pool", "max_surge": 1}])])
        # The cluster's range, read under the policy's spelling when the top-level one is absent, is counted apart.
        del record["clusterIpv4Cidr"]
        del record["nodePools"][0]["networkConfig"]
        self.assertEqual(capacity.pool_pod_range(record["nodePools"][0], record), "10.4.0.0/14")
        self.assertEqual(len(evaluate(record, nodes, target=GPU_TARGET)["blocking"]), 1)

    def test_block_sizes_follow_the_nodes_then_the_pod_limit(self):
        self.assertEqual([capacity.block_prefix_for_pods(pods) for pods in ("110", 64, 32, 8, 256)], [24, 25, 26, 28, 23])
        self.assertIsNone(capacity.block_prefix_for_pods("many"))
        record, nodes = recorded()
        pool = record["nodePools"][0]
        self.assertEqual(capacity.pool_block_prefix(pool, record, [nodes[0]]), 24)
        self.assertEqual(capacity.pool_block_prefix(pool, record, [node_with("10.4.0.0/26"), node_with("10.4.1.0/24")]), 24)  # the largest block among the nodes
        pool["maxPodsConstraint"] = {"maxPodsPerNode": "32"}
        self.assertEqual(capacity.pool_block_prefix(pool, record, []), 26)  # no node yet: the pool's limit
        del pool["maxPodsConstraint"]
        record["defaultMaxPodsConstraint"] = {"maxPodsPerNode": "64"}
        self.assertEqual(capacity.pool_block_prefix(pool, record, []), 25)  # the cluster's default, told from the /24 fallback
        del record["defaultMaxPodsConstraint"]
        self.assertEqual(capacity.pool_block_prefix(pool, record, []), 24)  # the fallback
        # Free blocks are counted at the unit's alignment: a /26 node holds the /24 it sits in and
        # a /23 node holds two; a node outside the range, with no block or with one that does not
        # parse holds none of it; a unit larger than the range is the range itself.
        range_22 = capacity.pod_network("10.4.0.0/22")
        self.assertEqual(capacity.free_blocks(range_22, 24, [node_with("10.4.0.64/26")]), (3, 1, 4))
        self.assertEqual(capacity.free_blocks(range_22, 24, [node_with("10.4.2.0/23")]), (2, 2, 4))
        self.assertEqual(capacity.free_blocks(range_22, 26, [node_with("10.4.0.0/24")]), (12, 4, 16))
        self.assertEqual(capacity.free_blocks(range_22, 24, [node_with("10.9.0.0/24"), node_with(None), node_with("not-a-cidr")]), (4, 0, 4))
        self.assertEqual(capacity.free_blocks(capacity.pod_network("10.4.0.0/26"), 24, []), (1, 0, 1))
        # The pools' blocks differ: the largest is the unit, so the count is the conservative one.
        record, nodes = recorded()
        pod_range(record, "10.4.0.0/23")
        surge(record, 1)
        record["nodePools"][1]["config"]["reservationAffinity"] = {"consumeReservationType": "ANY_RESERVATION"}
        nodes[1]["spec"]["podCIDR"] = "10.4.1.0/26"
        finding = evaluate(record, nodes, target=GPU_TARGET)["blocking"][0]
        self.assertEqual((finding["prefix"], finding["free"], finding["need"]), (24, 0, 2))

    def test_unknown_paths_of_the_surge_halves(self):
        record, nodes = recorded()
        self.assertIn("surge pool(s) default-pool: no target, so whether their surge nodes are needed in this upgrade is unknown", evaluate(record, nodes, target=None)["unknown"])
        # Nodes not read: the accelerator half still grades; the Pod range half is unknown.
        surge(record, 1)
        out = evaluate(record, nodes, target=GPU_TARGET, read_errors={"node": "nodes is forbidden"})
        self.assertEqual(([f["finding"] for f in out["blocking"]], [f["finding"] for f in out["risks"]]), ([], ["accelerator"]))
        self.assertIn("pod range headroom for the surge node(s) of pool(s) default-pool (maxSurge 1), gpu-pool (maxSurge 1): node not read (nodes is forbidden), so the free blocks were not counted", out["unknown"])
        # A record with no Pod range under either spelling, or one that does not parse.
        record, nodes = recorded()
        del record["clusterIpv4Cidr"], record["ipAllocationPolicy"]
        for pool in record["nodePools"]:
            del pool["networkConfig"]
        self.assertIn("pod range headroom for the surge node(s) of pool(s) default-pool (maxSurge 1): the cluster record carries no Pod range, so the free blocks were not counted", evaluate(record, nodes, target=GPU_TARGET)["unknown"])
        record["clusterIpv4Cidr"] = "ten-dot-four"
        self.assertIn("the Pod range 'ten-dot-four' did not parse", evaluate(record, nodes, target=GPU_TARGET)["unknown"][0])
        record["clusterIpv4Cidr"] = "fd00::/64"  # an IPv6 range is not where IPv4 blocks come from
        self.assertIn("did not parse", evaluate(record, nodes, target=GPU_TARGET)["unknown"][0])
        # An unparsable pool version, and Autopilot.
        record, nodes = recorded()
        record["nodePools"][0]["version"] = "latest"
        self.assertIn("surge pool default-pool: version unparsable, so whether its surge nodes are needed in this upgrade is unknown", evaluate(record, nodes, target=GPU_TARGET)["unknown"])
        record, nodes = recorded()
        pod_range(record, "10.4.0.0/23")
        surge(record, 1)
        record["autopilot"] = {"enabled": True}
        self.assertEqual(evaluate(record, nodes, target=GPU_TARGET), shared.new_result())


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
        record["clusterIpv4Cidr"] = "10.124.0.0/14"
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

    def test_the_pod_range_shortfall_reaches_the_table_through_the_projected_node_read(self):
        """The nodes' blocks come through the jsonpath read's `spec.podCIDR` field, so a range
        with no free block blocks the member in the table and the JSON."""
        state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state_dir, True)
        kubeconfig_dir = os.path.join(tempfile.mkdtemp(), "kubeconfigs")
        self.addCleanup(shutil.rmtree, os.path.dirname(kubeconfig_dir), True)
        record = report_cluster("gpu-canary", "us-central1-a", LAGGING, [("default-pool", LAGGING), ("gpu-pool", LAGGING)])
        record["clusterIpv4Cidr"] = "10.4.0.0/23"
        record["nodePools"][1]["config"] = {"accelerators": [{"acceleratorCount": "1", "acceleratorType": "nvidia-l4"}], "reservationAffinity": {"consumeReservationType": "ANY_RESERVATION"}}
        nodes = [node_with("10.4.0.0/24", "gke-gpu-canary-default-1", "default-pool"), node_with("10.4.1.0/24", "gke-gpu-canary-gpu-1", "gpu-pool")]
        fake = FakeReadinessCommands({"p1": [record]}, {}, {"gpu-canary": []}, pods_by_cluster={"gpu-canary": []}, nodes_by_cluster={"gpu-canary": nodes})
        out_path = os.path.join(state_dir, "report.json")
        with patch.object(report, "run_cmd", fake), redirect_stdout(io.StringIO()) as stdout:
            rc = report.main(["--state-dir", state_dir, "--project", "p1", "--target-version", TARGET_TEXT, "--readiness", "--at", AT, "--kubeconfig-dir", kubeconfig_dir, "--output", out_path])
        self.assertEqual(rc, report.EXIT_OK)
        with open(out_path, encoding="utf-8") as f:
            readiness = json.load(f)["members"][0]["readiness"]
        self.assertEqual(readiness["status"], "blocked")
        self.assertEqual([(f["rule"], f["finding"], f["free"], f["need"]) for f in readiness["rule_blocking"]], [("surge-capacity", "pod-range", 0, 2)])
        self.assertEqual(readiness["rules"]["surge-capacity"], {"entry": 2, "can_block": True, "blocking": 1, "risks": 0, "unknown": 0, "notes": 1})
        self.assertIn("surge-capacity: pool gpu-pool (1 x nvidia-l4) surges (maxSurge 1) under reservation affinity ANY_RESERVATION", readiness["notes"])
        row = next(line for line in stdout.getvalue().splitlines() if line.startswith("| p1 | gpu-canary |") and "| blocked |" in line)
        self.assertIn("pod range 10.4.0.0/23 has 0 free /24 block(s) (2 of 2 hold a node) and the 2 surge node(s) of pool(s) default-pool (maxSurge 1), gpu-pool (maxSurge 1) each need one", row)


if __name__ == "__main__":
    unittest.main()
