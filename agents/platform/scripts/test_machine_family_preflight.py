#!/usr/bin/env python3
"""Unit tests for machine_family_preflight.py."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

        rules = {b.rule for b in result.blockers}
        self.assertIn(preflight.RULE_SHIELDED_NODES_DISABLED, rules)
        self.assertIn(preflight.RULE_SHIELDED_VTPM_DISABLED, rules)
        self.assertIn(preflight.RULE_SHIELDED_INTEGRITY_DISABLED, rules)
        self.assertIn(preflight.RULE_SHIELDED_SECURE_BOOT_DISABLED, rules)
        self.assertIn(preflight.RULE_DISK_PD_ON_HYPERDISK_ONLY, rules)
        self.assertIn(preflight.RULE_LSSD_UNSUPPORTED_ON_N4, rules)

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
        """Confidential GKE Nodes are rejected on n2, n4, and c4."""
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

    def test_confidential_vm_sev_to_tdx_and_tdx_to_sev_mismatches(self):
        """AMD SEV/SEV_SNP on n2d versus Intel TDX on c3 transitions are flagged."""
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
        # SEV_SNP -> n2d passes.
        to_n2d = preflight.evaluate_node_pool_transition(sev_pool, "n2d")
        self.assertTrue(to_n2d.compatible)

        tdx_pool = {
            "name": "intel-tdx-pool",
            "version": "1.35.3-gke.1290000",
            "config": {
                "machineType": "c3-standard-8",
                "diskType": "pd-balanced",
                "confidentialNodes": {
                    "enabled": True,
                    "confidentialInstanceType": "TDX",
                },
            },
        }
        # TDX -> n2d fails with technology mismatch.
        tdx_to_n2d = preflight.evaluate_node_pool_transition(tdx_pool, "n2d")
        self.assertFalse(tdx_to_n2d.compatible)
        self.assertIn(
            preflight.RULE_CVM_TECHNOLOGY_MISMATCH,
            {b.rule for b in tdx_to_n2d.blockers},
        )
        # TDX -> c3-standard-8 passes.
        tdx_to_c3 = preflight.evaluate_node_pool_transition(
            tdx_pool,
            "c3",
            target_machine_type="c3-standard-8",
        )
        self.assertTrue(tdx_to_c3.compatible)
        # TDX -> c3-highcpu-8 fails because TDX requires c3-standard-* without -lssd.
        tdx_to_highcpu = preflight.evaluate_node_pool_transition(
            tdx_pool,
            "c3",
            target_machine_type="c3-highcpu-8",
        )
        self.assertFalse(tdx_to_highcpu.compatible)
        self.assertIn(
            preflight.RULE_CVM_TECHNOLOGY_MISMATCH,
            {b.rule for b in tdx_to_highcpu.blockers},
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

    def test_local_ssd_rejected_on_n4_and_non_lssd_c3_c4(self):
        """All Local SSD configurations fail on n4 and require NVMe -lssd shapes on c3/c4."""
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
                self.assertIn(
                    preflight.RULE_LSSD_REQUIRES_LSSD_SHAPE,
                    {b.rule for b in without_lssd_shape.blockers},
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
