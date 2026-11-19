#!/usr/bin/env python3
"""Unit tests for machine_family_preflight.py."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import machine_family_preflight as preflight  # noqa: E402


class MachineFamilyPreflightTests(unittest.TestCase):
    def test_n1_to_n4_rejects_shielded_security_and_persistent_disk_and_local_ssd(self):
        """An N1 pool with disabled Shielded VM settings, pd-standard, and Local SSD is rejected on N4."""
        cluster = {
            "name": "legacy-cluster",
            "location": "us-central1",
            "currentMasterVersion": "1.35.3-gke.1290000",
            "shieldedNodes": {"enabled": False},
        }
        pool = {
            "name": "n1-legacy-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "n1-standard-8",
                "diskType": "pd-standard",
                "imageType": "COS_CONTAINERD",
                "localSsdCount": 2,
                "shieldedInstanceConfig": {
                    "enableSecureBoot": False,
                    "enableVtpm": False,
                    "enableIntegrityMonitoring": False,
                },
            },
        }

        result = preflight.evaluate_node_pool_transition(
            pool,
            "n4",
            cluster=cluster,
        )
        self.assertFalse(result.compatible)
        self.assertEqual(result.status, "incompatible")
        self.assertEqual(result.source_family, "n1")
        self.assertEqual(result.target_family, "n4")

        by_rule = {b.rule: b for b in result.blockers}
        self.assertIn(preflight.RULE_SHIELDED_NODES_DISABLED, by_rule)
        self.assertIn(preflight.RULE_SHIELDED_VTPM_DISABLED, by_rule)
        self.assertIn(preflight.RULE_SHIELDED_INTEGRITY_DISABLED, by_rule)
        self.assertIn(preflight.RULE_SHIELDED_SECURE_BOOT_DISABLED, by_rule)
        self.assertIn(preflight.RULE_DISK_PD_ON_HYPERDISK_ONLY, by_rule)
        self.assertIn(preflight.RULE_LSSD_UNSUPPORTED_ON_N4, by_rule)
        # Remediation for vTPM must not hallucinate an enableVtpm field in ComputeClass/NodePool YAML.
        self.assertNotIn(
            "enableVtpm: true",
            by_rule[preflight.RULE_SHIELDED_VTPM_DISABLED].remediation,
        )
        self.assertIn(
            "does not accept an enableVtpm field",
            by_rule[preflight.RULE_SHIELDED_VTPM_DISABLED].remediation,
        )

    def test_proto3_omitted_false_booleans_for_shielded_nodes_and_shielded_instance_config(self):
        """GKE Proto3 JSON omits false booleans in shieldedNodes and shieldedInstanceConfig."""
        # Proto3 cluster with disabled Shielded Nodes serializes `"shieldedNodes": {}`,
        # and a pool with Secure Boot off and Integrity Monitoring on serializes
        # `"shieldedInstanceConfig": {"enableIntegrityMonitoring": true}`.
        cluster_proto3 = {
            "name": "proto3-cluster",
            "currentMasterVersion": "1.35.3-gke.1290000",
            "shieldedNodes": {},
        }
        pool_no_secure_boot = {
            "name": "pool-a",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "n1-standard-4",
                "diskType": "hyperdisk-balanced",
                "shieldedInstanceConfig": {"enableIntegrityMonitoring": True},
            },
        }
        res_a = preflight.evaluate_node_pool_transition(
            pool_no_secure_boot,
            "n4",
            cluster=cluster_proto3,
        )
        self.assertFalse(res_a.compatible)
        rules_a = {b.rule for b in res_a.blockers}
        self.assertIn(preflight.RULE_SHIELDED_NODES_DISABLED, rules_a)
        self.assertIn(preflight.RULE_SHIELDED_SECURE_BOOT_DISABLED, rules_a)
        self.assertNotIn(preflight.RULE_SHIELDED_INTEGRITY_DISABLED, rules_a)

        # Empty `"shieldedInstanceConfig": {}` (both Secure Boot and Integrity Monitoring false in Proto3)
        # and Terraform `"enable_shielded_nodes": False`.
        cluster_tf = {"name": "tf-cluster", "enable_shielded_nodes": False}
        pool_both_off = {
            "name": "pool-b",
            "config": {
                "machineType": "n1-standard-4",
                "diskType": "hyperdisk-balanced",
                "shieldedInstanceConfig": {},
            },
        }
        res_b = preflight.evaluate_node_pool_transition(
            pool_both_off,
            "n4",
            cluster=cluster_tf,
        )
        self.assertFalse(res_b.compatible)
        rules_b = {b.rule for b in res_b.blockers}
        self.assertIn(preflight.RULE_SHIELDED_NODES_DISABLED, rules_b)
        self.assertIn(preflight.RULE_SHIELDED_SECURE_BOOT_DISABLED, rules_b)
        self.assertIn(preflight.RULE_SHIELDED_INTEGRITY_DISABLED, rules_b)

    def test_compatible_n2_to_n4_with_hyperdisk_and_shielded_defaults_passes(self):
        """A clean pool meeting version, disk, and security requirements passes on N4."""
        cluster = {
            "name": "modern-cluster",
            "currentMasterVersion": "1.35.3-gke.1290000",
            "shieldedNodes": {"enabled": True},
        }
        pool = {
            "name": "general-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "n2-standard-8",
                "diskType": "hyperdisk-balanced",
                "imageType": "COS_CONTAINERD",
                "shieldedInstanceConfig": {
                    "enableSecureBoot": True,
                    "enableVtpm": True,
                    "enableIntegrityMonitoring": True,
                },
            },
        }

        result = preflight.evaluate_node_pool_transition(
            pool,
            "n4",
            cluster=cluster,
        )
        self.assertTrue(result.compatible)
        self.assertEqual(result.status, "compatible")
        self.assertEqual(result.blockers, ())
        self.assertEqual(result.warnings, ())

    def test_confidential_vm_rejected_on_non_confidential_families_n2_n4_c4(self):
        """Confidential GKE Nodes are rejected on n2, n4, and c4, while pool enabled=false overrides cluster."""
        pool = {
            "name": "cvm-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "n2d-standard-8",
                "diskType": "hyperdisk-balanced",
                "confidentialNodes": {
                    "enabled": True,
                    "confidentialInstanceType": "SEV",
                },
            },
        }
        for target in ("n2", "n4", "c4"):
            with self.subTest(target=target):
                res = preflight.evaluate_node_pool_transition(pool, target)
                self.assertFalse(res.compatible)
                rules = {b.rule for b in res.blockers}
                self.assertIn(preflight.RULE_CVM_UNSUPPORTED_FAMILY, rules)

        # A pool explicitly setting confidentialNodes.enabled=false on a CVM cluster is not blocked on n4.
        cvm_cluster = {
            "name": "cvm-cluster",
            "confidentialNodes": {"enabled": True},
        }
        non_cvm_pool = {
            "name": "non-cvm-pool",
            "config": {
                "machineType": "n2d-standard-8",
                "diskType": "hyperdisk-balanced",
                "confidentialNodes": {"enabled": False},
            },
        }
        override_res = preflight.evaluate_node_pool_transition(
            non_cvm_pool,
            "n4",
            cluster=cvm_cluster,
        )
        self.assertTrue(override_res.compatible)

    def test_confidential_vm_sev_to_tdx_and_tdx_to_sev_mismatches_and_inferred_types(self):
        """AMD SEV/SEV_SNP on n2d versus Intel TDX on c3 transitions and shape constraints are enforced."""
        sev_pool = {
            "name": "amd-sev-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "n2d-standard-8",
                "diskType": "pd-balanced",
                "confidentialNodes": {
                    "enabled": True,
                    "confidentialInstanceType": "SEV_SNP",
                },
            },
        }
        # SEV_SNP -> c3 fails with technology mismatch.
        to_c3 = preflight.evaluate_node_pool_transition(sev_pool, "c3")
        self.assertFalse(to_c3.compatible)
        self.assertIn(
            preflight.RULE_CVM_TECHNOLOGY_MISMATCH,
            {b.rule for b in to_c3.blockers},
        )
        # SEV_SNP -> c3-highcpu-8 reports both technology mismatch and invalid C3 TDX shape.
        to_c3_highcpu = preflight.evaluate_node_pool_transition(
            sev_pool,
            "c3",
            target_machine_type="c3-highcpu-8",
        )
        self.assertEqual(
            [
                b.field
                for b in to_c3_highcpu.blockers
                if b.rule == preflight.RULE_CVM_TECHNOLOGY_MISMATCH
            ],
            ["config.confidentialNodes.confidentialInstanceType", "machineType"],
        )
        # SEV_SNP -> n2d passes.
        to_n2d = preflight.evaluate_node_pool_transition(sev_pool, "n2d")
        self.assertTrue(to_n2d.compatible)

        # A c3 pool with confidentialNodes: {"enabled": true} and omitted confidentialInstanceType
        # infers TDX: compatible with c3, incompatible with n2d.
        inferred_tdx_pool = {
            "name": "c3-cvm-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "c3-standard-8",
                "diskType": "pd-balanced",
                "confidentialNodes": {"enabled": True},
            },
        }
        self.assertTrue(
            preflight.evaluate_node_pool_transition(inferred_tdx_pool, "c3").compatible
        )
        inferred_tdx_to_n2d = preflight.evaluate_node_pool_transition(
            inferred_tdx_pool,
            "n2d",
        )
        self.assertFalse(inferred_tdx_to_n2d.compatible)
        self.assertIn(
            preflight.RULE_CVM_TECHNOLOGY_MISMATCH,
            {b.rule for b in inferred_tdx_to_n2d.blockers},
        )

    def test_disk_generation_constraints_across_families(self):
        """pd-* fails on n4/c4, pd-standard fails on c3, and hyperdisk-* fails on n2/n2d."""
        for pd_type in ("pd-standard", "pd-balanced", "pd-ssd"):
            pool = {
                "name": "pd-pool",
                "config": {"machineType": "n1-standard-4", "diskType": pd_type},
            }
            for hyperdisk_only in ("n4", "c4"):
                res = preflight.evaluate_node_pool_transition(pool, hyperdisk_only)
                self.assertFalse(res.compatible)
                self.assertIn(
                    preflight.RULE_DISK_PD_ON_HYPERDISK_ONLY,
                    {b.rule for b in res.blockers},
                )

        # pd-standard on c3 is rejected, while pd-balanced, pd-ssd, and hyperdisk-balanced pass.
        c3_std = preflight.evaluate_node_pool_transition(
            {"name": "p", "config": {"machineType": "n1-standard-4", "diskType": "pd-standard"}},
            "c3",
        )
        self.assertFalse(c3_std.compatible)
        self.assertIn(
            preflight.RULE_DISK_PD_STANDARD_ON_C3,
            {b.rule for b in c3_std.blockers},
        )
        for allowed_c3_disk in ("pd-balanced", "pd-ssd", "hyperdisk-balanced"):
            c3_ok = preflight.evaluate_node_pool_transition(
                {"name": "p", "config": {"machineType": "n1-standard-4", "diskType": allowed_c3_disk}},
                "c3",
            )
            self.assertTrue(c3_ok.compatible, allowed_c3_disk)

        # hyperdisk-balanced on n2 and n2d is rejected.
        for gen2_family in ("n2", "n2d"):
            hd_on_gen2 = preflight.evaluate_node_pool_transition(
                {
                    "name": "p",
                    "config": {
                        "machineType": "n4-standard-4",
                        "diskType": "hyperdisk-balanced",
                    },
                },
                gen2_family,
            )
            self.assertFalse(hd_on_gen2.compatible)
            self.assertIn(
                preflight.RULE_DISK_HYPERDISK_ON_GEN2,
                {b.rule for b in hd_on_gen2.blockers},
            )

    def test_local_ssd_rejected_on_n4_and_non_lssd_c3_c4_and_zero_counts_allowed(self):
        """Active Local SSD configs fail on n4 and require -lssd on c3/c4; explicit zero counts pass."""
        for lssd_config in (
            {"localSsdCount": 2},
            {"ephemeralStorageLocalSsdConfig": {"localSsdCount": 2}},
            {"localNvmeSsdBlockConfig": {"localSsdCount": 2}},
            {"ephemeralStorageConfig": {"localSsdCount": 2}},
        ):
            pool = {
                "name": "scratch-pool",
                "config": {
                    "machineType": "n1-standard-8",
                    "diskType": "hyperdisk-balanced",
                    **lssd_config,
                },
            }
            on_n4 = preflight.evaluate_node_pool_transition(pool, "n4")
            self.assertFalse(on_n4.compatible)
            self.assertIn(
                preflight.RULE_LSSD_UNSUPPORTED_ON_N4,
                {b.rule for b in on_n4.blockers},
            )

            for target in ("c3", "c4"):
                without_lssd_shape = preflight.evaluate_node_pool_transition(pool, target)
                self.assertFalse(without_lssd_shape.compatible)
                target_rules = {b.rule for b in without_lssd_shape.blockers}
                self.assertIn(preflight.RULE_LSSD_REQUIRES_LSSD_SHAPE, target_rules)
                if "localSsdCount" in lssd_config:
                    self.assertIn(
                        preflight.RULE_LSSD_LEGACY_COUNT_ON_GEN3_GEN4,
                        target_rules,
                    )

        # Explicit zero counts in ephemeralStorageLocalSsdConfig / localNvmeSsdBlockConfig are not active Local SSDs.
        zero_lssd_pool = {
            "name": "zero-lssd",
            "config": {
                "machineType": "n2-standard-8",
                "diskType": "hyperdisk-balanced",
                "ephemeralStorageLocalSsdConfig": {"localSsdCount": 0, "dataCacheCount": 0},
                "localNvmeSsdBlockConfig": {"localSsdCount": 0},
            },
        }
        self.assertTrue(
            preflight.evaluate_node_pool_transition(zero_lssd_pool, "n4").compatible
        )

        # On c3/c4 with an explicit -lssd target shape and ephemeralStorageLocalSsdConfig, it passes.
        nvme_pool = {
            "name": "nvme-pool",
            "config": {
                "machineType": "n2-standard-8",
                "diskType": "hyperdisk-balanced",
                "ephemeralStorageLocalSsdConfig": {"localSsdCount": 2},
            },
        }
        c4_lssd_ok = preflight.evaluate_node_pool_transition(
            nvme_pool,
            "c4-standard-8-lssd",
        )
        self.assertTrue(c4_lssd_ok.compatible)

        # Legacy localSsdCount is still rejected even when targeting c4-standard-8-lssd.
        legacy_count_pool = {
            "name": "legacy-lssd-pool",
            "config": {
                "machineType": "n1-standard-8",
                "diskType": "hyperdisk-balanced",
                "localSsdCount": 2,
            },
        }
        c4_legacy = preflight.evaluate_node_pool_transition(
            legacy_count_pool,
            "c4-standard-8-lssd",
        )
        self.assertFalse(c4_legacy.compatible)
        self.assertIn(
            preflight.RULE_LSSD_LEGACY_COUNT_ON_GEN3_GEN4,
            {b.rule for b in c4_legacy.blockers},
        )

    def test_gpu_accelerators_rejected_across_gke_kcc_and_terraform_shapes(self):
        """Attached GPUs under accelerators, guestAccelerator, guestAccelerators, or guest_accelerator are rejected."""
        for accel_field in (
            {"accelerators": [{"acceleratorCount": "1", "acceleratorType": "nvidia-tesla-t4"}]},
            {"guestAccelerator": [{"count": 1, "type": "nvidia-tesla-t4"}]},
            {"guestAccelerators": [{"acceleratorCount": 2, "acceleratorType": "nvidia-tesla-v100"}]},
            {"guest_accelerator": [{"count": "1", "type": "nvidia-tesla-t4"}]},
        ):
            pool = {
                "name": "gpu-pool",
                "config": {
                    "machineType": "n1-standard-8",
                    "diskType": "pd-balanced",
                    **accel_field,
                },
            }
            res = preflight.evaluate_node_pool_transition(pool, "n2")
            self.assertFalse(res.compatible, accel_field)
            self.assertIn(
                preflight.RULE_GPU_UNSUPPORTED_ON_TARGET,
                {b.rule for b in res.blockers},
            )

        # Non-numeric or zero accelerator counts do not crash or trigger a false blocker.
        zero_gpu_pool = {
            "name": "no-gpu",
            "config": {
                "machineType": "n1-standard-8",
                "diskType": "pd-balanced",
                "accelerators": [{"acceleratorCount": "0"}, {"acceleratorCount": "invalid"}],
            },
        }
        self.assertTrue(
            preflight.evaluate_node_pool_transition(zero_gpu_pool, "n2").compatible
        )

    def test_kcc_container_node_pool_manifest_extracts_name_and_spec_version(self):
        """A Config Connector ContainerNodePool object reads metadata.name/spec.resourceID and spec.version."""
        kcc_pool = {
            "apiVersion": "container.cnrm.cloud.google.com/v1beta1",
            "kind": "ContainerNodePool",
            "metadata": {"name": "kcc-legacy-pool"},
            "spec": {
                "resourceID": "gke-legacy-pool",
                "version": "1.28.5-gke.1000000",
                "nodeConfig": {
                    "machineType": "n1-standard-4",
                    "diskType": "hyperdisk-balanced",
                },
            },
        }
        code, report = preflight.run_preflight(
            node_pool_json_sources=[json.dumps(kcc_pool)],
            node_pool_names=["gke-legacy-pool"],
            target_specs=["n4"],
        )
        self.assertEqual(code, preflight.EXIT_INCOMPATIBLE)
        self.assertEqual(len(report["results"]), 1)
        row = report["results"][0]
        self.assertEqual(row["node_pool"], "gke-legacy-pool")
        self.assertIn(
            preflight.RULE_GKE_NODE_VERSION_FLOOR,
            {b["rule"] for b in row["blockers"]},
        )

    def test_version_floors_and_dynamic_rwo_and_os_images(self):
        """Minimum GKE versions for target families, TDX, dynamic-rwo, and OS images are enforced."""
        old_cluster = {"name": "old", "currentMasterVersion": "1.28.5-gke.1000000"}
        old_pool = {
            "name": "p",
            "version": "1.28.5-gke.1000000",
            "config": {
                "machineType": "n1-standard-4",
                "diskType": "hyperdisk-balanced",
                "imageType": "COS",
            },
        }
        res_n4 = preflight.evaluate_node_pool_transition(
            old_pool,
            "n4",
            cluster=old_cluster,
            require_dynamic_rwo=True,
        )
        self.assertFalse(res_n4.compatible)
        rules = {b.rule for b in res_n4.blockers}
        self.assertIn(preflight.RULE_GKE_MASTER_VERSION_FLOOR, rules)
        self.assertIn(preflight.RULE_GKE_NODE_VERSION_FLOOR, rules)
        self.assertIn(preflight.RULE_DYNAMIC_RWO_VERSION_FLOOR, rules)
        self.assertIn(preflight.RULE_OS_IMAGE_LEGACY_DOCKER, rules)

        # Cross-generation migration below 1.35.3-gke.1290000 emits dynamic-rwo warning by default.
        mid_cluster = {"name": "mid", "currentMasterVersion": "1.34.2-gke.1000000"}
        mid_pool = {
            "name": "p",
            "version": "1.34.2-gke.1000000",
            "config": {
                "machineType": "n1-standard-4",
                "diskType": "hyperdisk-balanced",
                "imageType": "COS_CONTAINERD",
            },
        }
        res_mid = preflight.evaluate_node_pool_transition(
            mid_pool,
            "n4",
            cluster=mid_cluster,
        )
        self.assertTrue(res_mid.compatible)
        self.assertIn(
            preflight.RULE_DYNAMIC_RWO_VERSION_FLOOR,
            {w.rule for w in res_mid.warnings},
        )

        # Windows image on c4 or n4 is rejected.
        win_pool = {
            "name": "win-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "n2-standard-4",
                "diskType": "hyperdisk-balanced",
                "imageType": "WINDOWS_LTSC_CONTAINERD",
            },
        }
        res_win_c4 = preflight.evaluate_node_pool_transition(win_pool, "c4")
        self.assertFalse(res_win_c4.compatible)
        self.assertIn(
            preflight.RULE_OS_IMAGE_WINDOWS_UNSUPPORTED,
            {b.rule for b in res_win_c4.blockers},
        )

        # Confidential TDX on c3 below 1.28.6-gke.1317000 is rejected.
        old_tdx_pool = {
            "name": "tdx-pool",
            "version": "1.27.8-gke.1000000",
            "config": {
                "machineType": "c3-standard-8",
                "diskType": "pd-balanced",
                "confidentialNodes": {
                    "enabled": True,
                    "confidentialInstanceType": "TDX",
                },
            },
        }
        res_tdx_floor = preflight.evaluate_node_pool_transition(old_tdx_pool, "c3")
        self.assertFalse(res_tdx_floor.compatible)
        self.assertIn(
            preflight.RULE_GKE_CVM_TDX_VERSION_FLOOR,
            {b.rule for b in res_tdx_floor.blockers},
        )

    def test_cli_offline_json_and_output_file(self):
        """CLI writes atomic JSON to --output and prints Markdown or JSON report."""
        cluster_data = {
            "name": "prod-cluster",
            "location": "us-central1",
            "currentMasterVersion": "1.35.3-gke.1290000",
            "nodePools": [
                {
                    "name": "legacy-n1",
                    "version": "1.35.3-gke.1290000",
                    "config": {
                        "machineType": "n1-standard-4",
                        "diskType": "pd-standard",
                        "shieldedInstanceConfig": {
                            "enableSecureBoot": False,
                            "enableVtpm": False,
                        },
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cluster_file = Path(tmpdir) / "cluster.json"
            output_file = Path(tmpdir) / "report.json"
            cluster_file.write_text(json.dumps(cluster_data), encoding="utf-8")

            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                rc = preflight.main(
                    [
                        "--cluster-json",
                        str(cluster_file),
                        "--target-family",
                        "n4",
                        "--target-family",
                        "c3",
                        "--output",
                        str(output_file),
                    ]
                )
            self.assertEqual(rc, preflight.EXIT_INCOMPATIBLE)
            rendered = stdout_buf.getvalue()
            self.assertIn("VERDICT: INCOMPATIBLE", rendered)
            self.assertIn("shielded-vtpm-disabled", rendered)
            self.assertIn("disk-type-pd-on-hyperdisk-only", rendered)
            self.assertIn("disk-type-pd-standard-unsupported", rendered)
            self.assertIn(preflight.PREFLIGHT_STOP_LINE, rendered)

            saved = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertFalse(saved["compatible"])
            self.assertEqual(saved["status"], "incompatible")
            self.assertEqual(len(saved["results"]), 2)

    def test_default_runner_sets_shim_no_retry_deadline(self):
        """_default_runner sets CREDENTIAL_PROXY_RETRY_DEADLINE_SECONDS=0 in child env."""
        completed = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch("subprocess.run", return_value=completed) as run_mock:
            runner = preflight._default_runner(timeout=30)
            rc, out, err = runner(["gcloud", "version"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "{}")
        self.assertEqual(err, "")
        passed_env = run_mock.call_args.kwargs["env"]
        self.assertEqual(
            passed_env.get(preflight.SHIM_RETRY_DEADLINE_ENV),
            preflight.SHIM_NO_RETRY_DEADLINE,
        )

    def test_fail_closed_when_gcloud_fails_or_times_out_or_returns_no_pools(self):
        """Failed or timed-out gcloud calls exit non-zero and never declare compatible."""
        def failing_runner(argv: list[str]) -> tuple[int, str, str]:
            return 1, "", "ERROR: (gcloud.container.clusters.describe) NOT_FOUND"

        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            rc = preflight.main(
                [
                    "--project",
                    "demo-proj",
                    "--location",
                    "us-central1",
                    "--cluster",
                    "missing-cluster",
                    "--target-family",
                    "n4",
                ],
                run=failing_runner,
            )
        self.assertEqual(rc, preflight.EXIT_READ_ERROR)
        self.assertIn("VERDICT: ERROR", stdout_buf.getvalue())
        self.assertIn("NOT_FOUND", stdout_buf.getvalue())

        # Empty nodePools list also fails closed.
        def empty_pools_runner(argv: list[str]) -> tuple[int, str, str]:
            return 0, json.dumps({"name": "empty-cluster", "nodePools": []}), ""

        stdout_buf2 = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf2):
            rc2 = preflight.main(
                [
                    "--project",
                    "demo-proj",
                    "--location",
                    "us-central1",
                    "--cluster",
                    "empty-cluster",
                    "--target-family",
                    "n4",
                ],
                run=empty_pools_runner,
            )
        self.assertEqual(rc2, preflight.EXIT_READ_ERROR)
        self.assertIn("VERDICT: ERROR", stdout_buf2.getvalue())


if __name__ == "__main__":
    unittest.main()
