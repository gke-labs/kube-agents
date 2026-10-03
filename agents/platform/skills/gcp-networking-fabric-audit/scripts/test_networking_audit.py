#!/usr/bin/env python3
"""Unit tests for networking_audit.py."""

import io
import json
import tempfile
import unittest
from unittest.mock import patch

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import networking_audit

class TestNetworkingAudit(unittest.TestCase):
    @patch("networking_audit.run_gcloud_json")
    def test_audit_project_networking_rejected_psc(self, mock_gcloud):
        mock_gcloud.return_value = [
            {
                "name": "psc-ep-1",
                "region": "projects/p/regions/us-central1",
                "target": "projects/p/regions/us-central1/serviceAttachments/sa-1",
                "pscConnectionStatus": "REJECTED"
            },
            {
                "name": "psc-ep-2",
                "region": "projects/p/regions/us-central1",
                "target": "projects/p/regions/us-central1/serviceAttachments/sa-2",
                "pscConnectionStatus": "ACCEPTED"
            }
        ]

        skipped = []
        active = []
        findings = networking_audit.audit_project_networking("test-proj", skipped, active)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "psc-routing-deadlock")
        self.assertEqual(findings[0]["cluster"], "project/test-proj")
        self.assertEqual(findings[0]["object"], "ForwardingRule/psc-ep-1")
        self.assertEqual(len(skipped), 0)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["name"], "project/test-proj")
        self.assertEqual(active[0]["project"], "test-proj")
        self.assertEqual(active[0]["checks_run"][0]["check"], "psc-routing-deadlock")

    @patch("networking_audit.run_gcloud_json")
    def test_audit_project_networking_empty(self, mock_gcloud):
        mock_gcloud.return_value = []
        skipped = []
        active = []
        findings = networking_audit.audit_project_networking("test-proj", skipped, active)
        self.assertEqual(findings, [])
        self.assertEqual(len(skipped), 0)
        self.assertEqual(len(active), 1)

    @patch("networking_audit.run_gcloud_json")
    def test_unreadable_project_is_skipped_not_fatal(self, mock_gcloud):
        mock_gcloud.return_value = None
        skipped = []
        active = []
        findings = networking_audit.audit_project_networking("denied-proj", skipped, active)
        self.assertEqual(findings, [])
        self.assertEqual(len(active), 0)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["cluster"], "project/denied-proj")
        self.assertEqual(skipped[0]["project"], "denied-proj")
        self.assertIn("Failed to list forwarding rules", skipped[0]["reason"])

    @patch("networking_audit.run_cmd")
    def test_api_disabled_project_is_ignored_without_skip(self, mock_run_cmd):
        mock_run_cmd.return_value = (
            1,
            "",
            "ERROR: (gcloud.compute.forwarding-rules.list) SERVICE_DISABLED: Compute Engine API has not been used in project no-compute-proj",
        )
        skipped = []
        active = []
        findings = networking_audit.audit_project_networking("no-compute-proj", skipped, active)
        self.assertEqual(findings, [])
        self.assertEqual(active, [])
        self.assertEqual(skipped, [])

    def _refusal_run(self, own_number):
        def fake(cmd, *args, **kwargs):
            if cmd[:3] == ["gcloud", "projects", "describe"]:
                return (0, f"{own_number}\n", "")
            return (
                1,
                "",
                "ERROR: (gcloud.compute.forwarding-rules.list) SERVICE_DISABLED: Compute Engine API "
                "has not been used in project 111111111111 before or it is disabled.",
            )
        return fake

    def test_quota_project_refusal_is_a_failed_read(self):
        """A refusal naming another project's number says nothing about this one."""
        skipped, active = [], []
        with patch("networking_audit.run_cmd", side_effect=self._refusal_run("222222222222")):
            networking_audit.audit_project_networking("real-proj", skipped, active)
        self.assertEqual(active, [])
        self.assertEqual([t["cluster"] for t in skipped], ["project/real-proj"])

    def test_own_numbered_refusal_is_empty(self):
        skipped, active = [], []
        with patch("networking_audit.run_cmd", side_effect=self._refusal_run("111111111111")):
            networking_audit.audit_project_networking("real-proj", skipped, active)
        self.assertEqual((active, skipped), ([], []))


