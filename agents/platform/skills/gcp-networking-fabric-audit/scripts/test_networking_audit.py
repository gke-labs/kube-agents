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
    def setUp(self):
        # Project numbers are remembered for a run; each test is its own run.
        networking_audit.PROJECT_NUMBERS.clear()
        self.addCleanup(networking_audit.PROJECT_NUMBERS.clear)

    @patch("networking_audit.run_gcloud_json")
    def test_audit_project_networking_rejected_psc(self, mock_gcloud):
        mock_gcloud.return_value = ([
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
        ], None)

        skipped = []
        active = []
        findings = networking_audit.audit_project_networking("test-proj", skipped, active)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "psc-routing-deadlock")
        self.assertEqual(findings[0]["cluster"], "project/test-proj")
        self.assertEqual(findings[0]["object"], "ForwardingRule/psc-ep-1")
        self.assertEqual(findings[0]["remediation"], {"kind": "gcloud", "path": ""})
        self.assertEqual(len(skipped), 0)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["name"], "project/test-proj")
        self.assertEqual(active[0]["project"], "test-proj")
        self.assertEqual(active[0]["checks_run"][0]["check"], "psc-routing-deadlock")

    @patch("networking_audit.run_gcloud_json")
    def test_audit_project_networking_empty(self, mock_gcloud):
        mock_gcloud.return_value = ([], None)
        skipped = []
        active = []
        findings = networking_audit.audit_project_networking("test-proj", skipped, active)
        self.assertEqual(findings, [])
        self.assertEqual(len(skipped), 0)
        self.assertEqual(len(active), 1)

    @patch("networking_audit.run_gcloud_json")
    def test_unreadable_project_is_skipped_not_fatal(self, mock_gcloud):
        mock_gcloud.return_value = (None, "gcloud compute forwarding-rules list --project denied-proj --format=json failed (1): PERMISSION_DENIED")
        skipped = []
        active = []
        findings = networking_audit.audit_project_networking("denied-proj", skipped, active)
        self.assertEqual(findings, [])
        self.assertEqual(len(active), 0)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["cluster"], "project/denied-proj")
        self.assertEqual(skipped[0]["project"], "denied-proj")
        self.assertIn("PERMISSION_DENIED", skipped[0]["reason"])

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
        with patch("networking_audit.run_cmd", side_effect=self._refusal_run("222222222222")), \
                patch("sys.stderr", new_callable=io.StringIO):
            networking_audit.audit_project_networking("real-proj", skipped, active)
        self.assertEqual(active, [])
        self.assertEqual([t["cluster"] for t in skipped], ["project/real-proj"])
        self.assertIn(
            "the API is off in a project other than 'real-proj', such as a quota project",
            skipped[0]["reason"],
        )

        def describe_failed(cmd, *args, **kwargs):
            if cmd[:3] == ["gcloud", "projects", "describe"]:
                return (1, "", "PERMISSION_DENIED: resourcemanager.projects.get")
            return self._refusal_run("222222222222")(cmd, *args, **kwargs)

        networking_audit.PROJECT_NUMBERS.clear()  # a second run
        skipped, active = [], []
        with patch("networking_audit.run_cmd", side_effect=describe_failed), \
                patch("sys.stderr", new_callable=io.StringIO):
            networking_audit.audit_project_networking("real-proj", skipped, active)
        self.assertEqual(active, [])
        self.assertIn(
            "`gcloud projects describe real-proj` failed (rc=1), so the refusal's project number could not be compared",
            skipped[0]["reason"],
        )

    def test_project_number_is_described_once_per_run(self):
        calls = []

        def run(cmd, *args, **kwargs):
            calls.append(cmd)
            return self._refusal_run("111111111111")(cmd, *args, **kwargs)

        with patch("networking_audit.run_cmd", side_effect=run), patch("sys.stderr", new_callable=io.StringIO):
            for _ in range(3):
                networking_audit.run_gcloud_json(["gcloud", "compute", "instances", "list", "--project", "real-proj"])
        self.assertEqual(sum(cmd[:3] == ["gcloud", "projects", "describe"] for cmd in calls), 1)

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

        def fake_gcloud_json(cmd, warnings=None):
            if any("denied" in arg for arg in cmd):
                return (None, "failed")
            # The subnet sweep's reads find nothing; this test is about the PSC sweep.
            return (rejected_rule, None) if cmd[-1] == "--format=json" else ([], None)

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
        self.assertEqual(
            [t["cluster"] for t in doc["scope"]["skipped"]],
            ["project/denied", "denied/UNENUMERATED_SUBNETS"],
        )

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

    def test_killed_run_leaves_no_stale_document(self):
        out = os.path.join(self.tmpdir, "findings.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"audit": "gcp-networking-fabric-audit", "findings": ["yesterday"]}, f)
        with patch.object(networking_audit, "get_target_projects", side_effect=KeyboardInterrupt), \
                patch.object(sys, "argv", ["networking_audit.py", "--output", out]):
            with self.assertRaises(KeyboardInterrupt):
                networking_audit.main()
        self.assertFalse(os.path.exists(out))

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
            with patch.object(networking_audit, "run_gcloud_json", return_value=([], None)), \
                    patch.object(networking_audit, "run_cmd", return_value=(0, "", "")), \
                    patch.object(sys, "argv", ["networking_audit.py", "--project-id", "cli-proj", "--output", out]), \
                    patch("sys.stdout", new_callable=io.StringIO), \
                    patch("sys.stderr", new_callable=io.StringIO):
                networking_audit.main()
            with open(out, encoding="utf-8") as f:
                doc = json.load(f)
        self.assertIn("project/UNENUMERATED_PROJECTS", [t["cluster"] for t in doc["scope"]["skipped"]])

    def test_numeric_project_id_is_normalised_before_comparing_with_listing(self):
        env = {
            networking_audit.MONITORED_PROJECTS_ENV: "",
            "GCP_PROJECT_ID": "123456789012",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["gcloud", "projects", "describe"]:
                return (0, "p-host\n", "")
            if "projects" in cmd and "list" in cmd:
                return (0, "p-host\np-extra\n", "")
            return (0, "", "")

        errors: list[str] = []
        with patch.dict(os.environ, env, clear=False), patch.object(
            networking_audit, "run_cmd", side_effect=fake_run
        ):
            self.assertEqual(networking_audit.get_target_projects(None, errors), ["p-extra", "p-host"])
        self.assertEqual(errors, [])

    def test_numeric_project_id_describe_failure_records_error_without_double_auditing(self):
        env = {
            networking_audit.MONITORED_PROJECTS_ENV: "",
            "GCP_PROJECT_ID": "123456789012",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["gcloud", "projects", "describe"]:
                return (1, "", "PERMISSION_DENIED: resourcemanager.projects.get denied")
            if "config" in cmd:
                return (0, "p-host\n", "")
            if "projects" in cmd and "list" in cmd:
                return (0, "p-host\np-extra\n", "")
            return (0, "", "")

        errors: list[str] = []
        with patch.dict(os.environ, env, clear=False), patch.object(
            networking_audit, "run_cmd", side_effect=fake_run
        ):
            self.assertEqual(networking_audit.get_target_projects(None, errors), ["p-extra", "p-host"])
        self.assertEqual(len(errors), 1)
        self.assertIn("gcloud projects describe 123456789012", errors[0])
        self.assertIn("PERMISSION_DENIED", errors[0])
        self.assertNotIn("listing is filtered", errors[0])


FLEET_AUDIT_SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "fleet-audit", "scripts")
PLATFORM_SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts")
SUBNET_LINK = "https://www.googleapis.com/compute/v1/projects/p1/regions/us-central1/subnetworks/{}"
HOST_LINK = "https://www.googleapis.com/compute/v1/projects/host/regions/us-central1/subnetworks/{}"


def subnet(name, cidr, secondary=(), purpose="PRIVATE", project="p1"):
    return {
        "name": name,
        "region": f"https://www.googleapis.com/compute/v1/projects/{project}/regions/us-central1",
        "ipCidrRange": cidr,
        "secondaryIpRanges": [{"rangeName": r, "ipCidrRange": c} for r, c in secondary],
        "purpose": purpose,
        "selfLink": f"https://www.googleapis.com/compute/v1/projects/{project}/regions/us-central1/subnetworks/{name}",
    }


def gke_cluster(name, subnet_name, default_range, default_util, pools=(), subnet_project="p1"):
    return {
        "name": name,
        "location": "us-central1-a",
        "subnetwork": subnet_name,
        "ipAllocationPolicy": {
            "clusterSecondaryRangeName": default_range,
            "defaultPodIpv4RangeUtilization": default_util,
        },
        "nodePools": [
            {
                "name": pool,
                "podIpv4CidrSize": prefix,
                "networkConfig": {
                    "podRange": pod_range,
                    "podIpv4RangeUtilization": util,
                    "subnetwork": f"projects/{subnet_project}/regions/us-central1/subnetworks/{subnet_name}",
                },
            }
            for pool, pod_range, util, prefix in pools
        ],
    }


def reads(**results):
    """A run_gcloud_json fake answering each of the subnet sweep's reads by role."""
    roles = (
        ("subnets", "subnets"), ("clusters", "clusters"), ("instances", "instances"),
        ("addresses", "addresses"), ("forwarding-rules", "forwarding_rules"),
    )

    def fake(cmd, warnings=None):
        for word, role in roles:
            if word in cmd:
                result = results.get(role, [])
                return result if isinstance(result, tuple) else (result, None)
        raise AssertionError(f"unexpected command {cmd}")
    return fake


def fleet_reads(per_project):
    """A run_gcloud_json fake answering each project's reads from its own `reads` results."""
    fakes = {project: reads(**results) for project, results in per_project.items()}

    def fake(cmd, warnings=None):
        return fakes[networking_audit.project_flag_value(cmd)](cmd)
    return fake


def subnet_sweep(projects, fake):
    """Both subnet passes over `projects`, as main runs them."""
    skipped, active = [], []
    stderr = io.StringIO()
    with patch.object(networking_audit, "run_gcloud_json", side_effect=fake), patch("sys.stderr", stderr):
        usage = networking_audit.read_subnet_usage(projects)
        findings = networking_audit.audit_subnet_capacity(projects, usage, skipped, active)
    return findings, skipped, active, stderr.getvalue()


class SubnetCapacityTest(unittest.TestCase):
    def sweep(self, **results):
        return subnet_sweep(["p1"], reads(**results))

    def test_pod_range_at_90_percent_is_flagged_and_80_percent_is_not(self):
        findings, skipped, active, _ = self.sweep(
            subnets=[subnet("gke", "10.0.0.0/20", [("pods-a", "10.4.0.0/20"), ("pods-b", "10.8.0.0/20")])],
            clusters=[
                gke_cluster("c-a", "gke", "pods-a", 0.9, [("pool-a", "pods-a", 0.9, 24)]),
                gke_cluster("c-b", "gke", "pods-b", 0.8, [("pool-b", "pods-b", 0.8, 24)]),
            ],
        )
        self.assertEqual(skipped, [])
        self.assertEqual([f["object"] for f in findings], ["SecondaryRange/pods-a"])
        finding = findings[0]
        self.assertEqual(finding["check"], "subnet-ip-exhaustion")
        self.assertEqual(finding["severity"], "critical")
        self.assertEqual(finding["cluster"], "p1/us-central1/gke")
        self.assertEqual(finding["namespace"], "")
        self.assertEqual(finding["remediation"], {"kind": "manual"})
        self.assertEqual(
            finding["evidence"]["excerpt"],
            "Pod range pods-a (10.4.0.0/20): GKE reports 90.0% allocated (cluster c-a, node pool pool-a); "
            "about 1 more /24 node blocks fit",
        )
        self.assertEqual(
            finding["evidence"]["command"],
            "gcloud container clusters list --project=p1 "
            "'--format=json(name,location,subnetwork,networkConfig,ipAllocationPolicy,nodePools)'",
        )
        # 1 - 0.9 is 0.0999... in floating point; it reads as the 10% the excerpt implies.
        self.assertEqual(finding["title"], "Pod range pods-a of subnet gke in us-central1 has 10% available")
        self.assertEqual([e["name"] for e in active], ["p1/us-central1/gke"])

    def test_primary_counts_unique_addresses_plus_the_reserved_four(self):
        """A reserved address bound to a VM is one address; other subnets' addresses do not count."""
        own = SUBNET_LINK.format("small")
        findings, _, active, _ = self.sweep(
            subnets=[subnet("small", "10.0.0.0/29")],
            instances=[
                {"networkInterfaces": [{"networkIP": "10.0.0.2", "subnetwork": own}]},
                {"networkInterfaces": [{"networkIP": "10.0.0.3", "subnetwork": own}]},
                # Same subnet name and region in a Shared VPC host project.
                {"networkInterfaces": [{
                    "networkIP": "10.0.0.6",
                    "subnetwork": "https://www.googleapis.com/compute/v1/projects/host/regions/us-central1/subnetworks/small",
                }]},
            ],
            addresses=[{"address": "10.0.0.2", "subnetwork": own}],
            forwarding_rules=[
                {"IPAddress": "10.0.0.5", "subnetwork": own},
                {"IPAddress": "34.1.2.3"},
            ],
        )
        self.assertEqual([f["object"] for f in findings], ["Subnet/small"])
        self.assertEqual(
            findings[0]["evidence"]["excerpt"],
            "primary range 10.0.0.0/29: at least 7 of 8 addresses in use (12% available); counted from VM "
            "NICs, internal addresses and forwarding rules, so serverless connectors and Google-managed "
            "endpoints are not included",
        )
        self.assertEqual(findings[0]["title"], "Subnet small in us-central1 has 12% of its primary range available")
        command = findings[0]["evidence"]["command"]
        for read in ("subnets list", "instances list", "addresses list", "forwarding-rules list"):
            self.assertIn(read, command)
        self.assertNotIn("clusters list", command)
        self.assertNotIn("limitations", active[0])

    def test_roomy_primary_range_is_not_flagged(self):
        findings, _, active, _ = self.sweep(subnets=[subnet("big", "10.0.0.0/24")])
        self.assertEqual(findings, [])
        self.assertEqual(len(active), 1)

    def test_proxy_only_subnet_is_skipped(self):
        findings, skipped, active, stderr = self.sweep(
            subnets=[
                subnet("proxy", "10.9.0.0/30", purpose="REGIONAL_MANAGED_PROXY"),
                subnet("plain", "10.0.0.0/24", purpose=None),
            ],
        )
        self.assertEqual(findings, [])
        self.assertEqual(skipped, [])
        self.assertEqual([e["name"] for e in active], ["p1/us-central1/plain"])
        self.assertIn("proxy (REGIONAL_MANAGED_PROXY)", stderr)

    def test_clusters_list_failure_is_a_limitation_and_primary_is_still_measured(self):
        own = SUBNET_LINK.format("small")
        findings, skipped, active, _ = self.sweep(
            subnets=[subnet("small", "10.0.0.0/29", [("pods", "10.4.0.0/20")])],
            clusters=(None, "gcloud container clusters list failed (1): PERMISSION_DENIED"),
            instances=[{"networkInterfaces": [{"networkIP": f"10.0.0.{i}", "subnetwork": own}]} for i in (2, 3, 4)],
        )
        self.assertEqual(skipped, [])
        self.assertEqual([f["object"] for f in findings], ["Subnet/small"])
        self.assertIn("Pod ranges not read", active[0]["limitations"])
        self.assertIn("PERMISSION_DENIED", active[0]["limitations"])
        self.assertEqual(active[0]["checks_run"][0]["check"], "subnet-ip-exhaustion")

    def test_failed_primary_read_is_named_in_limitations(self):
        _, _, active, _ = self.sweep(
            subnets=[subnet("big", "10.0.0.0/24")],
            addresses=(None, "gcloud compute addresses list failed (1): PERMISSION_DENIED"),
        )
        self.assertIn("internal addresses not read", active[0]["limitations"])
        self.assertNotIn("Pod ranges", active[0]["limitations"])

    def test_subnets_list_failure_skips_the_project(self):
        findings, skipped, active, _ = self.sweep(subnets=(None, "subnets list failed (1): PERMISSION_DENIED"))
        self.assertEqual((findings, active), ([], []))
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["cluster"], "p1/UNENUMERATED_SUBNETS")
        self.assertEqual(skipped[0]["project"], "p1")
        self.assertIn("PERMISSION_DENIED", skipped[0]["reason"])

    @patch("networking_audit.run_cmd")
    def test_api_disabled_project_yields_nothing(self, mock_run_cmd):
        mock_run_cmd.return_value = (
            1, "", "ERROR: SERVICE_DISABLED: Compute Engine API has not been used in project p1",
        )
        skipped, active = [], []
        usage = networking_audit.read_subnet_usage(["p1"])
        findings = networking_audit.audit_subnet_capacity(["p1"], usage, skipped, active)
        self.assertEqual((findings, skipped, active), ([], [], []))

    def test_highest_utilization_wins_for_a_shared_range(self):
        findings, _, _, _ = self.sweep(
            subnets=[subnet("gke", "10.0.0.0/20", [("shared", "10.4.0.0/20")])],
            clusters=[
                gke_cluster("c-low", "gke", "shared", 0.5, [("pool-low", "shared", 0.5, 23)]),
                gke_cluster("c-high", "gke", "shared", 0.95, [("pool-high", "shared", 0.95, 24)]),
            ],
        )
        self.assertEqual(len(findings), 1)
        # The larger /23 block is kept, so the estimate errs low: 205 free addresses hold no /23.
        self.assertEqual(
            findings[0]["evidence"]["excerpt"],
            "Pod range shared (10.4.0.0/20): GKE reports 95.0% allocated (cluster c-high, node pool pool-high); "
            "about 0 more /23 node blocks fit",
        )

    def test_cluster_default_range_region_comes_from_a_zonal_location(self):
        cluster = gke_cluster("c", "default", "pods", 0.99)
        ranges = networking_audit.pod_range_utilization([cluster], "p1")
        self.assertEqual(
            ranges,
            {("p1", "us-central1", "default", "pods"): {
                "utilization": 0.99, "prefix": None, "cluster": "c", "pool": None, "project": "p1",
                "unreadable": [],
            }},
        )
        findings, _, _, _ = self.sweep(
            subnets=[{**subnet("default", "10.128.0.0/20", [("pods", "10.4.0.0/20")]),
                      "selfLink": SUBNET_LINK.format("default")}],
            clusters=[cluster],
        )
        # No node pool, so neither the pool nor the block estimate is known.
        self.assertEqual(
            findings[0]["evidence"]["excerpt"],
            "Pod range pods (10.4.0.0/20): GKE reports 99.0% allocated (cluster c)",
        )

    def test_unreadable_utilization_is_a_limitation_not_a_clean_range(self):
        subnets = [subnet("gke", "10.0.16.0/20", [("pods", "10.4.0.0/20")])]
        for raw in (float("nan"), float("inf"), 1.5, -0.2, "nan"):
            with self.subTest(raw=raw):
                findings, _, active, _ = self.sweep(
                    subnets=subnets, clusters=[gke_cluster("c", "gke", "pods", raw)])
                self.assertEqual(findings, [])
                self.assertIn("Pod range pods: GKE reported utilization", active[0]["limitations"])
                self.assertIn("not a fraction in 0-1", active[0]["limitations"])

    def test_unreadable_report_does_not_mask_a_readable_one(self):
        cluster = gke_cluster("c", "gke", "pods", float("nan"), [("pool", "pods", 0.9, 24)])
        findings, _, active, _ = self.sweep(
            subnets=[subnet("gke", "10.0.16.0/20", [("pods", "10.4.0.0/20")])], clusters=[cluster])
        self.assertEqual([f["object"] for f in findings], ["SecondaryRange/pods"])
        self.assertIn("90.0% allocated (cluster c, node pool pool)", findings[0]["evidence"]["excerpt"])
        self.assertIn("GKE reported utilization nan (cluster c)", active[0]["limitations"])

    def test_unparsable_subnet_range_is_uncounted_not_measured(self):
        findings, _, active, stderr = self.sweep(subnets=[subnet("bad", "not-a-cidr"), subnet("ok", "10.0.0.0/24")])
        self.assertEqual([e["name"] for e in active], ["p1/us-central1/ok"])
        self.assertEqual(findings, [])
        self.assertIn("bad (unparsable range 'not-a-cidr')", stderr)

    def test_additional_pod_ranges_are_read(self):
        cluster = gke_cluster("c", "gke", "pods", 0.1, [("pool", "pods", 0.1, 24)])
        cluster["ipAllocationPolicy"]["additionalPodRangesConfig"] = {
            "podRangeInfo": [{"rangeName": "extra", "utilization": 0.9}],
        }
        ranges = networking_audit.pod_range_utilization([cluster], "p1")
        self.assertEqual(ranges[("p1", "us-central1", "gke", "extra")]["utilization"], 0.9)
        self.assertEqual(ranges[("p1", "us-central1", "gke", "pods")]["pool"], "pool")

    def test_node_blocks_that_fit(self):
        # 0.29% of a /14 is three /24 node blocks; 262144 * 0.9971 = 261384 free, 1021 whole /24s.
        self.assertEqual(networking_audit.node_blocks_that_fit("10.0.0.0/14", 0.0029, 24), 1021)
        self.assertEqual(networking_audit.node_blocks_that_fit("10.0.0.0/24", 1.0, 24), 0)
        self.assertEqual(networking_audit.node_blocks_that_fit("10.0.0.0/20", 0.5, 22), 2)
        self.assertIsNone(networking_audit.node_blocks_that_fit("10.0.0.0/20", 0.5, None))

    def test_block_prefix_outside_ipv4_is_unreadable_not_fatal(self):
        for value in (33, -1, "40"):
            with self.subTest(value=value):
                self.assertIsNone(networking_audit._prefix(value))
        self.assertEqual(networking_audit._prefix("24"), 24)
        # A pool reporting a nonsense block size drops the estimate, not the run.
        cluster = gke_cluster("c1", "gke", "pods", 0.95, [("pool", "pods", 0.95, 99)])
        ranges = networking_audit.pod_range_utilization([cluster], "p1")
        pod = ranges[("p1", "us-central1", "gke", "pods")]
        self.assertIsNone(networking_audit.node_blocks_that_fit("10.0.0.0/20", pod["utilization"], pod.get("prefix")))

    def test_scope_entry_shape(self):
        _, _, active, _ = self.sweep(subnets=[subnet("big", "10.0.0.0/24")])
        entry = active[0]
        self.assertEqual(entry["location"], "us-central1")
        self.assertEqual(entry["project"], "p1")
        self.assertEqual(
            [na["check"] for na in entry["checks_not_applicable"]],
            ["cloud-nat-exhaustion", "psc-routing-deadlock", "mtu-packet-fragmentation", "cloud-armor-false-positive"],
        )
        command = entry["checks_run"][0]["command"]
        self.assertEqual(command.count(" && "), 4)
        self.assertTrue(command.startswith("gcloud compute networks subnets list --project=p1 "))

    def test_percent_available_rounds_float_noise_before_flooring(self):
        self.assertEqual(networking_audit.percent_available(1 - 0.9), 10)
        self.assertEqual(networking_audit.percent_available(1 - 0.86), 14)
        self.assertEqual(networking_audit.percent_available(0.149), 14)

    def test_partial_clusters_listing_is_a_limitation(self):
        """gcloud exits 0 when zones time out; the clusters it did return still count."""
        clusters = [gke_cluster("c", "gke", "pods", 0.95, [("pool", "pods", 0.95, 24)])]
        warning = (
            "WARNING: The following zones did not respond: us-east1-b. List results may be incomplete.\n"
        )

        def fake_run(cmd, *args, **kwargs):
            if "subnets" in cmd:
                return (0, json.dumps([subnet("gke", "10.0.0.0/20", [("pods", "10.4.0.0/20")])]), "")
            if "clusters" in cmd:
                return (0, json.dumps(clusters), warning)
            return (0, "[]", "")

        skipped, active = [], []
        with patch.object(networking_audit, "run_cmd", side_effect=fake_run):
            usage = networking_audit.read_subnet_usage(["p1"])
            findings = networking_audit.audit_subnet_capacity(["p1"], usage, skipped, active)
        self.assertEqual([f["object"] for f in findings], ["SecondaryRange/pods"])
        self.assertIn("Pod ranges partially read: `gcloud container clusters list --project=p1", active[0]["limitations"])
        self.assertIn("did not respond: us-east1-b", active[0]["limitations"])

    def test_clean_clusters_listing_has_no_limitation(self):
        def fake_run(cmd, *args, **kwargs):
            if "subnets" in cmd:
                return (0, json.dumps([subnet("gke", "10.0.0.0/20")]), "")
            return (0, "[]", "WARNING: some unrelated notice" if "clusters" in cmd else "")

        skipped, active = [], []
        with patch.object(networking_audit, "run_cmd", side_effect=fake_run):
            usage = networking_audit.read_subnet_usage(["p1"])
            networking_audit.audit_subnet_capacity(["p1"], usage, skipped, active)
        self.assertNotIn("limitations", active[0])


def project_entry(project):
    """The `project/<id>` entry SOP 2.1's merge sits beside, carrying the other four checks."""
    return {
        "name": f"project/{project}",
        "location": "global",
        "project": project,
        "checks_run": [
            {"check": "cloud-nat-exhaustion", "command": f"gcloud compute routers list --project={project} --format=json"},
            {"check": "psc-routing-deadlock", "command": f"gcloud compute forwarding-rules list --project {project} --format=json"},
            {"check": "mtu-packet-fragmentation", "command": f"gcloud compute networks list --project={project} --format=json"},
            {"check": "cloud-armor-false-positive", "command": f"gcloud compute security-policies list --project={project} --format=json"},
        ],
        "checks_not_applicable": [{
            "check": "subnet-ip-exhaustion",
            "reason": "Subnet IP capacity is audited per individual subnet scope entry.",
        }],
    }


def validate(test, findings, skipped, active, projects):
    """Validates a document holding the sweep's output, merged as SOP 2.1 says."""
    sys.path.insert(0, FLEET_AUDIT_SCRIPTS)
    test.addCleanup(sys.path.remove, FLEET_AUDIT_SCRIPTS)
    import audit_report

    doc = {
        "audit": "gcp-networking-fabric-audit",
        "scope": {"clusters": [*active, *(project_entry(p) for p in projects)], "skipped": skipped},
        "findings": findings,
    }
    audit_report.validate_findings(json.loads(json.dumps(doc)), "gcp-networking-fabric-audit")


class SharedVpcTest(unittest.TestCase):
    """Service-project clusters and VMs on a host project's subnet."""

    HOST_SUBNETS = [subnet("shared", "10.0.0.0/29", [("pods", "10.4.0.0/20")], project="host")]

    def service_reads(self, util=0.95, **extra):
        return {
            "subnets": [],
            "clusters": [gke_cluster("svc-c", "shared", "pods", util, [("svc-pool", "pods", util, 24)],
                                     subnet_project="host")],
            "instances": [{"networkInterfaces": [{"networkIP": f"10.0.0.{i}", "subnetwork": HOST_LINK.format("shared")}]}
                          for i in (2, 3, 4)],
            **extra,
        }

    def test_host_subnet_counts_service_project_clusters_and_nodes(self):
        findings, skipped, active, _ = subnet_sweep(
            ["host", "svc"], fleet_reads({"host": {"subnets": self.HOST_SUBNETS}, "svc": self.service_reads()})
        )
        self.assertEqual(skipped, [])
        self.assertEqual([e["name"] for e in active], ["host/us-central1/shared"])
        self.assertNotIn("limitations", active[0])
        by_object = {f["object"]: f for f in findings}
        self.assertEqual(sorted(by_object), ["SecondaryRange/pods", "Subnet/shared"])
        pod = by_object["SecondaryRange/pods"]
        self.assertEqual(pod["cluster"], "host/us-central1/shared")
        self.assertIn("(10.4.0.0/20): GKE reports 95.0% allocated (cluster svc-c, node pool svc-pool)",
                      pod["evidence"]["excerpt"])
        self.assertTrue(pod["evidence"]["command"].startswith("gcloud container clusters list --project=svc "))
        primary = by_object["Subnet/shared"]
        self.assertIn("at least 7 of 8 addresses in use", primary["evidence"]["excerpt"])
        command = primary["evidence"]["command"]
        self.assertTrue(command.startswith("gcloud compute networks subnets list --project=host "))
        self.assertIn("gcloud compute instances list --project=svc ", command)
        validate(self, findings, skipped, active, ["host", "svc"])

    def test_failed_service_project_read_is_a_skipped_row_not_a_host_limitation(self):
        findings, skipped, active, _ = subnet_sweep(
            ["host", "svc"],
            fleet_reads({
                "host": {"subnets": self.HOST_SUBNETS},
                "svc": {**self.service_reads(), "clusters": (None, "clusters list failed (1): PERMISSION_DENIED")},
            }),
        )
        self.assertEqual([f["object"] for f in findings], ["Subnet/shared"])
        # The host's own reads succeeded, so its subnet carries no limitation; the
        # service project owns no subnet entry, so its failure is a skipped row.
        self.assertNotIn("limitations", active[0])
        self.assertEqual([t["cluster"] for t in skipped], ["svc/UNREAD_SUBNET_USAGE"])
        self.assertIn("Pod ranges not read: `gcloud container clusters list --project=svc", skipped[0]["reason"])
        self.assertIn("PERMISSION_DENIED", skipped[0]["reason"])
        validate(self, findings, skipped, active, ["host", "svc"])

    def test_failed_read_marks_only_its_own_projects_subnets(self):
        # A host bound with compute.viewer alone: its clusters read is refused,
        # its compute reads succeed. Only its own subnet carries the limitation.
        findings, skipped, active, _ = subnet_sweep(
            ["host", "svc"],
            fleet_reads({
                "host": {"subnets": self.HOST_SUBNETS,
                         "clusters": (None, "clusters list failed (1): PERMISSION_DENIED")},
                "svc": {**self.service_reads(), "subnets": [subnet("own", "10.8.0.0/24", project="svc")]},
            }),
        )
        by_name = {e["name"]: e for e in active}
        self.assertIn("Pod ranges not read: `gcloud container clusters list --project=host",
                      by_name["host/us-central1/shared"]["limitations"])
        self.assertNotIn("limitations", by_name["svc/us-central1/own"])
        self.assertEqual(skipped, [])
        validate(self, findings, skipped, active, ["host", "svc"])

    def test_service_project_failure_is_recorded_under_check_all(self):
        """Under the default `--check all` the PSC sweep's project entries must not hide it."""
        fake_subnet_reads = fleet_reads({
            "host": {"subnets": self.HOST_SUBNETS},
            "svc": {**self.service_reads(), "clusters": (None, "clusters list failed (1): PERMISSION_DENIED")},
        })

        def fake(cmd, warnings=None):
            if cmd[-1] == "--format=json":  # the PSC sweep's forwarding-rules read
                return ([], None)
            return fake_subnet_reads(cmd, warnings)

        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "out.json")
            argv = ["networking_audit.py", "--output", output]
            with patch.object(networking_audit, "get_target_projects", return_value=["host", "svc"]), \
                    patch.object(networking_audit, "run_gcloud_json", side_effect=fake), \
                    patch.object(sys, "argv", argv), \
                    patch("sys.stdout", new_callable=io.StringIO), \
                    patch("sys.stderr", new_callable=io.StringIO):
                networking_audit.main()
            with open(output, encoding="utf-8") as f:
                doc = json.load(f)
        self.assertIn("project/svc", [e["name"] for e in doc["scope"]["clusters"]])
        self.assertEqual([t["cluster"] for t in doc["scope"]["skipped"]], ["svc/UNREAD_SUBNET_USAGE"])
        self.assertIn("PERMISSION_DENIED", doc["scope"]["skipped"][0]["reason"])

    def test_pod_range_on_out_of_scope_host_subnet_keeps_its_finding(self):
        findings, skipped, active, _ = subnet_sweep(["svc"], fleet_reads({"svc": self.service_reads()}))
        self.assertEqual(skipped, [])
        self.assertEqual([e["name"] for e in active], ["host/us-central1/shared"])
        entry = active[0]
        self.assertEqual((entry["project"], entry["location"]), ("host", "us-central1"))
        self.assertIn("subnet shared was not listed because host is outside this run's scope", entry["limitations"])
        self.assertIn("only the Pod ranges GKE reports on it were measured", entry["limitations"])
        self.assertTrue(entry["checks_run"][0]["command"].startswith("gcloud container clusters list --project=svc "))
        # Only the Pod range: the VMs on the unlisted subnet are not measured against a range nobody read.
        self.assertEqual([f["object"] for f in findings], ["SecondaryRange/pods"])
        self.assertEqual(findings[0]["cluster"], "host/us-central1/shared")
        self.assertEqual(
            findings[0]["evidence"]["excerpt"],
            "Pod range pods: GKE reports 95.0% allocated (cluster svc-c, node pool svc-pool)",
        )
        validate(self, findings, skipped, active, ["svc"])

    def test_quiet_pod_range_on_out_of_scope_host_subnet_is_still_recorded(self):
        findings, skipped, active, _ = subnet_sweep(["svc"], fleet_reads({"svc": self.service_reads(util=0.5)}))
        self.assertEqual(findings, [])
        self.assertEqual([e["name"] for e in active], ["host/us-central1/shared"])
        self.assertIn("host is outside this run's scope", active[0]["limitations"])
        validate(self, findings, skipped, active, ["svc"])

    def test_pod_range_on_unlistable_host_subnet_keeps_its_finding(self):
        findings, skipped, active, _ = subnet_sweep(
            ["host", "svc"],
            fleet_reads({
                "host": {"subnets": (None, "subnets list failed (1): PERMISSION_DENIED")},
                "svc": self.service_reads(),
            }),
        )
        self.assertEqual([t["cluster"] for t in skipped], ["host/UNENUMERATED_SUBNETS"])
        self.assertEqual([e["name"] for e in active], ["host/us-central1/shared"])
        self.assertIn("the subnet listing of host failed (see host/UNENUMERATED_SUBNETS)", active[0]["limitations"])
        self.assertEqual([f["object"] for f in findings], ["SecondaryRange/pods"])
        validate(self, findings, skipped, active, ["host", "svc"])


