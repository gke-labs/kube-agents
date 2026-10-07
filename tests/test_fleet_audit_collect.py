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


def pool(name, labels=None, taints=None, autoscaling=None):
    config = {}
    if labels is not None:
        config["labels"] = labels
    if taints is not None:
        config["taints"] = taints
    res = {"name": name, "config": config}
    if autoscaling is not None:
        res["autoscaling"] = autoscaling
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
        self.ns = namespace("default")

    def test_positive_untainted_cc_base_nodes_without_selector_emits_finding(self):
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool],
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
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
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
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_negative_cluster_without_compute_classes(self):
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [],
            "node_pools": [self.base_pool],
            "namespaces": [self.ns],
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNone(hit)

    def test_single_compute_class_emitted_on_candidate_when_matching(self):
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [self.base_pool],
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
            "node_pools": [self.base_pool],  # labeled standard-cc
            "namespaces": [self.ns],
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
            "node_pools": [self.base_pool],
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

        dne_affinity = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "cloud.google.com/compute-class",
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

    def test_the_dump_asks_for_namespaces_and_not_nodes(self):
        self.assertNotIn("nodes", collect.DUMP_COMMAND_KINDS)
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
        )
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc],
            "node_pools": [cc_pool, zero_pool],
            "namespaces": [self.ns],
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
        }
        hit = collect.check_untargeted_compute_class_workload(wl, ctx)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["single_compute_class"], "standard-cc")
        self.assertFalse(hit["multiple_compute_classes"])

    def test_compute_class_referencing_tainted_manual_nodepool_is_excluded(self):
        manual_pool_cc = compute_class("manual-pool-cc", priorities=[{"nodepools": ["pool-a"]}])
        pool_a = pool(
            "pool-a",
            labels={"cloud.google.com/gke-nodepool": "pool-a", "cloud.google.com/compute-class": "standard-cc"},
            taints=[{"key": "workload-specific", "value": "true", "effect": "NO_SCHEDULE"}],
        )
        wl = collect.normalize_workloads({"items": [deployment("api")]})[0]
        ctx = {
            "compute_classes": [self.cc, manual_pool_cc],
            "node_pools": [self.base_pool, pool_a],
            "namespaces": [self.ns],
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
        }
        hit = collect.check_untargeted_compute_class_workload(wl_noexec, ctx)
        self.assertIsNotNone(hit)

        # Pod tolerates Exists with effect NoSchedule -> DOES tolerate NO_SCHEDULE -> does not flag
        wl_nosched = collect.normalize_workloads({
            "items": [deployment("api-nosched", tolerations=[{"operator": "Exists", "effect": "NoSchedule"}])]
        })[0]
        hit_nosched = collect.check_untargeted_compute_class_workload(wl_nosched, ctx)
        self.assertIsNone(hit_nosched)

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
            "items": [deployment("api"), self.ns]
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
            "items": [deployment("api"), self.ns]
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
            "items": [deployment("api"), self.ns]
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
                self.assertEqual([self.ns], cc_context.context.get("namespaces"))
                self.assertEqual([self.cc], cc_context.context.get("compute_classes"))
                self.assertEqual([test_pool], cc_context.context.get("node_pools"))
                cmd_rec = cc_context.commands.get("untargeted-compute-class-workload")
                self.assertIsNotNone(cmd_rec)
                self.assertIn("kubectl get computeclasses -A -o json", cmd_rec["command"])
                self.assertEqual(0, cmd_rec["rc"])
                self.assertEqual(0.05, cmd_rec["duration_s"])
                self.assertEqual(cmd_rec["output_sha256"], collect.output_digest(cc_stdout))

    def test_collect_obtainability_autopilot_skips_node_pools_list(self):
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
                mock_run_and_gate.return_value = (
                    {"items": [self.cc]},
                    MagicMock(rc=0, duration_s=0.05, stdout=cc_stdout),
                )
                cc_context = collect._collect_obtainability(
                    {"name": "c1", "project": "p1", "location": "l1", "autopilot": True},
                    Path("/fake/kubeconfig"),
                    (spec,),
                    run=MagicMock(),
                )
                # On Autopilot, node-pools list is skipped because it returns 400; check is marked not_applicable
                self.assertEqual(1, mock_run_and_gate.call_count)
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


if __name__ == "__main__":
    unittest.main()