class MainSweepTest(unittest.TestCase):
    def test_one_denied_project_does_not_abort_the_rest(self):
        """The whole point of the sweep: a 403 on one project still audits the others."""
        rejected_rule = [{
            "name": "psc-ep-1",
            "region": "projects/readable/regions/us-central1",
            "target": "projects/readable/regions/us-central1/serviceAttachments/sa-1",
            "pscConnectionStatus": "REJECTED",
        }]

        def fake_gcloud_json(cmd):
            return None if "denied" in cmd else rejected_rule

        argv = ["networking_audit.py", "--output", os.path.join(self.tmpdir, "findings.json")]
        with patch.object(networking_audit, "get_target_projects", return_value=["denied", "readable"]), \
                patch.object(networking_audit, "run_gcloud_json", side_effect=fake_gcloud_json), \
                patch.object(sys, "argv", argv), \
                patch("sys.stdout", new_callable=io.StringIO):
            networking_audit.main()

        with open(os.path.join(self.tmpdir, "findings.json"), encoding="utf-8") as f:
            doc = json.load(f)

        self.assertEqual(doc["audit"], "gcp-networking-fabric-audit")
        self.assertEqual(len(doc["findings"]), 1)
        self.assertEqual([t["project"] for t in doc["scope"]["clusters"]], ["readable"])
        self.assertEqual([t["project"] for t in doc["scope"]["skipped"]], ["denied"])

    def test_no_projects_resolved_records_unknown_skip(self):
        argv = ["networking_audit.py", "--output", os.path.join(self.tmpdir, "empty.json")]
        with patch.object(networking_audit, "get_target_projects", return_value=[]), \
                patch.object(sys, "argv", argv), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO):
            networking_audit.main()

        with open(os.path.join(self.tmpdir, "empty.json"), encoding="utf-8") as f:
            doc = json.load(f)

        self.assertEqual(doc["scope"]["clusters"], [])
        self.assertEqual(doc["scope"]["skipped"][0]["project"], "unknown")

    def test_failed_projects_list_is_a_skipped_target_not_a_narrowed_sweep(self):
        """A listing failure must make the run partial, not read as the whole fleet."""
        env = {
            networking_audit.MONITORED_PROJECTS_ENV: "",
            "GCP_PROJECT_ID": "p-host",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }

        def fake_run(cmd, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (1, "", "ERROR: cloudresourcemanager.googleapis.com is not reachable")
            if "config" in cmd:
                return (0, "", "")
            return (0, "[]", "")

        argv = ["networking_audit.py", "--output", os.path.join(self.tmpdir, "narrowed.json")]
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=False), \
                patch.object(networking_audit, "run_cmd", side_effect=fake_run), \
                patch.object(sys, "argv", argv), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", stderr):
            networking_audit.main()

        with open(os.path.join(self.tmpdir, "narrowed.json"), encoding="utf-8") as f:
            doc = json.load(f)

        self.assertEqual([t["project"] for t in doc["scope"]["clusters"]], ["p-host"])
        self.assertEqual([t["cluster"] for t in doc["scope"]["skipped"]], ["project/UNENUMERATED_PROJECTS"])
        self.assertIn("gcloud projects list", doc["scope"]["skipped"][0]["reason"])
        self.assertIn("gcloud projects list", stderr.getvalue())

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)


