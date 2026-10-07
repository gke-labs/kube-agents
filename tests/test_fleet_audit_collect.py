"""Unit tests for the untargeted-compute-class-workload fleet-audit check."""

import unittest
from pathlib import Path
import sys

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
        self.base_node = node(
            "node-1",
            labels={"cloud.google.com/compute-class": "standard-cc"},
            taints=[],
        )
        self.ns = namespace("default")

    def test_positive_untainted_cc_base_nodes_without_selector_emits_finding(self):
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "nodes": [self.base_node],
            "namespaces": [self.ns],
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
            "nodes": [self.base_node],
            "namespaces": [self.ns],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_namespace_with_default_compute_class_label(self):
        labeled_ns = namespace("default", labels={"cloud.google.com/default-compute-class": "standard-cc"})
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "nodes": [self.base_node],
            "namespaces": [labeled_ns],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_namespace_with_default_compute_class_non_daemonset_label(self):
        labeled_ns = namespace("default", labels={"cloud.google.com/default-compute-class-non-daemonset": "standard-cc"})
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "nodes": [self.base_node],
            "namespaces": [labeled_ns],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_workload_with_explicit_node_selector(self):
        wl = collect.normalize_workloads({
            "items": [deployment("api", node_selector={"cloud.google.com/compute-class": "standard-cc"})]
        })[0]
        ctx = {
            "compute_classes": [self.cc],
            "nodes": [self.base_node],
            "namespaces": [self.ns],
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
            "nodes": [self.base_node],
            "namespaces": [self.ns],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_workload_tolerating_dedicated_tainted_pool(self):
        gpu_node = node(
            "gpu-node",
            labels={"accelerator": "nvidia-t4"},
            taints=[{"key": "nvidia.com/gpu", "value": "present", "effect": "NoSchedule"}],
        )
        wl = collect.normalize_workloads({
            "items": [deployment("gpu-worker", tolerations=[{"key": "nvidia.com/gpu", "operator": "Exists"}])]
        })[0]
        ctx = {
            "compute_classes": [self.cc],
            "nodes": [self.base_node, gpu_node],
            "namespaces": [self.ns],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_cluster_with_untainted_nodes_lacking_compute_class(self):
        general_node = node("standard-node", labels={"node-role": "worker"}, taints=[])
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "nodes": [self.base_node, general_node],
            "namespaces": [self.ns],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_cluster_without_compute_classes(self):
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [],
            "nodes": [self.base_node],
            "namespaces": [self.ns],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_single_compute_class_emitted_on_candidate_when_matching(self):
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "nodes": [self.base_node],
            "namespaces": [self.ns],
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
            "nodes": [self.base_node],  # labeled standard-cc
            "namespaces": [self.ns],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "")
        self.assertTrue(hit["multiple_compute_classes"])

    def test_controller_cordon_taint_on_non_cc_node_does_not_cause_false_major(self):
        cordoned_non_cc_node = node(
            "non-cc-1",
            labels={"node-role": "worker"},
            taints=[{"key": "node.kubernetes.io/unschedulable", "effect": "NoSchedule"}],
        )
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "nodes": [self.base_node, cordoned_non_cc_node],
            "namespaces": [self.ns],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_accelerator_compute_class_with_gpu_or_tpu_excluded(self):
        gpu_cc = compute_class("gpu-l4", priorities=[{"gpu": {"count": 1, "type": "nvidia-l4"}}])
        tpu_cc = compute_class("tpu-v5e", priorities=[{"tpu": {"count": 1, "type": "v5e"}}])
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc, gpu_cc, tpu_cc],
            "nodes": [self.base_node],
            "namespaces": [self.ns],
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
            "nodes": [self.base_node],
            "namespaces": [self.ns],
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
                                "values": ["standard-cc"],
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
            "nodes": [self.base_node],
            "namespaces": [self.ns],
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
        import json
        from unittest.mock import patch, MagicMock

        with patch.object(collect, "dump_state") as mock_dump:
            tmp_dump = Path("/tmp/test_dump.json")
            tmp_dump.write_text(json.dumps(fake_dump), encoding="utf-8")
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
            tmp_dump.unlink(missing_ok=True)

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
        import json
        from unittest.mock import patch, MagicMock

        with patch.object(collect, "dump_state") as mock_dump:
            tmp_dump = Path("/tmp/test_dump.json")
            tmp_dump.write_text(json.dumps(fake_dump), encoding="utf-8")
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
            tmp_dump.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
