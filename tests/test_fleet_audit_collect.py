"""Unit tests for the untargeted-compute-class-workload fleet-audit check."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure collect.py is importable
scripts_dir = Path(__file__).resolve().parent.parent / "agents/platform/skills/fleet-audit/scripts"
sys.path.insert(0, str(scripts_dir))

import collect  # noqa: E402


def compute_class(name, taints=None, priorities=None):
    spec = {}
    if taints is not None:
        spec["nodePoolConfig"] = {"taints": taints}
    if priorities is not None:
        spec["priorities"] = priorities
    return {
        "apiVersion": "computeclasses.cloud.google.com/v1",
        "kind": "ComputeClass",
        "metadata": {"name": name, "labels": {}, "annotations": {}},
        "spec": spec,
    }


def node(name, labels=None, taints=None):
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {"name": name, "labels": labels or {}},
        "spec": {"taints": taints or []},
    }


def pool(name, labels=None, taints=None, autoscaling=None, status="RUNNING", machine_type=None, image_type=None, initial_node_count=1, locations=None, spot=None, preemptible=None, placement_policy=None):
    config = {}
    if labels is not None:
        config["labels"] = labels
    if taints is not None:
        config["taints"] = taints
    if machine_type is not None:
        config["machineType"] = machine_type
    if image_type is not None:
        config["imageType"] = image_type
    if spot is not None:
        config["spot"] = spot
    if preemptible is not None:
        config["preemptible"] = preemptible
    res = {"name": name, "status": status, "config": config, "initialNodeCount": initial_node_count}
    if autoscaling is not None:
        res["autoscaling"] = autoscaling
    if locations is not None:
        res["locations"] = locations
    if placement_policy is not None:
        res["placementPolicy"] = placement_policy
    return res


def namespace(name, labels=None):
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": name, "labels": labels or {}},
    }


def deployment(name, ns="default", node_selector=None, node_affinity=None, tolerations=None):
    spec_template = {
        "metadata": {"labels": {"app": name}},
        "spec": {
            "containers": [{"name": "app", "resources": {}}],
            "nodeSelector": node_selector or {},
            "tolerations": tolerations or [],
        },
    }
    if node_affinity:
        spec_template["spec"]["affinity"] = {"nodeAffinity": node_affinity}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": {}, "annotations": {}},
        "spec": {
            "replicas": 2,
            "template": spec_template,
        },
    }


class TestUntargetedComputeClassWorkload(unittest.TestCase):
    def setUp(self):
        self.cc = compute_class("standard-cc")
        self.base_pool = pool(
            "pool-1",
            labels={"cloud.google.com/compute-class": "standard-cc"},
            taints=[],
        )
        self.base_node = node("node-1", labels={"cloud.google.com/gke-nodepool": "pool-1"})
        self.ns = namespace("default")

    def test_positive_untainted_cc_base_nodes_without_selector_emits_finding(self):
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["object"], "Deployment/api")
        self.assertIn("cloud.google.com/compute-class", hit["excerpt"])
        self.assertEqual(hit.get("single_compute_class"), "standard-cc")

    def test_negative_cluster_with_default_compute_class(self):
        default_cc = compute_class("default")
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc, default_cc],
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_namespace_with_default_compute_class_label(self):
        labeled_ns = namespace("default", labels={"cloud.google.com/default-compute-class": "standard-cc"})
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool],
            "namespaces": [labeled_ns],
            "nodes": [self.base_node],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_namespace_with_default_compute_class_non_daemonset_label(self):
        labeled_ns = namespace("default", labels={"cloud.google.com/default-compute-class-non-daemonset": "standard-cc"})
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool],
            "namespaces": [labeled_ns],
            "nodes": [self.base_node],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_workload_with_explicit_node_selector(self):
        wl = collect.normalize_workloads({
            "items": [deployment("api", node_selector={"cloud.google.com/compute-class": "standard-cc"})]
        })[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_workload_with_explicit_node_affinity(self):
        affinity = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "cloud.google.com/compute-class",
                                "operator": "In",
                                "values": ["standard-cc"],
                            }
                        ]
                    }
                ]
            }
        }
        wl = collect.normalize_workloads({
            "items": [deployment("api", node_affinity=affinity)]
        })[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_workload_tolerating_dedicated_tainted_pool(self):
        gpu_pool = pool(
            "gpu-pool",
            labels={"accelerator": "nvidia-t4"},
            taints=[{"key": "nvidia.com/gpu", "value": "present", "effect": "NO_SCHEDULE"}],
        )
        wl = collect.normalize_workloads({
            "items": [deployment("gpu-worker", tolerations=[{"key": "nvidia.com/gpu", "operator": "Exists"}])]
        })[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool, gpu_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node, node("node-gpu", labels={"cloud.google.com/gke-nodepool": "gpu-pool"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_cluster_with_untainted_nodes_lacking_compute_class(self):
        general_pool = pool("standard-pool", labels={"node-role": "worker"}, taints=[])
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool, general_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node, node("node-general", labels={"cloud.google.com/gke-nodepool": "standard-pool"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_cluster_without_compute_classes(self):
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [],
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_single_compute_class_emitted_on_candidate_when_matching(self):
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node],
        }
        spec = collect.CheckSpec(
            "untargeted-compute-class-workload",
            "workload",
            collect.check_untargeted_compute_class_workload,
            "major",
            None,
            "impact description",
        )
        declarations = {("test-cluster", "Deployment", "default", "api"): {"clusters/test-cluster/workloads/api.yaml"}}
        collected = collect.CollectedContext(ctx, [wl], {"untargeted-compute-class-workload": {}})
        result = collect.collect_cluster(
            {"name": "test-cluster", "project": "proj", "location": "loc"},
            checks=(spec,),
            collected=collected,
            declarations=declarations,
        )
        candidates = result.get("candidates") or []
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["single_compute_class"], "standard-cc")
        self.assertNotIn("remediation", candidates[0])

    def test_node_and_compute_class_name_divergence_clears_single_compute_class(self):
        burst_cc = compute_class("burst")
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [burst_cc],
            "node_pools": [self.base_pool],  # labeled standard-cc
            "namespaces": [self.ns],
            "nodes": [self.base_node],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "")
        self.assertTrue(hit["multiple_compute_classes"])

    def test_controller_cordon_taint_on_non_cc_node_does_not_cause_false_major(self):
        cordoned_non_cc_pool = pool(
            "non-cc-1",
            labels={"node-role": "worker"},
            taints=[{"key": "node.kubernetes.io/unschedulable", "effect": "NO_SCHEDULE"}],
        )
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool, cordoned_non_cc_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node, node("node-cordoned", labels={"cloud.google.com/gke-nodepool": "non-cc-1"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_accelerator_compute_class_with_gpu_or_tpu_excluded(self):
        gpu_cc = compute_class("gpu-l4", priorities=[{"gpu": {"count": 1, "type": "nvidia-l4"}}])
        tpu_cc = compute_class("tpu-v5e", priorities=[{"tpu": {"count": 1, "type": "v5e"}}])
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc, gpu_cc, tpu_cc],
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "standard-cc")
        self.assertFalse(hit["multiple_compute_classes"])

    def test_builtin_autopilot_classes_excluded(self):
        ap_cc = compute_class("autopilot")
        ap_spot_cc = compute_class("autopilot-spot")
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc, ap_cc, ap_spot_cc],
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "standard-cc")
        self.assertFalse(hit["multiple_compute_classes"])

    def test_affinity_not_in_or_preferred_does_not_silence_check(self):
        negative_affinity = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "cloud.google.com/compute-class",
                                "operator": "NotIn",
                                "values": ["other-cc"],
                            }
                        ]
                    }
                ]
            }
        }
        wl = collect.normalize_workloads({
            "items": [deployment("api", node_affinity=negative_affinity)]
        })[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)

        preferred_affinity = {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "weight": 1,
                    "preference": {
                        "matchExpressions": [
                            {
                                "key": "cloud.google.com/compute-class",
                                "operator": "In",
                                "values": ["standard-cc"],
                            }
                        ]
                    },
                }
            ]
        }
        wl_pref = collect.normalize_workloads({
            "items": [deployment("api-pref", node_affinity=preferred_affinity)]
        })[0]
        hit_pref = collect.check_untargeted_compute_class_workload(wl_pref, ctx)
        self.assertIsNotNone(hit_pref)

        dne_affinity = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "cloud.google.com/gke-spot",
                                "operator": "DoesNotExist",
                            }
                        ]
                    }
                ]
            }
        }
        wl_dne = collect.normalize_workloads({
            "items": [deployment("api-dne", node_affinity=dne_affinity)]
        })[0]
        hit_dne = collect.check_untargeted_compute_class_workload(wl_dne, ctx)
        self.assertIsNotNone(hit_dne)

    def test_the_dump_asks_for_nodes_and_namespaces(self):
        self.assertIn("nodes", collect.DUMP_COMMAND_KINDS)
        self.assertIn("namespaces", collect.DUMP_COMMAND_KINDS)

    def test_negative_non_cc_pool_autoscaled_to_zero_does_not_flag_workloads(self):
        # A general-purpose pool with 0 live nodes (autoscaling min 0) provides untainted
        # capacity outside ComputeClass, preventing false major findings.
        cc_pool = pool("cc-pool", labels={"cloud.google.com/compute-class": "standard-cc"})
        zero_pool = pool(
            "general-pool",
            labels={},
            taints=[],
            autoscaling={"enabled": True, "minNodeCount": 0, "maxNodeCount": 5},
            initial_node_count=0,
        )
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [cc_pool, zero_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-cc", labels={"cloud.google.com/gke-nodepool": "cc-pool"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_dedicated_taint_on_compute_class_is_excluded(self):
        dedicated_cc = compute_class("dedicated", taints=[{"key": "team", "effect": "NoSchedule"}])
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc, dedicated_cc],
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "standard-cc")
        self.assertFalse(hit["multiple_compute_classes"])

    def test_compute_class_referencing_tainted_manual_nodepool_is_excluded(self):
        manual_pool_cc = compute_class("manual-pool-cc", priorities=[{"nodepools": ["pool-a"]}])
        pool_a = pool(
            "pool-a",
            labels={"cloud.google.com/compute-class": "standard-cc"},
            taints=[{"key": "workload-specific", "value": "true", "effect": "NO_SCHEDULE"}],
        )
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc, manual_pool_cc],
            "node_pools": [self.base_pool, pool_a],
            "namespaces": [self.ns],
            "nodes": [self.base_node, node("node-a", labels={"cloud.google.com/gke-nodepool": "pool-a"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "standard-cc")
        self.assertFalse(hit["multiple_compute_classes"])

    def test_compute_class_with_all_labeled_nodes_carrying_workload_taint_is_excluded(self):
        custom_cc = compute_class("custom-tainted")
        custom_pool = pool(
            "custom-pool",
            labels={"cloud.google.com/compute-class": "custom-tainted"},
            taints=[{"key": "workload-pin", "value": "special", "effect": "NO_SCHEDULE"}],
        )
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc, custom_cc],
            "node_pools": [self.base_pool, custom_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node, node("node-custom", labels={"cloud.google.com/gke-nodepool": "custom-pool"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "standard-cc")
        self.assertFalse(hit["multiple_compute_classes"])

    def test_tolerations_keyless_exists_with_effect_distinction(self):
        dedicated_non_cc = pool(
            "non-cc-dedicated",
            labels={"node-role": "worker"},
            taints=[{"key": "dedicated-pool", "effect": "NO_SCHEDULE"}],
        )
        # Pod tolerates Exists with effect NoExecute -> does NOT tolerate NO_SCHEDULE -> flags
        wl_noexec = collect.normalize_workloads({
            "items": [deployment("api-noexec", tolerations=[{"operator": "Exists", "effect": "NoExecute"}])]
        })[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool, dedicated_non_cc],
            "namespaces": [self.ns],
            "nodes": [self.base_node, node("node-ded", labels={"cloud.google.com/gke-nodepool": "non-cc-dedicated"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl_noexec, ctx)
        self.assertIsNotNone(hit)

        # Pod tolerates Exists with effect NoSchedule -> DOES tolerate NO_SCHEDULE -> does not flag
        wl_nosched = collect.normalize_workloads({
            "items": [deployment("api-nosched", tolerations=[{"operator": "Exists", "effect": "NoSchedule"}])]
        })[0]
        hit_nosched = collect.check_untargeted_compute_class_workload(wl_nosched, ctx)
        self.assertIsNone(hit_nosched)

    def test_empty_string_compute_class_label_treated_as_non_cc_pool(self):
        # A pool with cloud.google.com/compute-class: "" and a workload taint
        dedicated_empty_cc = pool(
            "dedicated-empty-cc",
            labels={collect.COMPUTE_CLASS_LABEL: ""},
            taints=[{"key": "team", "value": "x", "effect": "NO_SCHEDULE"}],
        )
        # Deployment tolerates team=x -> can run on dedicated_empty_cc -> should NOT be flagged
        wl_tolerating = collect.normalize_workloads({
            "items": [deployment("api-tol", tolerations=[{"key": "team", "value": "x", "operator": "Equal", "effect": "NoSchedule"}])]
        })[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool, dedicated_empty_cc],
            "namespaces": [self.ns],
            "nodes": [self.base_node, node("node-empty", labels={"cloud.google.com/gke-nodepool": "dedicated-empty-cc"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl_tolerating, ctx)
        self.assertIsNone(hit)

        # Deployment does NOT tolerate team=x -> cannot run on dedicated_empty_cc -> flagged
        wl_not_tolerating = collect.normalize_workloads({
            "items": [deployment("api-nottol", tolerations=[])]
        })[0]
        hit_not_tol = collect.check_untargeted_compute_class_workload(wl_not_tolerating, ctx)
        self.assertIsNotNone(hit_not_tol)

    def test_workload_pinned_to_tainted_compute_class_pool_resolves_to_that_pool_class(self):
        base_pool = pool(
            "base-pool",
            labels={collect.COMPUTE_CLASS_LABEL: "base"},
        )
        batch_pool = pool(
            "batch-pool",
            labels={collect.COMPUTE_CLASS_LABEL: "batch"},
            taints=[{"key": collect.COMPUTE_CLASS_LABEL, "value": "batch", "effect": "NO_SCHEDULE"}],
        )
        cc_base = compute_class("base")
        cc_batch = compute_class("batch")
        ctx = {
            "compute_classes": [cc_base, cc_batch],
            "node_pools": [base_pool, batch_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}), node("node-batch", labels={"cloud.google.com/gke-nodepool": "batch-pool"})],
        }

        # Workload pinned to batch-pool by nodepool selector and tolerating batch taint:
        wl_batch = collect.normalize_workloads({
            "items": [deployment(
                "batch-worker",
                node_selector={"cloud.google.com/gke-nodepool": "batch-pool"},
                tolerations=[{"key": collect.COMPUTE_CLASS_LABEL, "value": "batch", "operator": "Equal", "effect": "NoSchedule"}],
            )]
        })[0]
        hit_batch = collect.check_untargeted_compute_class_workload(wl_batch, ctx)
        self.assertIsNotNone(hit_batch)
        self.assertEqual(hit_batch["single_compute_class"], "batch")

        # Unpinned workload (no selector, no tolerations) can only land on base-pool:
        wl_unpinned = collect.normalize_workloads({
            "items": [deployment("generic-worker")]
        })[0]
        hit_unpinned = collect.check_untargeted_compute_class_workload(wl_unpinned, ctx)
        self.assertIsNotNone(hit_unpinned)
        self.assertEqual(hit_unpinned["single_compute_class"], "base")

        # Workload with selector matching batch-pool but lacking toleration cannot schedule anywhere:
        wl_conflicted = collect.normalize_workloads({
            "items": [deployment(
                "conflicted-worker",
                node_selector={"cloud.google.com/gke-nodepool": "batch-pool"},
            )]
        })[0]
        hit_conflicted = collect.check_untargeted_compute_class_workload(wl_conflicted, ctx)
        self.assertIsNone(hit_conflicted)

    def test_stopped_or_zero_sized_non_cc_pool_does_not_clear_cluster_capacity(self):
        cc_pool = pool("cc-pool", labels={"cloud.google.com/compute-class": "base"})
        stopped_pool = pool("stopped-pool", labels={}, autoscaling={"enabled": True})
        stopped_pool["status"] = "STOPPING"
        error_pool = pool("error-pool", labels={}, autoscaling={"enabled": True})
        error_pool["status"] = "ERROR"
        zero_pool = pool(
            "parked-zero-pool",
            labels={},
            autoscaling={"enabled": False},
        )
        zero_pool["initialNodeCount"] = 0
        cc = compute_class("base")
        ctx = {
            "compute_classes": [cc],
            "node_pools": [cc_pool, stopped_pool, error_pool, zero_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-cc", labels={"cloud.google.com/gke-nodepool": "cc-pool"})],
        }
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "base")

    def test_multiple_untainted_general_purpose_compute_classes_clears_single_compute_class(self):
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"})
        cc_base = compute_class("base")
        cc_burst = compute_class("burst")
        ctx = {
            "compute_classes": [cc_base, cc_burst],
            "node_pools": [base_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"})],
        }
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "")
        self.assertTrue(hit["multiple_compute_classes"])

    def test_workload_selecting_gke_nodepool_with_empty_config_labels_matches(self):
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"})
        gpu_pool = pool(
            "gpu-pool",
            labels={},
            taints=[{"key": "nvidia.com/gpu", "value": "present", "effect": "NO_SCHEDULE"}],
        )
        cc_base = compute_class("base")
        ctx = {
            "compute_classes": [cc_base],
            "node_pools": [base_pool, gpu_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}), node("node-gpu", labels={"cloud.google.com/gke-nodepool": "gpu-pool"})],
        }
        wl = collect.normalize_workloads({
            "items": [deployment(
                "gpu-worker",
                node_selector={"cloud.google.com/gke-nodepool": "gpu-pool"},
                tolerations=[{"key": "nvidia.com/gpu", "value": "present", "effect": "NoSchedule"}],
            )]
        })[0]
        # Pinned to non-CC gpu-pool with empty config.labels and tolerating its taint -> non-CC capacity available -> not flagged
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_workload_with_unresolvable_unknown_selector_key_is_not_flagged(self):
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"}, locations=["us-central1-a"])
        unlabelled_pool = pool("unlabelled-pool", labels={}, locations=[])
        cc_base = compute_class("base")
        ctx = {
            "compute_classes": [cc_base],
            "node_pools": [base_pool, unlabelled_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}), node("node-unlabelled", labels={"cloud.google.com/gke-nodepool": "unlabelled-pool"})],
        }
        wl = collect.normalize_workloads({
            "items": [deployment(
                "custom-worker",
                node_selector={"topology.kubernetes.io/zone": "us-central1-a"},
            )]
        })[0]
        # Because unlabelled-pool has no locations in inventory, its zone placement is unknown (None),
        # so the collector clears the workload rather than flagging base-pool as an unambiguous CC target.
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_transient_controller_and_cloud_provider_taints_on_non_cc_nodes(self):
        ca_pool = pool(
            "non-cc-ca",
            labels={"node-role": "worker"},
            taints=[{"key": "ToBeDeletedByClusterAutoscaler", "effect": "NO_SCHEDULE"}],
        )
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx_ca = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool, ca_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node, node("node-ca", labels={"cloud.google.com/gke-nodepool": "non-cc-ca"})],
        }
        self.assertIsNone(collect.check_untargeted_compute_class_workload(wl, ctx_ca))

        spot_pool = pool(
            "non-cc-spot",
            labels={"node-role": "worker"},
            taints=[{"key": "cloud.google.com/impending-node-termination", "effect": "NO_SCHEDULE"}],
        )
        ctx_spot = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool, spot_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node, node("node-spot", labels={"cloud.google.com/gke-nodepool": "non-cc-spot"})],
        }
        self.assertIsNone(collect.check_untargeted_compute_class_workload(wl, ctx_spot))

        uninit_pool = pool(
            "non-cc-uninit",
            labels={"node-role": "worker"},
            taints=[{"key": "node.cloudprovider.kubernetes.io/uninitialized", "effect": "NO_SCHEDULE"}],
        )
        ctx_uninit = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool, uninit_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node, node("node-uninit", labels={"cloud.google.com/gke-nodepool": "non-cc-uninit"})],
        }
        self.assertIsNone(collect.check_untargeted_compute_class_workload(wl, ctx_uninit))

        win_pool = pool(
            "non-cc-win",
            labels={"node-role": "worker"},
            taints=[{"key": "node.kubernetes.io/os", "value": "windows", "effect": "NO_SCHEDULE"}],
        )
        ctx_win = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool, win_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node, node("node-win", labels={"cloud.google.com/gke-nodepool": "non-cc-win"})],
        }
        self.assertIsNotNone(collect.check_untargeted_compute_class_workload(wl, ctx_win))

    def _create_dump_file(self, fake_dump):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        dump_path = Path(tmp_dir.name) / "test_dump.json"
        dump_path.write_text(json.dumps(fake_dump), encoding="utf-8")
        return dump_path

    def test_collect_obtainability_crd_absent_sets_not_applicable(self):
        spec = collect.CheckSpec(
            "untargeted-compute-class-workload",
            "workload",
            collect.check_untargeted_compute_class_workload,
            "major",
            None,
            "impact description",
        )
        fake_dump = {
            "items": [deployment("api"), self.base_node, self.ns]
        }
        tmp_dump = self._create_dump_file(fake_dump)
        with patch.object(collect, "dump_state") as mock_dump:
            mock_dump.return_value = (tmp_dump, MagicMock(rc=0, duration_s=0.1, stdout="{}"), True)

            with patch.object(collect, "run_and_gate") as mock_run_and_gate:
                mock_run_and_gate.return_value = (
                    None,
                    MagicMock(rc=1, stderr="error: the server doesn't have a resource type 'computeclasses'"),
                )
                cc_context = collect._collect_obtainability(
                    {"name": "c1", "project": "p1", "location": "l1"},
                    Path("/fake/kubeconfig"),
                    (spec,),
                    run=MagicMock(),
                )
                self.assertIn("untargeted-compute-class-workload", cc_context.context.get("not_applicable", {}))
                self.assertEqual([self.base_node], cc_context.context.get("nodes"))
                self.assertEqual([self.ns], cc_context.context.get("namespaces"))

    def test_collect_obtainability_timeout_sets_unevaluated(self):
        spec = collect.CheckSpec(
            "untargeted-compute-class-workload",
            "workload",
            collect.check_untargeted_compute_class_workload,
            "major",
            None,
            "impact description",
        )
        fake_dump = {
            "items": [deployment("api"), self.base_node, self.ns]
        }
        tmp_dump = self._create_dump_file(fake_dump)
        with patch.object(collect, "dump_state") as mock_dump:
            mock_dump.return_value = (tmp_dump, MagicMock(rc=0, duration_s=0.1, stdout="{}"), True)

            with patch.object(collect, "run_and_gate") as mock_run_and_gate:
                mock_run_and_gate.return_value = (
                    None,
                    MagicMock(rc=124, stderr="command timed out after 60s"),
                )
                cc_context = collect._collect_obtainability(
                    {"name": "c1", "project": "p1", "location": "l1"},
                    Path("/fake/kubeconfig"),
                    (spec,),
                    run=MagicMock(),
                )
                self.assertIn("untargeted-compute-class-workload", cc_context.context.get("unevaluated", {}))
                self.assertEqual([self.base_node], cc_context.context.get("nodes"))
                self.assertEqual([self.ns], cc_context.context.get("namespaces"))

    def test_collect_obtainability_success_records_computeclasses_command(self):
        spec = collect.CheckSpec(
            "untargeted-compute-class-workload",
            "workload",
            collect.check_untargeted_compute_class_workload,
            "major",
            None,
            "impact description",
        )
        fake_dump = {
            "items": [deployment("api"), self.base_node, self.ns]
        }
        tmp_dump = self._create_dump_file(fake_dump)
        cc_stdout = json.dumps({"items": [self.cc]})
        test_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "standard-cc"})
        np_stdout = json.dumps([test_pool])
        with patch.object(collect, "dump_state") as mock_dump:
            mock_dump.return_value = (tmp_dump, MagicMock(rc=0, duration_s=0.1, stdout="{}"), True)

            with patch.object(collect, "run_and_gate") as mock_run_and_gate:
                mock_run_and_gate.side_effect = [
                    ({"items": [self.cc]}, MagicMock(rc=0, duration_s=0.05, stdout=cc_stdout)),
                    ([test_pool], MagicMock(rc=0, duration_s=0.08, stdout=np_stdout)),
                ]
                cc_context = collect._collect_obtainability(
                    {"name": "c1", "project": "p1", "location": "l1"},
                    Path("/fake/kubeconfig"),
                    (spec,),
                    run=MagicMock(),
                )
                self.assertEqual([self.base_node], cc_context.context.get("nodes"))
                self.assertEqual([self.ns], cc_context.context.get("namespaces"))
                self.assertEqual([self.cc], cc_context.context.get("compute_classes"))
                self.assertEqual([test_pool], cc_context.context.get("node_pools"))
                cmd_rec = cc_context.commands.get("untargeted-compute-class-workload")
                self.assertIsNotNone(cmd_rec)
                self.assertIn("kubectl get computeclasses -A -o json", cmd_rec["command"])
                self.assertEqual(0, cmd_rec["rc"])
                self.assertEqual(0.05, cmd_rec["duration_s"])
                self.assertEqual(cmd_rec["output_sha256"], collect.output_digest(cc_stdout))

    def test_collect_obtainability_autopilot_skips_node_pools_and_computeclasses(self):
        spec = collect.CheckSpec(
            "untargeted-compute-class-workload",
            "workload",
            collect.check_untargeted_compute_class_workload,
            "major",
            None,
            "impact description",
        )
        fake_dump = {"items": [deployment("api"), self.ns]}
        tmp_dump = self._create_dump_file(fake_dump)
        with patch.object(collect, "dump_state") as mock_dump:
            mock_dump.return_value = (tmp_dump, MagicMock(rc=0, duration_s=0.1, stdout="{}"), True)

            with patch.object(collect, "run_and_gate") as mock_run_and_gate:
                cc_context = collect._collect_obtainability(
                    {"name": "c1", "project": "p1", "location": "l1", "autopilot": True},
                    Path("/fake/kubeconfig"),
                    (spec,),
                    run=MagicMock(),
                )
                # On Autopilot, neither computeclasses nor node-pools list is issued; check is marked not_applicable directly
                self.assertEqual(0, mock_run_and_gate.call_count)
                self.assertIn("untargeted-compute-class-workload", cc_context.context.get("not_applicable", {}))
                self.assertNotIn("untargeted-compute-class-workload", cc_context.commands)

    def test_collect_obtainability_node_pools_failure_sets_unevaluated(self):
        spec = collect.CheckSpec(
            "untargeted-compute-class-workload",
            "workload",
            collect.check_untargeted_compute_class_workload,
            "major",
            None,
            "impact description",
        )
        fake_dump = {"items": [deployment("api"), self.ns]}
        tmp_dump = self._create_dump_file(fake_dump)
        cc_stdout = json.dumps({"items": [self.cc]})
        with patch.object(collect, "dump_state") as mock_dump:
            mock_dump.return_value = (tmp_dump, MagicMock(rc=0, duration_s=0.1, stdout="{}"), True)

            with patch.object(collect, "run_and_gate") as mock_run_and_gate:
                mock_run_and_gate.side_effect = [
                    ({"items": [self.cc]}, MagicMock(rc=0, duration_s=0.05, stdout=cc_stdout)),
                    (None, MagicMock(rc=1, stderr="ERROR: (gcloud.container.node-pools.list) Quota exceeded")),
                ]
                cc_context = collect._collect_obtainability(
                    {"name": "c1", "project": "p1", "location": "l1"},
                    Path("/fake/kubeconfig"),
                    (spec,),
                    run=MagicMock(),
                )
                # A failure on node-pools list does not gate-fail the cluster; it sets unevaluated
                self.assertIn("untargeted-compute-class-workload", cc_context.context.get("unevaluated", {}))
                self.assertNotIn("untargeted-compute-class-workload", cc_context.commands)

    def test_non_cc_pool_in_running_with_error_clears_cluster_capacity(self):
        # A pool in RUNNING_WITH_ERROR provides active capacity, clearing untargeted workloads
        cc_pool = pool("cc-pool", labels={"cloud.google.com/compute-class": "standard-cc"})
        err_pool = pool("err-pool", status="RUNNING_WITH_ERROR", labels={})
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [cc_pool, err_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-cc", labels={"cloud.google.com/gke-nodepool": "cc-pool"}), node("node-err", labels={"cloud.google.com/gke-nodepool": "err-pool"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_workload_selecting_user_label_present_on_one_pool_and_absent_on_another_flags_matching_pool(self):
        # When a user label is present on one pool and absent on another, the absent pool is a definite mismatch,
        # not an unknown key, allowing the matching pool to be flagged with single_compute_class.
        base_pool = pool("base", labels={"cloud.google.com/compute-class": "base", "workload-tier": "web"})
        gpu_pool = pool("gpu", labels={}, taints=[{"key": "nvidia.com/gpu", "value": "present", "effect": "NO_SCHEDULE"}])
        cc_base = compute_class("base")
        wl = collect.normalize_workloads({
            "items": [deployment("web", node_selector={"workload-tier": "web"})]
        })[0]
        ctx = {
            "compute_classes": [cc_base],
            "node_pools": [base_pool, gpu_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base"}), node("node-gpu", labels={"cloud.google.com/gke-nodepool": "gpu"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "base")

    def test_workload_pinned_by_required_node_affinity_resolves_to_pinned_compute_class(self):
        # Required nodeAffinity pointing at a specific pool resolves single_compute_class
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"})
        batch_pool = pool(
            "batch-pool",
            labels={"cloud.google.com/compute-class": "batch"},
            taints=[{"key": "cloud.google.com/compute-class", "value": "batch", "effect": "NO_SCHEDULE"}],
        )
        cc_base = compute_class("base")
        cc_batch = compute_class("batch")
        affinity = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "cloud.google.com/gke-nodepool",
                                "operator": "In",
                                "values": ["batch-pool"],
                            }
                        ]
                    }
                ]
            }
        }
        tolerations = [{"key": "cloud.google.com/compute-class", "value": "batch", "operator": "Equal"}]
        wl = collect.normalize_workloads({
            "items": [deployment("batch-job", node_affinity=affinity, tolerations=tolerations)]
        })[0]
        ctx = {
            "compute_classes": [cc_base, cc_batch],
            "node_pools": [base_pool, batch_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}), node("node-batch", labels={"cloud.google.com/gke-nodepool": "batch-pool"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "batch")

    def test_workload_selecting_arm64_on_axion_pool_is_flagged_with_matching_class(self):
        axion_pool = pool(
            "axion-pool",
            machine_type="c4a-standard-4",
            labels={"cloud.google.com/compute-class": "axion-cc"},
            taints=[{"key": "kubernetes.io/arch", "value": "arm64", "effect": "NO_SCHEDULE"}],
        )
        cc_axion = compute_class("axion-cc")
        wl = collect.normalize_workloads({
            "items": [deployment("arm-api", node_selector={"kubernetes.io/arch": "arm64"})]
        })[0]
        ctx = {
            "compute_classes": [cc_axion],
            "node_pools": [axion_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-axion", labels={"cloud.google.com/gke-nodepool": "axion-pool"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "axion-cc")

    def test_windows_pool_derives_os_from_image_type(self):
        win_pool = pool(
            "win-pool",
            image_type="WINDOWS_LTSC_CONTAINERD",
            labels={"cloud.google.com/compute-class": "win-cc"},
            taints=[{"key": "node.kubernetes.io/os", "value": "windows", "effect": "NO_SCHEDULE"}],
        )
        cc_win = compute_class("win-cc")
        wl = collect.normalize_workloads({
            "items": [deployment("win-app", node_selector={"kubernetes.io/os": "windows"})]
        })[0]
        ctx = {
            "compute_classes": [cc_win],
            "node_pools": [win_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-win", labels={"cloud.google.com/gke-nodepool": "win-pool"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "win-cc")

    def test_workload_selecting_amd64_on_n4_pool_is_flagged_with_matching_class(self):
        n4_pool = pool("n4-pool", machine_type="n4-standard-4", labels={"cloud.google.com/compute-class": "n4-cc"})
        cc_n4 = compute_class("n4-cc")
        wl = collect.normalize_workloads({
            "items": [deployment("amd64-api", node_selector={"kubernetes.io/arch": "amd64"})]
        })[0]
        ctx = {
            "compute_classes": [cc_n4],
            "node_pools": [n4_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-n4", labels={"cloud.google.com/gke-nodepool": "n4-pool"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "n4-cc")

    def test_workload_selecting_user_label_matches_compute_class_pool_when_untainted_unlabelled_pool_exists(self):
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base", "tier": "web"})
        legacy_pool = pool("legacy-pool", labels={"tier": "batch"})
        cc_base = compute_class("base")
        wl_web = collect.normalize_workloads({
            "items": [deployment("web", node_selector={"tier": "web"})]
        })[0]
        wl_unconstrained = collect.normalize_workloads({
            "items": [deployment("generic")]
        })[0]
        ctx = {
            "compute_classes": [cc_base],
            "node_pools": [base_pool, legacy_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}), node("node-legacy", labels={"cloud.google.com/gke-nodepool": "legacy-pool"})],
        }
        hit_web = collect.check_untargeted_compute_class_workload(wl_web, ctx)
        self.assertIsNotNone(hit_web)
        self.assertEqual(hit_web["single_compute_class"], "base")

        hit_unconstrained = collect.check_untargeted_compute_class_workload(wl_unconstrained, ctx)
        self.assertIsNone(hit_unconstrained)

    def test_workload_selecting_arm64_on_a4x_gb200_pool_is_flagged_with_matching_class(self):
        a4x_pool = pool(
            "gb200-pool",
            labels={"cloud.google.com/compute-class": "gb200-cc"},
            machine_type="a4x-highgpu-4g",
        )
        cc_gb200 = compute_class("gb200-cc")
        wl = collect.normalize_workloads({
            "items": [deployment("gb200-worker", node_selector={"kubernetes.io/arch": "arm64"})]
        })[0]
        ctx = {
            "compute_classes": [cc_gb200],
            "node_pools": [a4x_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-gb200", labels={"cloud.google.com/gke-nodepool": "gb200-pool"})],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "gb200-cc")

    def test_workload_selecting_deprecated_beta_aliases_matches_pools(self):
        pool_item = pool(
            "linux-arm-pool",
            labels={"cloud.google.com/compute-class": "arm-cc"},
            machine_type="c4a-standard-4",
            image_type="COS_CONTAINERD",
            locations=["us-central1-a"],
        )
        cc = compute_class("arm-cc")
        ctx = {
            "compute_classes": [cc],
            "node_pools": [pool_item],
            "namespaces": [self.ns],
            "nodes": [node("node-linux-arm", labels={"cloud.google.com/gke-nodepool": "linux-arm-pool"})],
        }
        wl = collect.normalize_workloads({
            "items": [deployment("beta-worker", node_selector={
                "beta.kubernetes.io/arch": "arm64",
                "beta.kubernetes.io/os": "linux",
                "beta.kubernetes.io/instance-type": "c4a-standard-4",
                "failure-domain.beta.kubernetes.io/zone": "us-central1-a",
            })]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "arm-cc")

    def test_workload_with_node_affinity_match_fields_treated_as_unknown(self):
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"})
        other_pool = pool("other-pool", labels={"cloud.google.com/compute-class": "other-cc"})
        cc_base = compute_class("base")
        cc_other = compute_class("other-cc")
        ctx = {
            "compute_classes": [cc_base, cc_other],
            "node_pools": [base_pool, other_pool],
            "namespaces": [self.ns],
            "nodes": [
                node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}),
                node("node-other", labels={"cloud.google.com/gke-nodepool": "other-pool"}),
            ],
        }
        affinity = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchFields": [{"key": "metadata.name", "operator": "In", "values": ["node-1"]}]},
                    {"matchExpressions": [{"key": "cloud.google.com/gke-nodepool", "operator": "In", "values": ["base-pool"]}]},
                ]
            }
        }
        wl = collect.normalize_workloads({
            "items": [deployment("pinned-to-node", node_affinity=affinity)]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_workload_with_implicit_gke_managed_taint_tolerations_schedules(self):
        arm_pool = pool(
            "arm-pool",
            labels={"cloud.google.com/compute-class": "arm-cc"},
            machine_type="c4a-standard-4",
            taints=[{"key": "kubernetes.io/arch", "value": "arm64", "effect": "NO_SCHEDULE"}],
        )
        cc_arm = compute_class("arm-cc")
        ctx = {
            "compute_classes": [cc_arm],
            "node_pools": [arm_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-arm", labels={"cloud.google.com/gke-nodepool": "arm-pool"})],
        }
        # Workload selecting arm64 implicitly tolerates GKE's kubernetes.io/arch=arm64 taint
        wl = collect.normalize_workloads({
            "items": [deployment("arm-worker", node_selector={"kubernetes.io/arch": "arm64"})]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "arm-cc")

    def test_static_non_cc_pool_resized_to_zero_live_nodes_flags_workload(self):
        # Walk A: legacy pool was created with initialNodeCount: 3, but live nodes count is 0.
        # It does not provide active capacity, so untargeted workloads are flagged for base-cc.
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"}, autoscaling={"enabled": True})
        legacy_pool = pool("legacy-pool", labels={}, initial_node_count=3)
        cc_base = compute_class("base")
        ctx = {
            "compute_classes": [cc_base],
            "node_pools": [base_pool, legacy_pool],
            "namespaces": [self.ns],
            "nodes": [node("base-node-1", labels={"cloud.google.com/gke-nodepool": "base-pool"})],
        }
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "base")

    def test_static_non_cc_pool_grown_from_zero_provides_live_capacity_clearing_workload(self):
        # Walk B: legacy pool was created with initialNodeCount: 0, but grown to 6 live nodes.
        # It provides active non-CC capacity, so untargeted workloads can land there and are cleared.
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"}, autoscaling={"enabled": True})
        legacy_pool = pool("legacy-pool", labels={}, initial_node_count=0)
        cc_base = compute_class("base")
        legacy_nodes = [
            node(f"legacy-node-{i}", labels={"cloud.google.com/gke-nodepool": "legacy-pool"})
            for i in range(6)
        ]
        ctx = {
            "compute_classes": [cc_base],
            "node_pools": [base_pool, legacy_pool],
            "namespaces": [self.ns],
            "nodes": legacy_nodes,
        }
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_gpu_workload_cannot_schedule_on_non_accelerator_pool_preventing_false_pin(self):
        # Walk C: base pool (CC-labelled, no GPUs) and gpu pool (CC-labelled, GPU taints).
        # A workload requesting GPUs should resolve to gpu-cc, NOT base.
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"})
        gpu_pool = pool(
            "gpu-pool",
            labels={"cloud.google.com/compute-class": "gpu-cc"},
            taints=[{"key": "nvidia.com/gpu", "value": "present", "effect": "NO_SCHEDULE"}],
        )
        gpu_pool["config"]["accelerators"] = [{"acceleratorType": "nvidia-tesla-t4"}]
        cc_base = compute_class("base")
        cc_gpu = compute_class("gpu-cc", priorities=[{"gpu": {"type": "nvidia-tesla-t4"}}])
        ctx = {
            "compute_classes": [cc_base, cc_gpu],
            "node_pools": [base_pool, gpu_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}), node("node-gpu", labels={"cloud.google.com/gke-nodepool": "gpu-pool"})],
        }
        wl = collect.normalize_workloads({
            "items": [{
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "gpu-app", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "containers": [{
                                "name": "trainer",
                                "resources": {"limits": {"nvidia.com/gpu": "1"}},
                            }],
                        }
                    }
                }
            }]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "gpu-cc")

    def test_gpu_workload_on_unlabelled_gpu_pool_escapes(self):
        # Walk C unlabelled: unlabelled gpu pool exists in non_cc_pools.
        # GPU workload can schedule on it, so it escapes without being flagged or pinned to base.
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"})
        gpu_pool = pool(
            "gpu-pool",
            labels={},
            taints=[{"key": "nvidia.com/gpu", "value": "present", "effect": "NO_SCHEDULE"}],
        )
        gpu_pool["config"]["accelerators"] = [{"acceleratorType": "nvidia-tesla-t4"}]
        cc_base = compute_class("base")
        ctx = {
            "compute_classes": [cc_base],
            "node_pools": [base_pool, gpu_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}), node("node-gpu", labels={"cloud.google.com/gke-nodepool": "gpu-pool"})],
        }
        wl = collect.normalize_workloads({
            "items": [{
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "gpu-app", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "containers": [{
                                "name": "trainer",
                                "resources": {"limits": {"nvidia.com/gpu": "1"}},
                            }],
                        }
                    }
                }
            }]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_node_affinity_empty_term_matches_nothing(self):
        # In Kubernetes, an empty nodeSelectorTerm {} matches no nodes.
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"})
        cc_base = compute_class("base")
        ctx = {
            "compute_classes": [cc_base],
            "node_pools": [base_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"})],
        }
        affinity = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [{}]
            }
        }
        wl = collect.normalize_workloads({
            "items": [deployment("empty-term-app", node_affinity=affinity)]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_node_affinity_gt_lt_operator_treated_as_unknown(self):
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"})
        other_pool = pool("other-pool", labels={"cloud.google.com/compute-class": "other-cc"})
        cc_base = compute_class("base")
        cc_other = compute_class("other-cc")
        ctx = {
            "compute_classes": [cc_base, cc_other],
            "node_pools": [base_pool, other_pool],
            "namespaces": [self.ns],
            "nodes": [
                node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}),
                node("node-other", labels={"cloud.google.com/gke-nodepool": "other-pool"}),
            ],
        }
        affinity = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [{"key": "custom.io/score", "operator": "Gt", "values": ["5"]}]},
                    {"matchExpressions": [{"key": "cloud.google.com/gke-nodepool", "operator": "In", "values": ["base-pool"]}]},
                ]
            }
        }
        wl = collect.normalize_workloads({
            "items": [deployment("gt-operator-app", node_affinity=affinity)]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_node_affinity_exists_on_zone_key_matches_pool_with_locations(self):
        # Required nodeAffinity Exists on topology.kubernetes.io/zone matches pool with locations
        base_pool = pool(
            "base-pool",
            labels={"cloud.google.com/compute-class": "base"},
            locations=["us-central1-a"],
        )
        cc_base = compute_class("base")
        ctx = {
            "compute_classes": [cc_base],
            "node_pools": [base_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"})],
        }
        affinity = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [{"key": "topology.kubernetes.io/zone", "operator": "Exists"}]}
                ]
            }
        }
        wl = collect.normalize_workloads({
            "items": [deployment("zoned-app", node_affinity=affinity)]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "base")

    def test_node_affinity_does_not_exist_on_zone_key_fails_pool_with_locations(self):
        # Required nodeAffinity DoesNotExist on topology.kubernetes.io/zone fails pool with locations (evaluates to False, not None)
        base_pool = pool(
            "base-pool",
            labels={"cloud.google.com/compute-class": "base"},
            locations=["us-central1-a"],
        )
        other_pool = pool("other-pool", labels={"cloud.google.com/compute-class": "other-cc"})
        cc_base = compute_class("base")
        cc_other = compute_class("other-cc")
        ctx = {
            "compute_classes": [cc_base, cc_other],
            "node_pools": [base_pool, other_pool],
            "namespaces": [self.ns],
            "nodes": [
                node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}),
                node("node-other", labels={"cloud.google.com/gke-nodepool": "other-pool"}),
            ],
        }
        affinity = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [{"key": "topology.kubernetes.io/zone", "operator": "DoesNotExist"}]},
                    {"matchExpressions": [{"key": "cloud.google.com/gke-nodepool", "operator": "In", "values": ["other-pool"]}]},
                ]
            }
        }
        wl = collect.normalize_workloads({
            "items": [deployment("no-zone-app", node_affinity=affinity)]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "other-cc")

    def test_tpu_workload_matches_tpu_pool_preventing_false_clean(self):
        # Workload requesting google.com/tpu matches TPU pool (identified by TPU machine type,
        # with no accelerators config), preventing false clean.
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"}, machine_type="e2-standard-4")
        tpu_pool = pool(
            "tpu-pool",
            labels={"cloud.google.com/compute-class": "tpu-cc"},
            machine_type="ct5lp-hightpu-4t",
            taints=[{"key": "google.com/tpu", "value": "present", "effect": "NO_SCHEDULE"}],
            placement_policy={"tpuTopology": "2x2"},
        )
        cc_base = compute_class("base")
        cc_tpu = compute_class("tpu-cc", priorities=[{"tpu": {"count": 4, "type": "v5litepod"}}])
        ctx = {
            "compute_classes": [cc_base, cc_tpu],
            "node_pools": [base_pool, tpu_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}), node("node-tpu", labels={"cloud.google.com/gke-nodepool": "tpu-pool"})],
        }
        wl = collect.normalize_workloads({
            "items": [{
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "tpu-app", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "nodeSelector": {
                                "cloud.google.com/gke-tpu-accelerator": "tpu-v5-lite-podslice",
                                "cloud.google.com/gke-tpu-topology": "2x2",
                            },
                            "containers": [{
                                "name": "trainer",
                                "resources": {"limits": {"google.com/tpu": "4"}},
                            }],
                        }
                    }
                }
            }]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "tpu-cc")

    def test_tpu_workload_on_unlabelled_tpu_pool_escapes(self):
        # Workload requesting google.com/tpu can schedule on unlabelled TPU pool in non_cc_pools, so it escapes.
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"}, machine_type="e2-standard-4")
        tpu_pool = pool(
            "tpu-pool",
            labels={},
            machine_type="ct5lp-hightpu-4t",
            taints=[{"key": "google.com/tpu", "value": "present", "effect": "NO_SCHEDULE"}],
        )
        cc_base = compute_class("base")
        ctx = {
            "compute_classes": [cc_base],
            "node_pools": [base_pool, tpu_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}), node("node-tpu", labels={"cloud.google.com/gke-nodepool": "tpu-pool"})],
        }
        wl = collect.normalize_workloads({
            "items": [{
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "tpu-app", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "containers": [{
                                "name": "trainer",
                                "resources": {"limits": {"google.com/tpu": "4"}},
                            }],
                        }
                    }
                }
            }]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_gvisor_runtime_class_matches_sandbox_pool_preventing_false_clean(self):
        # Walk A: sandbox pool (CC-labelled, GVISOR sandboxConfig) and general pool (unlabelled, untainted).
        # Workload with runtimeClassName: gvisor receives injected selector sandbox.gke.io/runtime=gvisor,
        # preventing it from scheduling on general pool, correctly flagging for sandbox-cc.
        sandbox_pool = pool(
            "sandbox-pool",
            labels={"cloud.google.com/compute-class": "sandbox-cc"},
            taints=[{"key": "sandbox.gke.io/runtime", "value": "gvisor", "effect": "NO_SCHEDULE"}],
        )
        sandbox_pool["config"]["sandboxConfig"] = {"type": "GVISOR"}
        general_pool = pool("general-pool", labels={})
        cc_sandbox = compute_class("sandbox-cc")
        ctx = {
            "compute_classes": [cc_sandbox],
            "node_pools": [sandbox_pool, general_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-sb", labels={"cloud.google.com/gke-nodepool": "sandbox-pool"}), node("node-gen", labels={"cloud.google.com/gke-nodepool": "general-pool"})],
        }
        wl = collect.normalize_workloads({
            "items": [{
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "sandbox-app", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "runtimeClassName": "gvisor",
                            "containers": [{"name": "app", "resources": {}}],
                        }
                    }
                }
            }]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "sandbox-cc")

    def test_gvisor_runtime_class_both_pools_class_labeled_resolves_to_sandbox_class(self):
        # Walk B: both pools class-labelled (base and sandbox-cc).
        # Workload with runtimeClassName: gvisor matches sandbox-pool only, resolving to sandbox-cc without ambiguity.
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"})
        sandbox_pool = pool(
            "sandbox-pool",
            labels={"cloud.google.com/compute-class": "sandbox-cc"},
            taints=[{"key": "sandbox.gke.io/runtime", "value": "gvisor", "effect": "NO_SCHEDULE"}],
        )
        sandbox_pool["config"]["sandboxConfig"] = {"type": "GVISOR"}
        cc_base = compute_class("base")
        cc_sandbox = compute_class("sandbox-cc")
        ctx = {
            "compute_classes": [cc_base, cc_sandbox],
            "node_pools": [base_pool, sandbox_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}), node("node-sb", labels={"cloud.google.com/gke-nodepool": "sandbox-pool"})],
        }
        wl = collect.normalize_workloads({
            "items": [{
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "sandbox-app", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "runtimeClassName": "gvisor",
                            "containers": [{"name": "app", "resources": {}}],
                        }
                    }
                }
            }]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "sandbox-cc")

    def test_node_affinity_not_in_or_does_not_exist_on_unknown_key_routes_to_unknown(self):
        # Required nodeAffinity NotIn or DoesNotExist on a key the inventory cannot resolve routes to unknown (None),
        # clearing the workload per SOP §3.24 rather than treating as definite match across all pools.
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"})
        dedicated_pool = pool(
            "dedicated-pool",
            labels={"cloud.google.com/compute-class": "ded-cc"},
            taints=[{"key": "team", "value": "x", "effect": "NO_SCHEDULE"}],
        )
        cc_base = compute_class("base")
        cc_ded = compute_class("ded-cc")
        ctx = {
            "compute_classes": [cc_base, cc_ded],
            "node_pools": [base_pool, dedicated_pool],
            "namespaces": [self.ns],
            "nodes": [node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}), node("node-ded", labels={"cloud.google.com/gke-nodepool": "dedicated-pool"})],
        }
        affinity_not_in = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [{"key": "custom.io/unresolvable-key", "operator": "NotIn", "values": ["spot"]}]},
                    {"matchExpressions": [{"key": "cloud.google.com/gke-nodepool", "operator": "In", "values": ["base-pool"]}]},
                ]
            }
        }
        wl_not_in = collect.normalize_workloads({
            "items": [deployment("anti-spot-app", node_affinity=affinity_not_in, tolerations=[{"key": "team", "value": "x", "operator": "Equal"}])]
        })[0]
        hit_not_in = collect.check_untargeted_compute_class_workload(wl_not_in, ctx)
        self.assertIsNone(hit_not_in)

        affinity_dne = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [{"key": "custom.io/unresolvable-key", "operator": "DoesNotExist"}]},
                    {"matchExpressions": [{"key": "cloud.google.com/gke-nodepool", "operator": "In", "values": ["base-pool"]}]},
                ]
            }
        }
        wl_dne = collect.normalize_workloads({
            "items": [deployment("anti-spot-dne-app", node_affinity=affinity_dne, tolerations=[{"key": "team", "value": "x", "operator": "Equal"}])]
        })[0]
        hit_dne = collect.check_untargeted_compute_class_workload(wl_dne, ctx)
        self.assertIsNone(hit_dne)

    def test_workload_with_zero_gpu_or_tpu_limit_treated_as_cpu_workload(self):
        # A zero-quantity nvidia.com/gpu or google.com/tpu limit is not an accelerator request.
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"}, machine_type="e2-standard-4")
        gpu_pool = pool(
            "gpu-pool",
            labels={"cloud.google.com/compute-class": "gpu-cc"},
            machine_type="n1-standard-4",
            taints=[{"key": "nvidia.com/gpu", "value": "present", "effect": "NO_SCHEDULE"}],
        )
        gpu_pool["config"]["accelerators"] = [{"acceleratorType": "nvidia-tesla-t4", "acceleratorCount": 1}]
        cc_base = compute_class("base")
        cc_gpu = compute_class("gpu-cc", priorities=[{"dimension": ["nvidia.com/gpu"]}])
        ctx = {
            "compute_classes": [cc_base, cc_gpu],
            "node_pools": [base_pool, gpu_pool],
            "namespaces": [self.ns],
            "nodes": [
                node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}),
                node("node-gpu", labels={"cloud.google.com/gke-nodepool": "gpu-pool"}),
            ],
        }
        wl_gpu_zero = collect.normalize_workloads({
            "items": [{
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "zero-gpu-app", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "containers": [{
                                "name": "app",
                                "resources": {"limits": {"nvidia.com/gpu": 0}},
                            }],
                        }
                    }
                }
            }]
        })[0]
        hit_gpu_zero = collect.check_untargeted_compute_class_workload(wl_gpu_zero, ctx)
        self.assertIsNotNone(hit_gpu_zero)
        self.assertEqual(hit_gpu_zero["single_compute_class"], "base")

        wl_tpu_zero = collect.normalize_workloads({
            "items": [{
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "zero-tpu-app", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "containers": [{
                                "name": "app",
                                "resources": {"limits": {"google.com/tpu": "0"}},
                            }],
                        }
                    }
                }
            }]
        })[0]
        hit_tpu_zero = collect.check_untargeted_compute_class_workload(wl_tpu_zero, ctx)
        self.assertIsNotNone(hit_tpu_zero)
        self.assertEqual(hit_tpu_zero["single_compute_class"], "base")

    def test_mixed_architecture_cluster_resolves_class_conditionally(self):
        # On a mixed-architecture cluster with both x86 and Arm ComputeClasses,
        # workload constraints resolve single_compute_class rather than clearing to "".
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"}, machine_type="e2-standard-4")
        axion_pool = pool(
            "axion-pool",
            labels={"cloud.google.com/compute-class": "axion-cc"},
            machine_type="c4a-standard-4",
            taints=[{"key": "kubernetes.io/arch", "value": "arm64", "effect": "NO_SCHEDULE"}],
        )
        cc_base = compute_class("base")
        cc_axion = compute_class("axion-cc")
        ctx = {
            "compute_classes": [cc_base, cc_axion],
            "node_pools": [base_pool, axion_pool],
            "namespaces": [self.ns],
            "nodes": [
                node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}),
                node("node-axion", labels={"cloud.google.com/gke-nodepool": "axion-pool"}),
            ],
        }
        # Arm workload selects arm64 -> matches axion-pool only
        wl_arm = collect.normalize_workloads({
            "items": [{
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "arm-app", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "nodeSelector": {"kubernetes.io/arch": "arm64"},
                            "containers": [{"name": "app"}],
                        }
                    }
                }
            }]
        })[0]
        hit_arm = collect.check_untargeted_compute_class_workload(wl_arm, ctx)
        self.assertIsNotNone(hit_arm)
        self.assertEqual(hit_arm["single_compute_class"], "axion-cc")
        self.assertFalse(hit_arm["multiple_compute_classes"])

        # x86 workload has no selector -> matches base-pool only (fails axion-pool taint)
        wl_x86 = collect.normalize_workloads({
            "items": [deployment("x86-app")]
        })[0]
        hit_x86 = collect.check_untargeted_compute_class_workload(wl_x86, ctx)
        self.assertIsNotNone(hit_x86)
        self.assertEqual(hit_x86["single_compute_class"], "base")
        self.assertFalse(hit_x86["multiple_compute_classes"])

    def test_gke_provisioning_synthesis_and_anti_spot_idiom(self):
        # cloud.google.com/gke-provisioning is synthesized (spot/preemptible/standard) and resolves anti-spot affinity.
        base_pool = pool("base-pool", labels={"cloud.google.com/compute-class": "base"}, spot=False)
        spot_pool = pool("spot-pool", labels={"cloud.google.com/compute-class": "spot-cc"}, spot=True)
        cc_base = compute_class("base")
        cc_spot = compute_class("spot-cc")
        ctx = {
            "compute_classes": [cc_base, cc_spot],
            "node_pools": [base_pool, spot_pool],
            "namespaces": [self.ns],
            "nodes": [
                node("node-base", labels={"cloud.google.com/gke-nodepool": "base-pool"}),
                node("node-spot", labels={"cloud.google.com/gke-nodepool": "spot-pool"}),
            ],
        }
        affinity = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [{"key": "cloud.google.com/gke-provisioning", "operator": "NotIn", "values": ["spot"]}]}
                ]
            }
        }
        wl = collect.normalize_workloads({
            "items": [deployment("anti-spot-app", node_affinity=affinity)]
        })[0]
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "base")


if __name__ == "__main__":
    unittest.main()
