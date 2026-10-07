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

        # Workload with scheduling constraints routes to manual remediation:
        wl_constrained = collect.normalize_workloads({
            "items": [deployment(
                "batch-worker",
                node_selector={"cloud.google.com/gke-nodepool": "batch-pool"},
                tolerations=[{"key": collect.COMPUTE_CLASS_LABEL, "value": "batch", "operator": "Equal", "effect": "NoSchedule"}],
            )]
        })[0]
        hit_constrained = collect.check_untargeted_compute_class_workload(wl_constrained, ctx)
        self.assertIsNotNone(hit_constrained)
        self.assertEqual(hit_constrained["single_compute_class"], "")
        self.assertTrue(hit_constrained["multiple_compute_classes"])

        # Unconstrained workload (no selector, no tolerations) resolves to single class:
        wl_unpinned = collect.normalize_workloads({
            "items": [deployment("generic-worker")]
        })[0]
        hit_unpinned = collect.check_untargeted_compute_class_workload(wl_unpinned, ctx)
        self.assertIsNotNone(hit_unpinned)
        self.assertEqual(hit_unpinned["single_compute_class"], "base")
        self.assertFalse(hit_unpinned["multiple_compute_classes"])

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

    def test_static_non_cc_pool_resized_to_zero_live_nodes_flags_workload(self):
        # Legacy pool was created with initialNodeCount: 3, but live nodes count is 0.
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
        # Legacy pool was created with initialNodeCount: 0, but grown to 6 live nodes.
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

    def test_workload_with_scheduling_constraints_routes_to_manual_remediation(self):
        # Constrained workloads (nodeSelector, affinity, tolerations, runtimeClassName, accelerators)
        # route to manual remediation (single_compute_class: "") rather than running a placement simulator.
        cc_base = compute_class("base")
        ctx = {
            "compute_classes": [cc_base],
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
            "nodes": [self.base_node],
        }

        # 1. nodeSelector constraint
        wl_sel = collect.normalize_workloads({
            "items": [deployment("api-sel", node_selector={"tier": "frontend"})]
        })[0]
        hit_sel = collect.check_untargeted_compute_class_workload(wl_sel, ctx)
        self.assertIsNotNone(hit_sel)
        self.assertEqual(hit_sel["single_compute_class"], "")
        self.assertTrue(hit_sel["multiple_compute_classes"])

        # 2. tolerations constraint
        wl_tol = collect.normalize_workloads({
            "items": [deployment("api-tol", tolerations=[{"key": "custom", "operator": "Exists"}])]
        })[0]
        hit_tol = collect.check_untargeted_compute_class_workload(wl_tol, ctx)
        self.assertIsNotNone(hit_tol)
        self.assertEqual(hit_tol["single_compute_class"], "")
        self.assertTrue(hit_tol["multiple_compute_classes"])

        # 3. runtimeClassName constraint
        wl_rc = collect.normalize_workloads({
            "items": [{"kind": "Deployment", "metadata": {"name": "api-rc", "namespace": "default"}, "spec": {"template": {"spec": {"runtimeClassName": "gvisor"}}}}]
        })[0]
        hit_rc = collect.check_untargeted_compute_class_workload(wl_rc, ctx)
        self.assertIsNotNone(hit_rc)
        self.assertEqual(hit_rc["single_compute_class"], "")
        self.assertTrue(hit_rc["multiple_compute_classes"])