class ProjectResolutionTest(unittest.TestCase):
    def test_cli_project_wins(self):
        with patch.dict(os.environ, {networking_audit.MONITORED_PROJECTS_ENV: "x,y"}):
            self.assertEqual(networking_audit.get_target_projects("cli-proj"), ["cli-proj"])

    def test_env_projects_merge(self):
        env = {
            networking_audit.MONITORED_PROJECTS_ENV: "m1, m2 m3",
            "GCP_PROJECT_ID": "g1",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(
                networking_audit.get_target_projects(None),
                ["g1", "m1", "m2", "m3"],
            )

    def test_gcloud_default_when_nothing_set(self):
        env = {
            networking_audit.MONITORED_PROJECTS_ENV: "",
            "GCP_PROJECT_ID": "",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            networking_audit, "run_cmd", return_value=(0, "from-gcloud\n", "")
        ):
            self.assertEqual(networking_audit.get_target_projects(None), ["from-gcloud"])

    def test_projects_list_discovered_when_monitored_not_set(self):
        env = {
            networking_audit.MONITORED_PROJECTS_ENV: "",
            "GCP_PROJECT_ID": "p-host",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }

        def fake_run(cmd, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (0, "p-host\np-extra\n", "")
            return (0, "", "")

        with patch.dict(os.environ, env, clear=False), patch.object(
            networking_audit, "run_cmd", side_effect=fake_run
        ):
            self.assertEqual(
                networking_audit.get_target_projects(None),
                ["p-extra", "p-host"],
            )

    def test_listing_that_omits_the_host_project_is_reported_as_filtered(self):
        env = {
            networking_audit.MONITORED_PROJECTS_ENV: "",
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
            networking_audit, "run_cmd", side_effect=fake_run
        ):
            projects = networking_audit.get_target_projects(None, errors)
        self.assertEqual(projects, ["p-extra", "p-host"])
        self.assertEqual(len(errors), 1)
        self.assertIn("did not name p-host", errors[0])

    def test_blank_monitored_projects_runs_discovery(self):
        env = {
            networking_audit.MONITORED_PROJECTS_ENV: " , ",
            "GCP_PROJECT_ID": "p-host",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }

        def fake_run(cmd, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (0, "p-host\np-extra\n", "")
            return (0, "", "")

        errors: list[str] = []
        with patch.dict(os.environ, env, clear=False), patch.object(
            networking_audit, "run_cmd", side_effect=fake_run
        ):
            self.assertEqual(networking_audit.get_target_projects(None, errors), ["p-extra", "p-host"])
        self.assertEqual(errors, [])

    def test_config_project_is_unioned_even_when_env_names_one(self):
        env = {
            networking_audit.MONITORED_PROJECTS_ENV: "",
            "GCP_PROJECT_ID": "p-env",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }

        def fake_run(cmd, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (0, "p-env\np-config\n", "")
            if "config" in cmd:
                return (0, "p-config\n", "")
            return (0, "", "")

        errors: list[str] = []
        with patch.dict(os.environ, env, clear=False), patch.object(
            networking_audit, "run_cmd", side_effect=fake_run
        ) as run:
            self.assertEqual(networking_audit.get_target_projects(None, errors), ["p-config", "p-env"])
        self.assertIn(list(networking_audit.CONFIG_PROJECT_CMD), [c.args[0] for c in run.call_args_list])
        self.assertEqual(errors, [])

    def test_narrowed_runs_are_reported_so_other_projects_are_not_resolved(self):
        with patch.dict(os.environ, {networking_audit.MONITORED_PROJECTS_ENV: ""}):
            errors: list[str] = []
            self.assertEqual(networking_audit.get_target_projects("cli-proj", errors), ["cli-proj"])
            self.assertEqual(len(errors), 1)
            self.assertIn("--project-id", errors[0])
        env = {
            networking_audit.MONITORED_PROJECTS_ENV: "m1,m2",
            "GCP_PROJECT_ID": "",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(networking_audit, "run_cmd") as run:
            errors = []
            self.assertEqual(networking_audit.get_target_projects(None, errors), ["m1", "m2"])
            run.assert_not_called()
        self.assertEqual(len(errors), 1)
        self.assertIn(networking_audit.MONITORED_PROJECTS_ENV, errors[0])

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "findings.json")
            with patch.object(networking_audit, "run_gcloud_json", return_value=[]), \
                    patch.object(networking_audit, "run_cmd", return_value=(0, "", "")), \
                    patch.object(sys, "argv", ["networking_audit.py", "--project-id", "cli-proj", "--output", out]), \
                    patch("sys.stdout", new_callable=io.StringIO), \
                    patch("sys.stderr", new_callable=io.StringIO):
                networking_audit.main()
            with open(out, encoding="utf-8") as f:
                doc = json.load(f)
        self.assertIn("project/UNENUMERATED_PROJECTS", [t["cluster"] for t in doc["scope"]["skipped"]])



if __name__ == "__main__":
    unittest.main()
