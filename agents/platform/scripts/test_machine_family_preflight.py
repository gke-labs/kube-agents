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
        self.assertIn(preflight.RULE_DISK_PD_ON_HYPERDISK_ONLY, by_rule)
        self.assertIn(preflight.RULE_LSSD_UNSUPPORTED_ON_N4, by_rule)

        warnings_by_rule = {w.rule: w for w in result.warnings}
        self.assertIn(preflight.RULE_SHIELDED_NODES_DISABLED, warnings_by_rule)
        self.assertIn(preflight.RULE_SHIELDED_VTPM_DISABLED, warnings_by_rule)
        self.assertIn(preflight.RULE_SHIELDED_INTEGRITY_DISABLED, warnings_by_rule)
        self.assertIn(preflight.RULE_SHIELDED_SECURE_BOOT_DISABLED, warnings_by_rule)
        # Remediation for vTPM must not hallucinate an enableVtpm field in ComputeClass/NodePool YAML.
        self.assertNotIn(
            "enableVtpm: true",
            warnings_by_rule[preflight.RULE_SHIELDED_VTPM_DISABLED].remediation,
        )
        self.assertIn(
            "does not accept an enableVtpm field",
            warnings_by_rule[preflight.RULE_SHIELDED_VTPM_DISABLED].remediation,
        )
        self.assertNotIn(
            "requires Shielded GKE Nodes",
            warnings_by_rule[preflight.RULE_SHIELDED_NODES_DISABLED].message,
        )
        self.assertNotIn(
            "requires Shielded VM vTPM",
            warnings_by_rule[preflight.RULE_SHIELDED_VTPM_DISABLED].message,
        )

    def test_proto3_omitted_false_booleans_for_shielded_nodes_and_shielded_instance_config(self):
        """GKE Proto3 JSON omits false booleans in shieldedNodes and shieldedInstanceConfig and reports them as warnings."""
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
        self.assertTrue(res_a.compatible)
        self.assertEqual(res_a.blockers, ())
        rules_a = {w.rule for w in res_a.warnings}
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
        self.assertTrue(res_b.compatible)
        self.assertEqual(res_b.blockers, ())
        rules_b = {w.rule for w in res_b.warnings}
        self.assertIn(preflight.RULE_SHIELDED_NODES_DISABLED, rules_b)
        self.assertIn(preflight.RULE_SHIELDED_SECURE_BOOT_DISABLED, rules_b)
        self.assertIn(preflight.RULE_SHIELDED_INTEGRITY_DISABLED, rules_b)

    def test_default_gke_pool_shape_to_n2_is_compatible(self):
        """A default GKE e2-standard-4 pool (Secure Boot disabled by default) migrating to n2 is COMPATIBLE."""
        default_pool = {
            "name": "default-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "e2-standard-4",
                "diskType": "pd-balanced",
                "imageType": "COS_CONTAINERD",
                "shieldedInstanceConfig": {
                    "enableIntegrityMonitoring": True,
                },
            },
        }
        res = preflight.evaluate_node_pool_transition(default_pool, "n2")
        self.assertTrue(res.compatible)
        self.assertEqual(res.status, "compatible")
        self.assertEqual(res.blockers, ())
        self.assertEqual(
            {w.rule for w in res.warnings},
            {preflight.RULE_SHIELDED_SECURE_BOOT_DISABLED},
        )

        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            rc = preflight.main(
                [
                    "--node-pool-json",
                    json.dumps(default_pool),
                    "--target-family",
                    "n2",
                ]
            )
        self.assertEqual(rc, preflight.EXIT_OK)
        rendered = stdout_buf.getvalue()
        self.assertIn(preflight.VERDICT_COMPATIBLE, rendered)
        self.assertNotIn(preflight.PREFLIGHT_STOP_LINE, rendered)

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
        """Confidential GKE Nodes are rejected on n2 and n4; c4 requires Intel TDX on c4-standard-* shapes; pool enabled=false overrides cluster."""
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
        for target in ("n2", "n4"):
            with self.subTest(target=target):
                res = preflight.evaluate_node_pool_transition(pool, target)
                self.assertFalse(res.compatible)
                rules = {b.rule for b in res.blockers}
                self.assertIn(preflight.RULE_CVM_UNSUPPORTED_FAMILY, rules)

        # AMD SEV targeting c4 fails with technology mismatch (c4 supports Intel TDX).
        sev_on_c4 = preflight.evaluate_node_pool_transition(pool, "c4")
        self.assertFalse(sev_on_c4.compatible)
        self.assertIn(
            preflight.RULE_CVM_TECHNOLOGY_MISMATCH,
            {b.rule for b in sev_on_c4.blockers},
        )

        # Intel TDX on c3-standard-8 targeting c4 or c4-standard-8 is compatible, while non-c4-standard-* or -lssd shapes are rejected.
        tdx_pool = {
            "name": "tdx-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "c3-standard-8",
                "diskType": "hyperdisk-balanced",
                "confidentialNodes": {"enabled": True},
            },
        }
        self.assertTrue(
            preflight.evaluate_node_pool_transition(tdx_pool, "c4-standard-8").compatible
        )
        for bad_c4_shape in ("c4-highcpu-8", "c4-standard-8-lssd"):
            with self.subTest(bad_c4_shape=bad_c4_shape):
                bad_c4_res = preflight.evaluate_node_pool_transition(
                    tdx_pool,
                    bad_c4_shape,
                )
                self.assertFalse(bad_c4_res.compatible)
                self.assertIn(
                    preflight.RULE_CVM_TECHNOLOGY_MISMATCH,
                    {b.rule for b in bad_c4_res.blockers},
                )

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

    def test_node_pool_json_cluster_wrapper_empty_object_and_terraform_list_node_config(self):
        """--node-pool-json fails closed on {}, unwraps cluster nodePools, and unwraps 1-element list node_config."""
        # 1. Empty object or missing machineType fails closed (EXIT_READ_ERROR = 2) instead of defaulting to e2-medium.
        code_empty, report_empty = preflight.run_preflight(
            node_pool_json_sources=["{}"],
            target_specs=["n4"],
        )
        self.assertEqual(code_empty, preflight.EXIT_READ_ERROR)
        self.assertFalse(report_empty["compatible"])
        self.assertEqual(report_empty["status"], "error")
        self.assertTrue(report_empty["errors"])

        # 2. A cluster describe object passed to --node-pool-json routes through cluster_doc and nodePools.
        cluster_wrapper = {
            "name": "wrapped-cluster",
            "currentMasterVersion": "1.35.3-gke.1290000",
            "shieldedNodes": {"enabled": False},
            "nodePools": [
                {
                    "name": "wrapped-pool",
                    "version": "1.35.3-gke.1290000",
                    "config": {
                        "machineType": "n1-standard-4",
                        "diskType": "pd-standard",
                    },
                }
            ],
        }
        code_wrap, report_wrap = preflight.run_preflight(
            node_pool_json_sources=[json.dumps(cluster_wrapper)],
            target_specs=["n4"],
        )
        self.assertEqual(code_wrap, preflight.EXIT_INCOMPATIBLE)
        self.assertEqual(report_wrap["cluster"], "wrapped-cluster")
        wrap_rules = {b["rule"] for b in report_wrap["results"][0]["blockers"]}
        wrap_warnings = {w["rule"] for w in report_wrap["results"][0]["warnings"]}
        self.assertIn(preflight.RULE_SHIELDED_NODES_DISABLED, wrap_warnings)
        self.assertIn(preflight.RULE_DISK_PD_ON_HYPERDISK_ONLY, wrap_rules)

        # 3. Terraform state / .tf.json node pool with 1-element list node_config and shielded_instance_config.
        tf_pool = {
            "name": "tf-list-pool",
            "version": "1.35.3-gke.1290000",
            "node_config": [
                {
                    "machine_type": "n1-standard-8",
                    "disk_type": "pd-standard",
                    "local_ssd_count": 2,
                    "shielded_instance_config": [
                        {
                            "enable_secure_boot": False,
                            "enable_integrity_monitoring": True,
                        }
                    ],
                }
            ],
        }
        code_tf, report_tf = preflight.run_preflight(
            node_pool_json_sources=[json.dumps(tf_pool)],
            target_specs=["n4"],
        )
        self.assertEqual(code_tf, preflight.EXIT_INCOMPATIBLE)
        tf_row = report_tf["results"][0]
        self.assertEqual(tf_row["source_machine_type"], "n1-standard-8")
        tf_rules = {b["rule"] for b in tf_row["blockers"]}
        tf_warnings = {w["rule"] for w in tf_row["warnings"]}
        self.assertIn(preflight.RULE_SHIELDED_SECURE_BOOT_DISABLED, tf_warnings)
        self.assertIn(preflight.RULE_DISK_PD_ON_HYPERDISK_ONLY, tf_rules)
        self.assertIn(preflight.RULE_LSSD_UNSUPPORTED_ON_N4, tf_rules)
        self.assertNotIn(preflight.RULE_SHIELDED_INTEGRITY_DISABLED, tf_warnings)

    def test_shielded_instance_config_schema_distinction_kcc_terraform_vs_gke_proto3(self):
        """KCC and Terraform manifests default omitted enableIntegrityMonitoring to true; omitted enableSecureBoot is a warning, not a blocker."""
        # Hand-written KCC ContainerNodePool with enableSecureBoot: true and omitted enableIntegrityMonitoring.
        kcc_pool = {
            "spec": {
                "nodeConfig": {
                    "machineType": "n1-standard-8",
                    "diskType": "hyperdisk-balanced",
                    "shieldedInstanceConfig": {"enableSecureBoot": True},
                }
            }
        }
        kcc_res = preflight.evaluate_node_pool_transition(kcc_pool, "n4")
        self.assertTrue(kcc_res.compatible)
        self.assertEqual(kcc_res.blockers, ())
        self.assertEqual(kcc_res.warnings, ())

        # GKE describe pool carrying enableVtpm: true and enableIntegrityMonitoring: true but omitting enableSecureBoot.
        gke_pool_with_vtpm = {
            "name": "gke-pool",
            "config": {
                "machineType": "n1-standard-8",
                "diskType": "hyperdisk-balanced",
                "shieldedInstanceConfig": {
                    "enableVtpm": True,
                    "enableIntegrityMonitoring": True,
                },
            },
        }
        gke_res = preflight.evaluate_node_pool_transition(gke_pool_with_vtpm, "n4")
        self.assertTrue(gke_res.compatible)
        self.assertEqual(gke_res.blockers, ())
        gke_warnings = {w.rule for w in gke_res.warnings}
        self.assertIn(preflight.RULE_SHIELDED_SECURE_BOOT_DISABLED, gke_warnings)
        self.assertNotIn(preflight.RULE_SHIELDED_INTEGRITY_DISABLED, gke_warnings)

        # KCC and Terraform manifests that declare shieldedInstanceConfig but omit enableSecureBoot
        # (which defaults to false in KCC and Terraform) emit shielded-secure-boot-disabled as a warning
        # without triggering shielded-integrity-monitoring-disabled.
        kcc_omitted_secure_boot = {
            "spec": {
                "nodeConfig": {
                    "machineType": "n1-standard-8",
                    "diskType": "hyperdisk-balanced",
                    "shieldedInstanceConfig": {"enableIntegrityMonitoring": True},
                }
            }
        }
        kcc_no_sb_res = preflight.evaluate_node_pool_transition(
            kcc_omitted_secure_boot,
            "n4",
        )
        self.assertTrue(kcc_no_sb_res.compatible)
        self.assertEqual(kcc_no_sb_res.blockers, ())
        self.assertEqual(
            {w.rule for w in kcc_no_sb_res.warnings},
            {preflight.RULE_SHIELDED_SECURE_BOOT_DISABLED},
        )

        tf_omitted_secure_boot = {
            "name": "tf-pool-no-sb",
            "node_config": [
                {
                    "machine_type": "n1-standard-8",
                    "disk_type": "hyperdisk-balanced",
                    "shielded_instance_config": [
                        {"enable_integrity_monitoring": True}
                    ],
                }
            ],
        }
        tf_no_sb_res = preflight.evaluate_node_pool_transition(
            tf_omitted_secure_boot,
            "n4",
        )
        self.assertTrue(tf_no_sb_res.compatible)
        self.assertEqual(tf_no_sb_res.blockers, ())
        self.assertEqual(
            {w.rule for w in tf_no_sb_res.warnings},
            {preflight.RULE_SHIELDED_SECURE_BOOT_DISABLED},
        )

    def test_node_pool_json_combined_with_live_cluster_and_location_describes_cluster(self):
        """Combining --node-pool-json with --cluster and --location calls _describe_cluster_live for cluster checks."""
        live_cluster_payload = {
            "name": "live-cluster",
            "location": "us-central1",
            "currentMasterVersion": "1.28.5-gke.1000000",
            "shieldedNodes": {"enabled": False},
        }
        candidate_pool = {
            "name": "candidate-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "n2-standard-4",
                "diskType": "hyperdisk-balanced",
            },
        }
        calls: list[list[str]] = []

        def fake_runner(argv: list[str]) -> tuple[int, str, str]:
            calls.append(argv)
            return 0, json.dumps(live_cluster_payload), ""

        code, report = preflight.run_preflight(
            project="demo-proj",
            location="us-central1",
            cluster_name="live-cluster",
            node_pool_json_sources=[json.dumps(candidate_pool)],
            target_specs=["n4"],
            run=fake_runner,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(code, preflight.EXIT_INCOMPATIBLE)
        self.assertEqual(report["control_plane_version"], "1.28.5-gke.1000000")
        rules = {b["rule"] for b in report["results"][0]["blockers"]}
        warnings = {w["rule"] for w in report["results"][0]["warnings"]}
        self.assertIn(preflight.RULE_SHIELDED_NODES_DISABLED, warnings)
        self.assertIn(preflight.RULE_GKE_MASTER_VERSION_FLOOR, rules)

        # Partial live flags (--cluster without --location) alongside --node-pool-json fail closed.
        code_partial, report_partial = preflight.run_preflight(
            cluster_name="live-cluster",
            node_pool_json_sources=[json.dumps(candidate_pool)],
            target_specs=["n4"],
            run=fake_runner,
        )
        self.assertEqual(code_partial, preflight.EXIT_READ_ERROR)
        self.assertTrue(report_partial["errors"])

    def test_target_machine_type_without_target_family_or_with_multiple_families(self):
        """--target-machine-type narrows the default family when --target-family is omitted and rejects conflicts."""
        pool = {
            "name": "lssd-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "n2-standard-8",
                "diskType": "hyperdisk-balanced",
                "ephemeralStorageLocalSsdConfig": {"localSsdCount": 2},
            },
        }
        # 1. --target-machine-type alone narrows target_families to ['c3'] with shape 'c3-standard-8-lssd'.
        code_alone, report_alone = preflight.run_preflight(
            node_pool_json_sources=[json.dumps(pool)],
            target_machine_type="c3-standard-8-lssd",
        )
        self.assertEqual(code_alone, preflight.EXIT_OK)
        self.assertEqual(report_alone["target_families"], ["c3"])
        self.assertEqual(
            report_alone["results"][0]["target_machine_type"],
            "c3-standard-8-lssd",
        )

        # 2. --target-family c3,c4 --target-machine-type c3-standard-8-lssd applies the shape to c3 and keeps c4.
        code_multi, report_multi = preflight.run_preflight(
            node_pool_json_sources=[json.dumps(pool)],
            target_specs=["c3,c4"],
            target_machine_type="c3-standard-8-lssd",
        )
        self.assertEqual(code_multi, preflight.EXIT_INCOMPATIBLE)
        by_fam = {r["target_family"]: r for r in report_multi["results"]}
        self.assertTrue(by_fam["c3"]["compatible"])
        self.assertEqual(by_fam["c3"]["target_machine_type"], "c3-standard-8-lssd")
        self.assertFalse(by_fam["c4"]["compatible"])
        self.assertEqual(by_fam["c4"]["target_machine_type"], "")

        # 3. Conflicting concrete shapes between --target-family and --target-machine-type raise ValueError.
        with self.assertRaises(ValueError):
            preflight.run_preflight(
                node_pool_json_sources=[json.dumps(pool)],
                target_specs=["c3-standard-8,c3-standard-8-lssd"],
                target_machine_type="c3-highcpu-8",
            )

        # 4. A concrete shape in --target-family for a different family (c4-standard-8-lssd) does not
        # conflict with --target-machine-type c3-standard-8-lssd.
        code_cross, report_cross = preflight.run_preflight(
            node_pool_json_sources=[json.dumps(pool)],
            target_specs=["c3,c4-standard-8-lssd"],
            target_machine_type="c3-standard-8-lssd",
        )
        self.assertEqual(code_cross, preflight.EXIT_OK)
        by_fam_cross = {r["target_family"]: r for r in report_cross["results"]}
        self.assertEqual(
            by_fam_cross["c3"]["target_machine_type"],
            "c3-standard-8-lssd",
        )
        self.assertEqual(
            by_fam_cross["c4"]["target_machine_type"],
            "c4-standard-8-lssd",
        )

        # 5. Empty, whitespace, or comma-only --target-family raises ValueError instead of evaluating all 5 defaults.
        for bad_target in ("", " ", ","):
            with self.subTest(bad_target=bad_target):
                with self.assertRaises(ValueError):
                    preflight.run_preflight(
                        node_pool_json_sources=[json.dumps(pool)],
                        target_specs=[bad_target],
                    )

    def test_two_component_minor_versions_and_unparseable_versions_and_cluster_version_validation(self):
        """Two-component MAJOR.MINOR versions compare at minor precision; unparseable versions emit gke-version-unparseable or fail closed on CLI."""
        kcc_minor_pool = {
            "spec": {
                "resourceID": "minor-pool",
                "version": "1.35",
                "nodeConfig": {
                    "machineType": "n1-standard-4",
                    "diskType": "hyperdisk-balanced",
                },
            }
        }
        code_ok, report_ok = preflight.run_preflight(
            node_pool_json_sources=[json.dumps(kcc_minor_pool)],
            target_specs=["n4"],
            cluster_version_override="1.35",
            require_dynamic_rwo=True,
        )
        self.assertEqual(code_ok, preflight.EXIT_OK)
        self.assertTrue(report_ok["compatible"])
        self.assertEqual(report_ok["results"][0]["warnings"], [])

        # Two-component MAJOR.MINOR equal to each target family's non-zero patch floor minor is compatible.
        for target_fam, minor_ver, boot_disk in (
            ("n4", "1.29", "hyperdisk-balanced"),
            ("c4", "1.30", "hyperdisk-balanced"),
            ("c3", "1.25", "pd-balanced"),
            ("n2d", "1.16", "pd-balanced"),
        ):
            with self.subTest(target_fam=target_fam, minor_ver=minor_ver):
                floor_pool = {
                    "spec": {
                        "resourceID": f"{target_fam}-pool",
                        "version": minor_ver,
                        "nodeConfig": {
                            "machineType": "n1-standard-4",
                            "diskType": boot_disk,
                        },
                    }
                }
                res = preflight.evaluate_node_pool_transition(
                    floor_pool,
                    target_fam,
                    cluster_version_override=minor_ver,
                )
                self.assertTrue(res.compatible, f"unexpected blockers for {target_fam} at {minor_ver}: {res.blockers}")

        # Confidential TDX on c3 with two-component version "1.28" (floor 1.28.6-gke.1317000) is compatible.
        tdx_minor_pool = {
            "name": "tdx-pool",
            "version": "1.28",
            "config": {
                "machineType": "c3-standard-4",
                "diskType": "pd-balanced",
                "confidentialNodes": {
                    "enabled": True,
                    "confidentialInstanceType": "TDX",
                },
            },
        }
        res_tdx = preflight.evaluate_node_pool_transition(
            tdx_minor_pool,
            "c3",
            cluster_version_override="1.28",
        )
        self.assertTrue(res_tdx.compatible)

        # Two-component minor below the floor's minor ("1.28" targeting n4, floor 1.29.2-gke.1521000) is rejected.
        below_minor_pool = {
            "name": "old-minor-pool",
            "version": "1.28",
            "config": {
                "machineType": "n1-standard-4",
                "diskType": "hyperdisk-balanced",
            },
        }
        res_below = preflight.evaluate_node_pool_transition(
            below_minor_pool,
            "n4",
            cluster_version_override="1.28",
        )
        self.assertFalse(res_below.compatible)
        self.assertEqual(
            {b.rule for b in res_below.blockers},
            {
                preflight.RULE_GKE_MASTER_VERSION_FLOOR,
                preflight.RULE_GKE_NODE_VERSION_FLOOR,
            },
        )

        # Non-numeric version in a manifest ('latest') emits gke-version-unparseable rather than a false below-floor message.
        latest_pool = {
            "name": "latest-pool",
            "version": "latest",
            "config": {
                "machineType": "n1-standard-4",
                "diskType": "pd-balanced",
            },
        }
        res_latest = preflight.evaluate_node_pool_transition(latest_pool, "n2")
        self.assertFalse(res_latest.compatible)
        self.assertEqual(
            {b.rule for b in res_latest.blockers},
            {preflight.RULE_GKE_VERSION_UNPARSEABLE},
        )

        # Unparseable or whitespace --cluster-version raises ValueError in run_preflight.
        for bad_ver in ("latest", "   "):
            with self.subTest(bad_ver=bad_ver):
                with self.assertRaises(ValueError):
                    preflight.run_preflight(
                        node_pool_json_sources=[json.dumps(kcc_minor_pool)],
                        target_specs=["n4"],
                        cluster_version_override=bad_ver,
                    )

    def test_multiple_cluster_wrappers_or_mixed_cluster_sources_fail_closed(self):
        """Combining a cluster wrapper in --node-pool-json with another wrapper, --cluster-json, or live flags fails closed."""
        wrapper_a = {
            "name": "cluster-a",
            "shieldedNodes": {"enabled": True},
            "nodePools": [
                {
                    "name": "pool-a",
                    "config": {
                        "machineType": "n2-standard-4",
                        "diskType": "hyperdisk-balanced",
                    },
                }
            ],
        }
        wrapper_b = {
            "name": "cluster-b",
            "shieldedNodes": {"enabled": False},
            "nodePools": [
                {
                    "name": "pool-b",
                    "config": {
                        "machineType": "n2-standard-4",
                        "diskType": "hyperdisk-balanced",
                    },
                }
            ],
        }
        code_two_wrappers, report_two_wrappers = preflight.run_preflight(
            node_pool_json_sources=[json.dumps(wrapper_a), json.dumps(wrapper_b)],
            target_specs=["n4"],
        )
        self.assertEqual(code_two_wrappers, preflight.EXIT_READ_ERROR)
        self.assertFalse(report_two_wrappers["compatible"])
        self.assertTrue(report_two_wrappers["errors"])

        code_cluster_and_wrapper, _ = preflight.run_preflight(
            cluster_json_source=json.dumps(wrapper_a),
            node_pool_json_sources=[json.dumps(wrapper_b)],
            target_specs=["n4"],
        )
        self.assertEqual(code_cluster_and_wrapper, preflight.EXIT_READ_ERROR)

        unexpected_calls: list[list[str]] = []

        def unexpected_runner(argv: list[str]) -> tuple[int, str, str]:
            unexpected_calls.append(argv)
            return 0, "{}", ""

        code_live_and_wrapper, report_live_and_wrapper = preflight.run_preflight(
            cluster_name="live-cluster",
            location="us-central1",
            node_pool_json_sources=[json.dumps(wrapper_b)],
            target_specs=["n4"],
            run=unexpected_runner,
        )
        self.assertEqual(code_live_and_wrapper, preflight.EXIT_READ_ERROR)
        self.assertEqual(unexpected_calls, [])
        self.assertEqual(len(report_live_and_wrapper["errors"]), 1)
        self.assertIn(
            "cluster wrapper in --node-pool-json",
            report_live_and_wrapper["errors"][0]["message"],
        )

    def test_cvm_pool_with_local_ssd_targeting_c3_reports_combined_conflict(self):
        """Confidential VM on c3 rejects localNvmeSsdBlockConfig while allowing c3-standard-*-lssd with ephemeralStorageLocalSsdConfig."""
        cvm_lssd_pool = {
            "name": "cvm-lssd-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "n2d-standard-8",
                "diskType": "pd-balanced",
                "localNvmeSsdBlockConfig": {"localSsdCount": 2},
                "confidentialNodes": {
                    "enabled": True,
                    "confidentialInstanceType": "SEV",
                },
            },
        }
        res_c3 = preflight.evaluate_node_pool_transition(cvm_lssd_pool, "c3")
        self.assertFalse(res_c3.compatible)
        machine_type_cvm_blockers = [
            b
            for b in res_c3.blockers
            if b.rule == preflight.RULE_CVM_TECHNOLOGY_MISMATCH
            and b.field == preflight.FIELD_MACHINE_TYPE
        ]
        self.assertEqual(len(machine_type_cvm_blockers), 1)
        self.assertIn(
            preflight.FIELD_NVME_SSD_BLOCK,
            machine_type_cvm_blockers[0].message,
        )

        # Intel TDX on c3-standard-8-lssd with ephemeralStorageLocalSsdConfig targeting c3-standard-8-lssd is compatible.
        tdx_ephemeral_lssd_pool = {
            "name": "tdx-ephemeral-lssd-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "c3-standard-8-lssd",
                "diskType": "pd-balanced",
                "ephemeralStorageLocalSsdConfig": {"localSsdCount": 2},
                "confidentialNodes": {
                    "enabled": True,
                    "confidentialInstanceType": "TDX",
                },
            },
        }
        res_c3_lssd = preflight.evaluate_node_pool_transition(
            tdx_ephemeral_lssd_pool,
            "c3-standard-8-lssd",
        )
        self.assertTrue(res_c3_lssd.compatible)
        self.assertEqual(res_c3_lssd.blockers, ())

    def test_kcc_container_cluster_and_terraform_cluster_fields_normalized(self):
        """Config Connector ContainerCluster spec fields and Terraform min_master_version/master_version are enforced at cluster level."""
        kcc_cluster = {
            "apiVersion": "container.cnrm.cloud.google.com/v1beta1",
            "kind": "ContainerCluster",
            "metadata": {"name": "kcc-prod-cluster"},
            "spec": {
                "location": "us-central1",
                "minMasterVersion": "1.28.5-gke.1000000",
                "enableShieldedNodes": False,
                "nodeConfig": {
                    "shieldedInstanceConfig": {
                        "enableSecureBoot": False,
                        "enableVtpm": False,
                    }
                },
                "confidentialNodes": {
                    "enabled": True,
                    "confidentialInstanceType": "SEV",
                },
            },
        }
        pool = {
            "name": "worker-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "n2d-standard-4",
                "diskType": "hyperdisk-balanced",
            },
        }
        code_kcc, report_kcc = preflight.run_preflight(
            cluster_json_source=json.dumps(kcc_cluster),
            node_pool_json_sources=[json.dumps(pool)],
            target_specs=["n4"],
        )
        self.assertEqual(code_kcc, preflight.EXIT_INCOMPATIBLE)
        self.assertEqual(report_kcc["cluster"], "kcc-prod-cluster")
        self.assertEqual(report_kcc["location"], "us-central1")
        self.assertEqual(report_kcc["control_plane_version"], "1.28.5-gke.1000000")
        kcc_rules = {b["rule"] for b in report_kcc["results"][0]["blockers"]}
        kcc_warnings = {w["rule"] for w in report_kcc["results"][0]["warnings"]}
        self.assertIn(preflight.RULE_SHIELDED_NODES_DISABLED, kcc_warnings)
        self.assertIn(preflight.RULE_SHIELDED_VTPM_DISABLED, kcc_warnings)
        self.assertIn(preflight.RULE_SHIELDED_SECURE_BOOT_DISABLED, kcc_warnings)
        self.assertNotIn(preflight.RULE_SHIELDED_INTEGRITY_DISABLED, kcc_warnings)
        self.assertIn(preflight.RULE_CVM_UNSUPPORTED_FAMILY, kcc_rules)
        self.assertIn(preflight.RULE_GKE_MASTER_VERSION_FLOOR, kcc_rules)

        # Terraform google_container_cluster with min_master_version and master_version.
        tf_cluster = {
            "name": "tf-prod-cluster",
            "location": "us-east1",
            "min_master_version": "1.28.5-gke.1000000",
            "enable_shielded_nodes": False,
        }
        code_tf, report_tf = preflight.run_preflight(
            cluster_json_source=json.dumps(tf_cluster),
            node_pool_json_sources=[json.dumps(pool)],
            target_specs=["n4"],
        )
        self.assertEqual(code_tf, preflight.EXIT_INCOMPATIBLE)
        self.assertEqual(report_tf["control_plane_version"], "1.28.5-gke.1000000")
        tf_rules = {b["rule"] for b in report_tf["results"][0]["blockers"]}
        tf_warnings = {w["rule"] for w in report_tf["results"][0]["warnings"]}
        self.assertIn(preflight.RULE_SHIELDED_NODES_DISABLED, tf_warnings)
        self.assertIn(preflight.RULE_GKE_MASTER_VERSION_FLOOR, tf_rules)

        tf_master_ver_cluster = {
            "name": "tf-master-ver-cluster",
            "master_version": "1.28.1-gke.100",
        }
        res_tf_master = preflight.evaluate_node_pool_transition(
            pool,
            "c3",
            cluster={
                **tf_master_ver_cluster,
                "confidential_nodes": [{"enabled": True, "confidential_instance_type": "TDX"}],
            },
        )
        self.assertFalse(res_tf_master.compatible)
        self.assertIn(
            preflight.RULE_GKE_CVM_TDX_VERSION_FLOOR,
            {b.rule for b in res_tf_master.blockers},
        )

    def test_compute_class_storage_local_ssd_count_enforced(self):
        """ComputeClass priority-shaped objects with storage.localSSDCount enforce Local SSD blockers on n4, c3, and c4."""
        cc_priority_pool = {
            "name": "cc-priority-pool",
            "machineType": "n2-standard-8",
            "storage": {
                "bootDiskType": "hyperdisk-balanced",
                "localSSDCount": 2,
            },
        }
        res_n4 = preflight.evaluate_node_pool_transition(cc_priority_pool, "n4")
        self.assertFalse(res_n4.compatible)
        self.assertIn(
            preflight.RULE_LSSD_UNSUPPORTED_ON_N4,
            {b.rule for b in res_n4.blockers},
        )

        res_c3 = preflight.evaluate_node_pool_transition(cc_priority_pool, "c3")
        self.assertFalse(res_c3.compatible)
        self.assertEqual(
            {b.rule for b in res_c3.blockers},
            {
                preflight.RULE_LSSD_REQUIRES_LSSD_SHAPE,
                preflight.RULE_LSSD_LEGACY_COUNT_ON_GEN3_GEN4,
            },
        )

    def test_blank_node_pool_raises_and_duplicate_targets_deduplicated(self):
        """Blank --node-pool values raise ValueError and duplicate --target-family entries are deduplicated."""
        pool = {
            "name": "pool-a",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "n2-standard-4",
                "diskType": "hyperdisk-balanced",
            },
        }
        for blank_pool in ("", "   "):
            with self.subTest(blank_pool=blank_pool):
                with self.assertRaises(ValueError):
                    preflight.run_preflight(
                        node_pool_json_sources=[json.dumps(pool)],
                        node_pool_names=[blank_pool],
                        target_specs=["n4"],
                    )

        code_dup, report_dup = preflight.run_preflight(
            node_pool_json_sources=[json.dumps(pool)],
            target_specs=["n4,n4", "n4"],
        )
        self.assertEqual(code_dup, preflight.EXIT_OK)
        self.assertEqual(report_dup["target_families"], ["n4"])
        self.assertEqual(len(report_dup["results"]), 1)


if __name__ == "__main__":
    unittest.main()