class SubnetCommandsTest(unittest.TestCase):
    def test_instances_read_is_projected_to_the_fields_counted(self):
        self.assertEqual(
            networking_audit.subnet_commands("p1")["instances"],
            ["gcloud", "compute", "instances", "list", "--project=p1",
             "--format=json(networkInterfaces[].networkIP,networkInterfaces[].subnetwork)"],
        )

    def test_reads_pass_command_policy_and_fit_checks_run(self):
        sys.path.insert(0, PLATFORM_SCRIPTS)
        sys.path.insert(0, FLEET_AUDIT_SCRIPTS)
        self.addCleanup(sys.path.remove, PLATFORM_SCRIPTS)
        self.addCleanup(sys.path.remove, FLEET_AUDIT_SCRIPTS)
        import audit_report
        import command_policy

        # The longest project id GCP allows.
        cmds = networking_audit.subnet_commands("p" * 30)
        for role, argv in cmds.items():
            decision = command_policy.evaluate(argv)
            self.assertTrue(decision.allowed, f"{role}: {decision}")
        self.assertLessEqual(len(networking_audit.command_text(*cmds.values())), audit_report.MAX_COMMAND_CHARS)


class RunCmdTest(unittest.TestCase):
    def test_timeout_is_a_failed_read(self):
        with patch("subprocess.run", side_effect=networking_audit.subprocess.TimeoutExpired(["gcloud"], 60)):
            rc, stdout, stderr = networking_audit.run_cmd(["gcloud", "compute", "instances", "list"])
        self.assertEqual((rc, stdout), (-1, ""))
        self.assertIn("timed out after 300 seconds", stderr)


class CheckRoutingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.output = os.path.join(self._tmp.name, "out.json")

    def run_main(self, *extra):
        argv = ["networking_audit.py", "--output", self.output, *extra]
        with patch.object(networking_audit, "get_target_projects", return_value=["p1"]), \
                patch.object(networking_audit, "audit_project_networking", return_value=[]) as psc, \
                patch.object(networking_audit, "read_subnet_usage", return_value={}), \
                patch.object(networking_audit, "audit_subnet_capacity", return_value=[]) as subnets, \
                patch.object(sys, "argv", argv), \
                patch("sys.stdout", new_callable=io.StringIO):
            networking_audit.main()
        return psc.call_count, subnets.call_count

    def test_check_flag_picks_the_sweep(self):
        self.assertEqual(self.run_main(), (1, 1))
        self.assertEqual(self.run_main("--check", "all"), (1, 1))
        self.assertEqual(self.run_main("--check", "subnet-ip-exhaustion"), (0, 1))
        self.assertEqual(self.run_main("--check", "psc-routing-deadlock"), (1, 0))

    def test_document_passes_audit_report_validation(self):
        """The helper's subnet entries and findings, merged as SOP 2.1 says, validate."""
        small = SUBNET_LINK.format("small")
        fake = reads(
            subnets=[
                subnet("small", "10.0.0.0/29"),
                subnet("gke", "10.0.16.0/20", [("pods", "10.4.0.0/20"), ("services", "10.8.0.0/24")]),
            ],
            clusters=[gke_cluster("c", "gke", "pods", 0.97, [("pool", "pods", 0.97, 24)])],
            instances=[{"networkInterfaces": [{"networkIP": f"10.0.0.{i}", "subnetwork": small}]} for i in (2, 3, 4)],
            addresses=(None, "gcloud compute addresses list failed (1): PERMISSION_DENIED"),
        )
        argv = ["networking_audit.py", "--check", "subnet-ip-exhaustion", "--output", self.output]
        with patch.object(networking_audit, "get_target_projects", return_value=["p1"]), \
                patch.object(networking_audit, "run_gcloud_json", side_effect=fake), \
                patch.object(sys, "argv", argv), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO):
            networking_audit.main()
        with open(self.output, encoding="utf-8") as f:
            doc = json.load(f)

        self.assertEqual(
            sorted(f["object"] for f in doc["findings"]), ["SecondaryRange/pods", "Subnet/small"]
        )
        validate(self, doc["findings"], doc["scope"]["skipped"], doc["scope"]["clusters"], ["p1"])


if __name__ == "__main__":
    unittest.main()
