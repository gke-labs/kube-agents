#!/usr/bin/env python3
"""Unit tests for compute_fleet_audit.py."""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import compute_fleet_audit


class ProjectResolutionTest(unittest.TestCase):
    def test_cli_project_wins(self):
        with patch.dict(os.environ, {compute_fleet_audit.MONITORED_PROJECTS_ENV: "x,y"}):
            self.assertEqual(compute_fleet_audit.get_target_projects("cli-proj"), ["cli-proj"])

    def test_env_projects_merge(self):
        env = {
            compute_fleet_audit.MONITORED_PROJECTS_ENV: "m1, m2 m3",
            "GCP_PROJECT_ID": "g1",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(
                compute_fleet_audit.get_target_projects(None),
                ["g1", "m1", "m2", "m3"],
            )

    def test_gcloud_default_when_nothing_set(self):
        env = {
            compute_fleet_audit.MONITORED_PROJECTS_ENV: "",
            "GCP_PROJECT_ID": "",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            compute_fleet_audit, "run_cmd", return_value=(0, "from-gcloud\n", "")
        ):
            self.assertEqual(compute_fleet_audit.get_target_projects(None), ["from-gcloud"])

    def test_projects_list_discovered_when_monitored_not_set(self):
        env = {
            compute_fleet_audit.MONITORED_PROJECTS_ENV: "",
            "GCP_PROJECT_ID": "p-host",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }

        def fake_run(cmd, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (0, "p-host\np-extra\n", "")
            return (0, "", "")

        with patch.dict(os.environ, env, clear=False), patch.object(
            compute_fleet_audit, "run_cmd", side_effect=fake_run
        ):
            self.assertEqual(
                compute_fleet_audit.get_target_projects(None),
                ["p-extra", "p-host"],
            )

    def test_listing_that_omits_the_host_project_is_reported_as_filtered(self):
        env = {
            compute_fleet_audit.MONITORED_PROJECTS_ENV: "",
            "GCP_PROJECT_ID": "p-host",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }

        def fake_run(cmd, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (0, "p-extra\n", "")
            return (0, "", "")

        errors: list[str] = []
        with patch.dict(os.environ, env, clear=False), patch.object(
            compute_fleet_audit, "run_cmd", side_effect=fake_run
        ):
            projects = compute_fleet_audit.get_target_projects(None, errors)
        self.assertEqual(projects, ["p-extra", "p-host"])
        self.assertEqual(len(errors), 1)
        self.assertIn("did not name p-host", errors[0])

    def test_failed_projects_list_is_reported_and_recorded_as_skipped(self):
        env = {
            compute_fleet_audit.MONITORED_PROJECTS_ENV: "",
            "GCP_PROJECT_ID": "p-host",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }

        def fake_run(cmd, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (1, "", "ERROR: PERMISSION_DENIED resourcemanager.projects.list")
            return (0, "[]", "")

        errors: list[str] = []
        with patch.dict(os.environ, env, clear=False), patch.object(
            compute_fleet_audit, "run_cmd", side_effect=fake_run
        ):
            self.assertEqual(compute_fleet_audit.get_target_projects(None, errors), ["p-host"])
        self.assertEqual(len(errors), 1)
        self.assertIn("PERMISSION_DENIED", errors[0])

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "findings.json")
            with patch.dict(os.environ, env, clear=False), \
                    patch.object(compute_fleet_audit, "run_cmd", side_effect=fake_run), \
                    patch.object(sys, "argv", ["compute_fleet_audit.py", "--output", out]), \
                    patch("sys.stdout", new_callable=io.StringIO), \
                    patch("sys.stderr", new_callable=io.StringIO):
                compute_fleet_audit.main()
            with open(out, encoding="utf-8") as f:
                doc = json.load(f)
        self.assertIn("project/UNENUMERATED_PROJECTS", [t["cluster"] for t in doc["scope"]["skipped"]])


class AuditComputeTest(unittest.TestCase):
    @patch("compute_fleet_audit.run_gcloud_json")
    @patch("compute_fleet_audit.run_cmd")
    def test_startup_script_failure_detected(self, mock_run_cmd, mock_gcloud_json):
        mock_gcloud_json.side_effect = [
            [{"name": "vm-fail", "zone": "zones/us-central1-a", "status": "RUNNING"}],
            [],  # disks
            [],  # snapshots
        ]
        mock_run_cmd.return_value = (0, "Oct 12 10:00:00 Finished running startup scripts with error\n", "")

        skipped = []
        active = []
        findings = compute_fleet_audit.audit_project_compute("test-proj", skipped, active)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "gce-startup-script-status")
        self.assertEqual(findings[0]["cluster"], "project/test-proj")
        self.assertEqual(findings[0]["object"], "ComputeInstance/vm-fail")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["name"], "project/test-proj")
        self.assertEqual(active[0]["project"], "test-proj")
        self.assertEqual(len(skipped), 0)

    @patch("compute_fleet_audit.run_gcloud_json")
    def test_failed_instance_list_skips_project(self, mock_gcloud_json):
        mock_gcloud_json.return_value = None

        skipped = []
        active = []
        findings = compute_fleet_audit.audit_project_compute("test-proj", skipped, active)

        self.assertEqual(len(findings), 0)
        self.assertEqual(len(active), 0)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["cluster"], "project/test-proj")
        self.assertIn("Failed to list compute instances", skipped[0]["reason"])

    @patch("compute_fleet_audit.run_cmd")
    def test_api_disabled_project_is_ignored_without_skip(self, mock_run_cmd):
        mock_run_cmd.return_value = (
            1,
            "",
            "ERROR: (gcloud.compute.instances.list) SERVICE_DISABLED: Compute Engine API has not been used in project no-compute-proj",
        )
        skipped = []
        active = []
        findings = compute_fleet_audit.audit_project_compute("no-compute-proj", skipped, active)
        self.assertEqual(findings, [])
        self.assertEqual(active, [])
        self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()
