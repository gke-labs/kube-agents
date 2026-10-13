#!/usr/bin/env python3
"""Unit tests for networking_audit.py."""

import hashlib
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

    def test_unreadable_project_target_is_gate_failed_not_fatal(self):
        def run(cmd, *args, **kwargs):
            return (1, "", "ERROR: PERMISSION_DENIED compute.routers.list")
        with patch("networking_audit.run_cmd", side_effect=run), patch("sys.stderr", new_callable=io.StringIO):
            entry = networking_audit.collect_project_target("denied-proj")
        self.assertEqual(entry["name"], "project/denied-proj")
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("PERMISSION_DENIED", entry["error"])

    def test_api_disabled_project_contributes_no_target(self):
        def run(cmd, *args, **kwargs):
            return (
                1,
                "",
                "ERROR: (gcloud.compute.routers.list) SERVICE_DISABLED: Compute Engine API has not been used in project no-compute-proj",
            )
        with patch("networking_audit.run_cmd", side_effect=run), patch("sys.stderr", new_callable=io.StringIO):
            self.assertIsNone(networking_audit.collect_project_target("no-compute-proj"))

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
        with patch("networking_audit.run_cmd", side_effect=self._refusal_run("222222222222")), \
                patch("sys.stderr", new_callable=io.StringIO):
            entry = networking_audit.collect_project_target("real-proj")
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("the API is off in a project other than 'real-proj', such as a quota project", entry["error"])

        def describe_failed(cmd, *args, **kwargs):
            if cmd[:3] == ["gcloud", "projects", "describe"]:
                return (1, "", "PERMISSION_DENIED: resourcemanager.projects.get")
            return self._refusal_run("222222222222")(cmd, *args, **kwargs)

        networking_audit.PROJECT_NUMBERS.clear()  # a second run
        with patch("networking_audit.run_cmd", side_effect=describe_failed), \
                patch("sys.stderr", new_callable=io.StringIO):
            entry = networking_audit.collect_project_target("real-proj")
        self.assertIn(
            "`gcloud projects describe real-proj` failed (rc=1), so the refusal's project number could not be compared",
            entry["error"],
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
        with patch("networking_audit.run_cmd", side_effect=self._refusal_run("111111111111")), \
                patch("sys.stderr", new_callable=io.StringIO):
            self.assertIsNone(networking_audit.collect_project_target("real-proj"))


class Unstubbed(BaseException):
    """A call no fake modelled. A `BaseException`, so no `except Exception` in
    the collector can turn it into an ordinary gate-failed row: the test errors
    naming the command instead."""


def project_answers(**overrides):
    """JSON answers for every read the collector makes against one project, by
    the phrase that picks the read out of its argv. Every list read answers
    `[]` and `get-status` answers `{}` unless a test overrides it."""
    answers = {
        "get-nat-mapping-info": [],
        "get-status": {},
        "routers list": [],
        "subnets list": [],
        "clusters list": [],
        "instances list": [],
        "addresses list": [],
        "forwarding-rules list": [],
        "networks list": [],
        "security-policies list": [],
        "backend-services list": [],
        "firewall-rules list": [],
    }
    answers.update(overrides)
    return answers


def fake_run_cmd(per_project, calls=None):
    """A `run_cmd` answering each project's reads from `project_answers`. An
    answer that is a `(rc, stderr)` tuple is a failed read."""

    def run(cmd, *args, **kwargs):
        if calls is not None:
            calls.append(list(cmd))
        joined = " ".join(cmd)
        answers = per_project.get(networking_audit.project_flag_value(cmd) or "", {})
        for phrase, answer in answers.items():
            if phrase in joined:
                if isinstance(answer, tuple):
                    return (answer[0], "", answer[1])
                return (0, json.dumps(answer), "")
        raise Unstubbed(f"unstubbed command: {joined}")

    return run


class MainSweepTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        networking_audit.PROJECT_NUMBERS.clear()
        self.addCleanup(networking_audit.PROJECT_NUMBERS.clear)

    def manifest(self, projects, per_project, *extra):
        output = os.path.join(self.tmpdir, "manifest.json")
        with patch.object(networking_audit, "get_target_projects", return_value=projects), \
                patch.object(networking_audit, "run_cmd", side_effect=fake_run_cmd(per_project)), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO):
            rc = networking_audit.main(["--output", output, *extra])
        with open(output, encoding="utf-8") as f:
            return rc, json.load(f)

    def test_one_denied_project_does_not_abort_the_rest(self):
        """The whole point of the sweep: a 403 on one project still audits the others."""
        rejected_rule = [{
            "name": "psc-ep-1",
            "region": "projects/readable/regions/us-central1",
            "target": "projects/readable/regions/us-central1/serviceAttachments/sa-1",
            "pscConnectionStatus": "REJECTED",
        }]
        denied = {phrase: (1, "PERMISSION_DENIED") for phrase in project_answers()}
        rc, manifest = self.manifest(
            ["denied", "readable"],
            {"denied": denied, "readable": project_answers(**{"forwarding-rules list": rejected_rule})},
        )
        self.assertEqual(rc, 0)
        self.assertEqual(manifest["audit"], "gcp-networking-fabric-audit")
        by_name = {entry["name"]: entry for entry in manifest["clusters"]}
        self.assertEqual(by_name["project/denied"]["outcome"], "gate-failed")
        self.assertEqual(by_name["denied/UNENUMERATED_SUBNETS"]["outcome"], "gate-failed")
        readable = by_name["project/readable"]
        self.assertEqual(readable["outcome"], "collected")
        self.assertEqual([c["object"] for c in readable["candidates"]], ["ForwardingRule/us-central1/psc-ep-1"])

    def test_no_projects_resolved_records_unknown_and_exits_non_zero(self):
        rc, manifest = self.manifest([], {})
        self.assertEqual(rc, 1)
        self.assertEqual([entry["name"] for entry in manifest["clusters"]], ["project/unknown"])
        self.assertIn("no target could be read", manifest["error"])

    def test_failed_projects_list_is_one_unenumerated_row_with_its_tail(self):
        """A listing failure must make the run partial, not read as the whole fleet."""
        env = {
            networking_audit.MONITORED_PROJECTS_ENV: "",
            "GCP_PROJECT_ID": "p-host",
            "GKE_PROJECT_ID": "",
            "PROJECT_ID": "",
        }
        answers = fake_run_cmd({"p-host": project_answers()})

        def run(cmd, *args, **kwargs):
            if "projects" in cmd and "list" in cmd:
                return (1, "", "ERROR: cloudresourcemanager.googleapis.com is not reachable " + "x" * 400)
            if "config" in cmd:
                return (0, "", "")
            return answers(cmd)

        output = os.path.join(self.tmpdir, "narrowed.json")
        with patch.dict(os.environ, env, clear=False), \
                patch.object(networking_audit, "run_cmd", side_effect=run), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO):
            networking_audit.main(["--output", output])
        with open(output, encoding="utf-8") as f:
            manifest = json.load(f)
        rows = [e for e in manifest["clusters"] if e["name"] == "project/UNENUMERATED_PROJECTS"]
        self.assertEqual(len(rows), 1)
        self.assertIn("gcloud projects list", rows[0]["error"])
        self.assertTrue(rows[0]["error"].endswith(networking_audit.UNENUMERATED_TAIL))
        self.assertLessEqual(len(rows[0]["error"]), networking_audit.ERROR_EXCERPT_CHARS)
        self.assertIn("project/p-host", [e["name"] for e in manifest["clusters"]])

    def test_killed_run_leaves_no_stale_manifest(self):
        out = os.path.join(self.tmpdir, "manifest.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"audit": "gcp-networking-fabric-audit", "clusters": ["yesterday"]}, f)
        with patch.object(networking_audit, "get_target_projects", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                networking_audit.main(["--output", out])
        self.assertFalse(os.path.exists(out))

    def test_without_output_the_manifest_goes_to_stdout(self):
        stdout = io.StringIO()
        with patch.object(networking_audit, "get_target_projects", return_value=["p1"]), \
                patch.object(networking_audit, "run_cmd", side_effect=fake_run_cmd({"p1": project_answers()})), \
                patch("sys.stdout", stdout), patch("sys.stderr", new_callable=io.StringIO):
            rc = networking_audit.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(stdout.getvalue())["audit"], "gcp-networking-fabric-audit")


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
            out = os.path.join(tmpdir, "manifest.json")
            with patch.object(networking_audit, "run_cmd", return_value=(0, "", "")), \
                    patch("sys.stdout", new_callable=io.StringIO), \
                    patch("sys.stderr", new_callable=io.StringIO):
                networking_audit.main(["--project-id", "cli-proj", "--output", out])
            with open(out, encoding="utf-8") as f:
                manifest = json.load(f)
        self.assertIn("project/UNENUMERATED_PROJECTS", [t["name"] for t in manifest["clusters"]])

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
            "'--format=json(name,location,subnetwork,networkConfig,ipAllocationPolicy,nodePools,autopilot)'",
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
        # A subnet target owes `subnet-ip-exhaustion` alone (`AuditSpec.scopes`),
        # so it declares nothing inapplicable.
        self.assertNotIn("checks_not_applicable", entry)
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
        """Under the default `--check all` the project targets must not hide it."""
        fake_subnet_reads = fleet_reads({
            "host": {"subnets": self.HOST_SUBNETS},
            "svc": {**self.service_reads(), "clusters": (None, "clusters list failed (1): PERMISSION_DENIED")},
        })
        with patch.object(networking_audit, "get_target_projects", return_value=["host", "svc"]), \
                patch.object(networking_audit, "run_gcloud_json", side_effect=fake_subnet_reads), \
                patch.object(networking_audit, "collect_project_target",
                             side_effect=lambda p, *_: {"name": f"project/{p}", "project": p, "location": "global",
                                                    "outcome": "collected", "commands": [], "candidates": [],
                                                    "checks_not_applicable": []}), \
                patch("sys.stderr", new_callable=io.StringIO):
            manifest = networking_audit.collect_fleet()
        by_name = {e["name"]: e for e in manifest["clusters"]}
        self.assertIn("project/svc", by_name)
        self.assertEqual(by_name["svc/UNREAD_SUBNET_USAGE"]["outcome"], "gate-failed")
        self.assertIn("PERMISSION_DENIED", by_name["svc/UNREAD_SUBNET_USAGE"]["error"])

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
    def test_instances_read_is_the_project_check_s_read(self):
        # One read serves the subnet sweep and the firewall check.
        self.assertEqual(
            networking_audit.subnet_commands("p1")["instances"],
            networking_audit.project_commands("p1")["instances"],
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
        self.output = os.path.join(self._tmp.name, "manifest.json")

    def run_main(self, *extra):
        def no_gcloud(cmd, *args, **kwargs):
            raise AssertionError(f"the test ran gcloud: {cmd}")

        with patch.object(networking_audit, "get_target_projects", return_value=["p1"]), \
                patch.object(networking_audit, "run_cmd", side_effect=no_gcloud), \
                patch.object(networking_audit, "read_subnet_usage", return_value={}), \
                patch.object(networking_audit, "collect_project_target", return_value=None) as project, \
                patch.object(networking_audit, "subnet_targets", return_value=[]) as subnets, \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO):
            networking_audit.main(["--output", self.output, *extra])
        return project.call_count, subnets.call_count

    def test_check_flag_picks_the_sweep(self):
        self.assertEqual(self.run_main(), (1, 1))
        self.assertEqual(self.run_main("--check", "all"), (1, 1))
        self.assertEqual(self.run_main("--check", "subnet-ip-exhaustion"), (0, 1))
        # The value PSC had when it was the only project-level check here.
        self.assertEqual(self.run_main("--check", "psc-routing-deadlock"), (1, 0))

    def test_subnet_manifest_carries_the_sweep_s_entries_and_findings(self):
        """#2468's sweep, unchanged, read through the manifest: one collected
        target per subnet carrying its findings as candidates, a skipped row as
        a gate-failed target."""
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
        with patch.object(networking_audit, "run_gcloud_json", side_effect=fake), \
                patch("sys.stderr", new_callable=io.StringIO):
            entries = networking_audit.subnet_targets(["p1"])
        by_name = {entry["name"]: entry for entry in entries}
        self.assertEqual(sorted(by_name), ["p1/us-central1/gke", "p1/us-central1/small"])
        for entry in entries:
            self.assertEqual(entry["outcome"], "collected")
            self.assertEqual(entry["checks_not_applicable"], [])
            self.assertEqual([c["check"] for c in entry["commands"]], ["subnet-ip-exhaustion"])
            self.assertEqual(entry["commands"][0]["rc"], 0)
            self.assertIn("internal addresses not read", entry["limitations"])
        self.assertEqual([c["object"] for c in by_name["p1/us-central1/small"]["candidates"]], ["Subnet/small"])
        pod = by_name["p1/us-central1/gke"]["candidates"][0]
        self.assertEqual(pod["object"], "SecondaryRange/pods")
        self.assertEqual(pod["severity"], "critical")
        self.assertIn("GKE reports 97.0% allocated", pod["excerpt"])
        self.assertTrue(pod["command"].startswith("gcloud container clusters list --project=p1 "))
        self.assertNotIn("recommendation", pod)

    def test_a_skipped_subnet_row_is_a_gate_failed_target(self):
        fake = reads(subnets=(None, "subnets list failed (1): PERMISSION_DENIED"))
        with patch.object(networking_audit, "run_gcloud_json", side_effect=fake), \
                patch("sys.stderr", new_callable=io.StringIO):
            entries = networking_audit.subnet_targets(["p1"])
        self.assertEqual([(e["name"], e["outcome"]) for e in entries], [("p1/UNENUMERATED_SUBNETS", "gate-failed")])
        self.assertIn("PERMISSION_DENIED", entries[0]["error"])


na = networking_audit


class RouterNatTest(unittest.TestCase):
    def router(self, **overrides):
        base = {
            "name": "nat-router",
            "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-central1",
            "nats": [{"name": "nat-gw", "natIpAllocateOption": "AUTO_ONLY", "enableDynamicPortAllocation": True, "maxPortsPerVm": 4096}],
        }
        base.update(overrides)
        return base

    def test_flags_missing_auto_allocated_ip(self):
        status = {"result": {"natStatus": [{"name": "nat-gw", "autoAllocatedNatIps": []}]}}
        hit = na.check_router_nat(self.router(), status, {})
        self.assertEqual(hit["object"], "Router/us-central1/nat-router")
        self.assertIn("no auto-allocated external IP", hit["excerpt"])

    def test_two_same_named_routers_in_two_regions_are_two_objects(self):
        """Both land in the one `project/<p>` target, so a bare name would give
        them one finding identity and `finish` would refuse the document."""
        status = {"result": {"natStatus": [{"name": "nat-gw", "autoAllocatedNatIps": []}]}}
        east = self.router(region="https://www.googleapis.com/compute/v1/projects/p/regions/us-east4")
        objects = {
            na.check_router_nat(self.router(), status, {})["object"],
            na.check_router_nat(east, status, {})["object"],
        }
        self.assertEqual(objects, {"Router/us-central1/nat-router", "Router/us-east4/nat-router"})

    def test_does_not_flag_healthy_auto_allocation(self):
        status = {"result": {"natStatus": [{"name": "nat-gw", "autoAllocatedNatIps": ["34.1.2.3"]}]}}
        mappings = {"nat-gw": [{"instanceName": "vm-1", "interfaceNatMappings": [{"numTotalNatPorts": 512}]}]}
        hit = na.check_router_nat(self.router(), status, mappings)
        self.assertIsNone(hit)

    def test_flags_port_ceiling_at_80_percent(self):
        status = {"result": {"natStatus": [{"name": "nat-gw", "autoAllocatedNatIps": ["34.1.2.3"]}]}}
        mappings = {"nat-gw": [{"instanceName": "vm-1", "interfaceNatMappings": [{"numTotalNatPorts": 3277}]}]}  # 80.0% of 4096
        hit = na.check_router_nat(self.router(), status, mappings)
        self.assertIn("vm-1", hit["excerpt"])
        self.assertIn("3277/4096", hit["excerpt"])

    def test_static_allocation_is_never_measured_against_its_own_reservation(self):
        """With dynamic port allocation off, Cloud NAT hands every VM exactly
        `minPortsPerVm` ports, so `numTotalNatPorts` is the ceiling and the
        ratio is the constant 1.0. Measuring it flagged `critical` port
        exhaustion for every VM behind every stock gateway, every day."""
        router = self.router(nats=[{"name": "nat-gw", "natIpAllocateOption": "MANUAL_ONLY", "minPortsPerVm": 64, "natIps": ["34.1.2.3"]}])
        mappings = {"nat-gw": [{"instanceName": "vm-1", "interfaceNatMappings": [{"numTotalNatPorts": 64}]}]}
        self.assertIsNone(na.check_router_nat(router, None, mappings))

        # Same gateway on GCP's stock ceiling, which `routers list` omits.
        stock = self.router(nats=[{"name": "nat-gw", "natIpAllocateOption": "MANUAL_ONLY", "natIps": ["34.1.2.3"]}])
        self.assertIsNone(na.check_router_nat(stock, None, mappings))

    def test_a_dynamic_nat_on_the_default_ceiling_uses_the_dynamic_default(self):
        router = self.router(
            nats=[{"name": "nat-gw", "natIpAllocateOption": "MANUAL_ONLY", "enableDynamicPortAllocation": True, "natIps": ["34.1.2.3"]}]
        )
        mappings = {"nat-gw": [{"instanceName": "vm-1", "interfaceNatMappings": [{"numTotalNatPorts": 60000}]}]}  # 91.6% of 65536
        hit = na.check_router_nat(router, None, mappings)
        self.assertIn("60000/65536", hit["excerpt"])

    def test_two_gateways_on_one_router_do_not_cross_attribute(self):
        """A router's mapping used to be read once, unfiltered, and compared
        against each gateway's ceiling in turn -- so `wide`'s VM at 4096 ports
        was measured a second time against `narrow`'s 1024 and reported at
        400%. Each gateway is keyed to its own `--nat-name` read."""
        router = self.router(
            nats=[
                {"name": "wide", "natIpAllocateOption": "MANUAL_ONLY", "enableDynamicPortAllocation": True, "maxPortsPerVm": 8192, "natIps": ["34.1.2.3"]},
                {"name": "narrow", "natIpAllocateOption": "MANUAL_ONLY", "enableDynamicPortAllocation": True, "maxPortsPerVm": 1024, "natIps": ["34.1.2.4"]},
            ]
        )
        mappings = {
            "wide": [{"instanceName": "busy-vm", "interfaceNatMappings": [{"numTotalNatPorts": 4096}]}],  # 50% of 8192
            "narrow": [{"instanceName": "quiet-vm", "interfaceNatMappings": [{"numTotalNatPorts": 64}]}],  # 6% of 1024
        }
        self.assertIsNone(na.check_router_nat(router, None, mappings))

    def test_no_mapping_data_is_not_a_crash(self):
        hit = na.check_router_nat(self.router(), {"result": {"natStatus": [{"name": "nat-gw", "autoAllocatedNatIps": ["1.2.3.4"]}]}}, None)
        self.assertIsNone(hit)


class PscRoutingTest(unittest.TestCase):
    def test_flags_rejected_service_attachment(self):
        hits = na.check_psc_routing(
            [{"name": "psc-ep-1", "target": "projects/p/regions/us-central1/serviceAttachments/sa-1", "pscConnectionStatus": "REJECTED"}]
        )
        self.assertEqual(hits, [{"object": "ForwardingRule/global/psc-ep-1", "excerpt": "pscConnectionStatus: REJECTED"}])

    def test_a_regional_rule_carries_its_region_in_the_object(self):
        hits = na.check_psc_routing(
            [
                {
                    "name": "psc-ep-1",
                    "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-east4",
                    "target": "projects/p/regions/us-east4/serviceAttachments/sa-1",
                    "pscConnectionStatus": "CLOSED",
                }
            ]
        )
        self.assertEqual(hits[0]["object"], "ForwardingRule/us-east4/psc-ep-1")

    def test_does_not_flag_accepted(self):
        hits = na.check_psc_routing(
            [{"name": "psc-ep-2", "target": "projects/p/regions/us-central1/serviceAttachments/sa-2", "pscConnectionStatus": "ACCEPTED"}]
        )
        self.assertEqual(hits, [])

    def test_ignores_non_psc_forwarding_rules(self):
        hits = na.check_psc_routing([{"name": "lb-rule", "target": "projects/p/global/targetHttpProxies/lb", "pscConnectionStatus": ""}])
        self.assertEqual(hits, [])

    def test_empty_list(self):
        self.assertEqual(na.check_psc_routing([]), [])


class MtuMismatchTest(unittest.TestCase):
    def test_flags_active_peering_with_differing_mtu(self):
        networks = [
            {"name": "vpc-a", "mtu": 1460, "peerings": [{"network": ".../networks/vpc-b", "state": "ACTIVE"}]},
            {"name": "vpc-b", "mtu": 1500, "peerings": [{"network": ".../networks/vpc-a", "state": "ACTIVE"}]},
        ]
        hits = na.check_mtu_mismatch(networks, "p")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "NetworkPeering/vpc-a--vpc-b")

    def test_does_not_double_count_the_pair_from_both_sides(self):
        networks = [
            {"name": "vpc-a", "mtu": 1460, "peerings": [{"network": ".../networks/vpc-b", "state": "ACTIVE"}]},
            {"name": "vpc-b", "mtu": 1500, "peerings": [{"network": ".../networks/vpc-a", "state": "ACTIVE"}]},
        ]
        hits = na.check_mtu_mismatch(networks, "p")
        self.assertEqual(len(hits), 1)

    def test_does_not_flag_matching_mtu(self):
        networks = [
            {"name": "vpc-a", "mtu": 1460, "peerings": [{"network": ".../networks/vpc-b", "state": "ACTIVE"}]},
            {"name": "vpc-b", "mtu": 1460, "peerings": [{"network": ".../networks/vpc-a", "state": "ACTIVE"}]},
        ]
        self.assertEqual(na.check_mtu_mismatch(networks, "p"), [])

    def test_does_not_flag_inactive_peering(self):
        networks = [
            {"name": "vpc-a", "mtu": 1460, "peerings": [{"network": ".../networks/vpc-b", "state": "INACTIVE"}]},
            {"name": "vpc-b", "mtu": 1500, "peerings": []},
        ]
        self.assertEqual(na.check_mtu_mismatch(networks, "p"), [])

    def test_peer_outside_this_project_is_not_a_crash(self):
        networks = [{"name": "vpc-a", "mtu": 1460, "peerings": [{"network": ".../networks/other-project-vpc", "state": "ACTIVE"}]}]
        self.assertEqual(na.check_mtu_mismatch(networks, "p"), [])

    def test_an_absent_mtu_key_is_the_default_and_still_mismatches(self):
        """The shape `networks list` actually returns, and the only one that fires.

        GCP omits `mtu` on every network left at 1460, so the mismatch that
        happens in practice -- a default network peered with one raised to 8896
        -- arrives with the key present on one side only.
        """
        networks = [
            {"name": "vpc-default", "peerings": [{"network": ".../networks/vpc-jumbo", "state": "ACTIVE"}]},
            {"name": "vpc-jumbo", "mtu": 8896, "peerings": [{"network": ".../networks/vpc-default", "state": "ACTIVE"}]},
        ]
        hits = na.check_mtu_mismatch(networks, "p")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "NetworkPeering/vpc-default--vpc-jumbo")
        self.assertIn("vpc-default mtu=1460", hits[0]["excerpt"])
        self.assertIn("vpc-jumbo mtu=8896", hits[0]["excerpt"])

    def test_two_networks_both_silent_about_mtu_agree(self):
        networks = [
            {"name": "vpc-a", "peerings": [{"network": ".../networks/vpc-b", "state": "ACTIVE"}]},
            {"name": "vpc-b", "peerings": [{"network": ".../networks/vpc-a", "state": "ACTIVE"}]},
        ]
        self.assertEqual(na.check_mtu_mismatch(networks, "p"), [])

    def test_a_silent_network_matches_one_that_spells_the_default_out(self):
        networks = [
            {"name": "vpc-a", "peerings": [{"network": ".../networks/vpc-b", "state": "ACTIVE"}]},
            {"name": "vpc-b", "mtu": 1460, "peerings": [{"network": ".../networks/vpc-a", "state": "ACTIVE"}]},
        ]
        self.assertEqual(na.check_mtu_mismatch(networks, "p"), [])

    def test_an_unlisted_peer_is_not_defaulted_into_a_mismatch(self):
        """Absent from the listing is unknown; absent `mtu` on a listed network is 1460.

        Collapsing the two would invent a finding against every jumbo-MTU VPC
        peered out to another project.
        """
        networks = [{"name": "vpc-jumbo", "mtu": 8896, "peerings": [{"network": ".../networks/elsewhere", "state": "ACTIVE"}]}]
        self.assertEqual(na.check_mtu_mismatch(networks, "p"), [])

    def test_a_peer_in_another_project_does_not_resolve_to_the_local_namesake(self):
        """`default` is the most common network name in GCP, so a bare-name
        lookup resolves a cross-project peering to this project's `default` and
        compares the wrong two MTUs. Both sides here are 8896 — a healthy
        peering — and the only way to report one is to have compared
        `vpc-jumbo` against the local `default` instead.
        """
        networks = [
            {
                "name": "default",
                "selfLink": "https://www.googleapis.com/compute/v1/projects/p/global/networks/default",
                "peerings": [],
            },
            {
                "name": "vpc-jumbo",
                "selfLink": "https://www.googleapis.com/compute/v1/projects/p/global/networks/vpc-jumbo",
                "mtu": 8896,
                "peerings": [
                    {
                        "network": "https://www.googleapis.com/compute/v1/projects/other-proj/global/networks/default",
                        "state": "ACTIVE",
                    }
                ],
            },
        ]
        self.assertEqual(na.check_mtu_mismatch(networks, "p"), [])

    def test_a_peer_in_this_project_still_resolves_through_its_self_link(self):
        """The other half of the pair: same URL shape, same project, so the
        lookup must hit. Without it the fix above would be a check that never
        fires rather than one that stopped colliding.
        """
        networks = [
            {
                "name": "default",
                "selfLink": "https://www.googleapis.com/compute/v1/projects/p/global/networks/default",
                "peerings": [
                    {
                        "network": "https://www.googleapis.com/compute/v1/projects/p/global/networks/vpc-jumbo",
                        "state": "ACTIVE",
                    }
                ],
            },
            {
                "name": "vpc-jumbo",
                "selfLink": "https://www.googleapis.com/compute/v1/projects/p/global/networks/vpc-jumbo",
                "mtu": 8896,
                "peerings": [],
            },
        ]
        hits = na.check_mtu_mismatch(networks, "p")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "NetworkPeering/default--vpc-jumbo")
        self.assertIn("default mtu=1460", hits[0]["excerpt"])
        self.assertIn("vpc-jumbo mtu=8896", hits[0]["excerpt"])

    def test_a_peering_given_as_a_partial_url_resolves(self):
        """`NetworkPeering.network` is documented as a full *or* partial URL.

        A partial one carries no `projects/` segment, so keying it as a bare
        name left it unable to join a `selfLink`-keyed listing: a real
        1460-vs-8896 mismatch read clean, with nothing logged. Relative means
        the audited project, so it is qualified with it.
        """
        networks = [
            {
                "name": "default",
                "selfLink": "https://www.googleapis.com/compute/v1/projects/p/global/networks/default",
                "peerings": [
                    {"network": "global/networks/vpc-jumbo", "state": "ACTIVE"}
                ],
            },
            {
                "name": "vpc-jumbo",
                "selfLink": "https://www.googleapis.com/compute/v1/projects/p/global/networks/vpc-jumbo",
                "mtu": 8896,
                "peerings": [],
            },
        ]
        hits = na.check_mtu_mismatch(networks, "p")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "NetworkPeering/default--vpc-jumbo")

    def test_a_listing_entry_without_a_self_link_still_joins_a_full_url(self):
        """The check must not depend on `networks list` emitting `selfLink`.

        If it stops, every entry keys on its bare name, a fully-qualified
        `peerings[].network` matches none of them, and the check reports clean
        for the whole project without erroring -- the silent blind spot this
        file refuses to accept for the PSC filter. Qualifying a `selfLink`-less
        entry with the audited project is what keeps the join working.
        """
        networks = [
            {
                "name": "default",
                "peerings": [
                    {
                        "network": "https://www.googleapis.com/compute/v1/projects/p/global/networks/vpc-jumbo",
                        "state": "ACTIVE",
                    }
                ],
            },
            {"name": "vpc-jumbo", "mtu": 8896, "peerings": []},
        ]
        hits = na.check_mtu_mismatch(networks, "p")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "NetworkPeering/default--vpc-jumbo")

    def test_qualifying_a_relative_peer_does_not_reach_another_project(self):
        """The control for both tests above: qualification uses the audited
        project, so a `selfLink`-less listing does not become a namespace a
        cross-project peering can accidentally land in.
        """
        networks = [
            {
                "name": "vpc-jumbo",
                "mtu": 8896,
                "peerings": [
                    {
                        "network": "https://www.googleapis.com/compute/v1/projects/other-proj/global/networks/default",
                        "state": "ACTIVE",
                    }
                ],
            },
            {"name": "default", "peerings": []},
        ]
        self.assertEqual(na.check_mtu_mismatch(networks, "p"), [])


class CloudArmorTest(unittest.TestCase):
    def test_flags_preview_rule_on_production_backend(self):
        policies = [{"name": "waf-1", "rules": [{"priority": 1000, "preview": True}, {"priority": 2147483647, "preview": False}]}]
        backends = [{"name": "checkout-api", "securityPolicy": ".../securityPolicies/waf-1"}]
        hits = na.check_cloud_armor(policies, backends)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "SecurityPolicy/waf-1")
        self.assertIn("checkout-api", hits[0]["excerpt"])

    def test_does_not_flag_preview_on_staging_backend(self):
        policies = [{"name": "waf-1", "rules": [{"priority": 1000, "preview": True}]}]
        backends = [{"name": "checkout-staging", "securityPolicy": ".../securityPolicies/waf-1"}]
        self.assertEqual(na.check_cloud_armor(policies, backends), [])

    def test_ignores_the_implicit_default_rule_in_preview(self):
        policies = [{"name": "waf-1", "rules": [{"priority": 2147483647, "preview": True}]}]
        backends = [{"name": "checkout-api", "securityPolicy": ".../securityPolicies/waf-1"}]
        self.assertEqual(na.check_cloud_armor(policies, backends), [])

    def test_flags_conflicting_priorities_on_a_production_backend(self):
        policies = [{"name": "waf-2", "rules": [{"priority": 1000}, {"priority": 1000}]}]
        backends = [{"name": "checkout-api", "securityPolicy": ".../securityPolicies/waf-2"}]
        hits = na.check_cloud_armor(policies, backends)
        self.assertIn("conflicting rule priorities: [1000]", hits[0]["excerpt"])

    def test_the_production_gate_governs_the_priority_limb_too(self):
        """§2.5's Do-NOT-flag rule is written about the check, not about its
        first condition. Gating only the preview branch reported a priority
        collision on every policy in the project — including ones protecting a
        `dev` backend and ones protecting nothing at all, where the effective
        policy governs no traffic to be unpredictable about."""
        policies = [{"name": "waf-2", "rules": [{"priority": 1000}, {"priority": 1000}]}]
        for label, backends in (
            ("unattached", []),
            ("staging only", [{"name": "checkout-staging", "securityPolicy": ".../securityPolicies/waf-2"}]),
        ):
            with self.subTest(label):
                self.assertEqual(na.check_cloud_armor(policies, backends), [])

    def test_unattached_policy_is_never_flagged_for_preview(self):
        policies = [{"name": "waf-3", "rules": [{"priority": 1000, "preview": True}]}]
        self.assertEqual(na.check_cloud_armor(policies, []), [])


NETWORK = "https://www.googleapis.com/compute/v1/projects/p1/global/networks/default"
OTHER_NETWORK = "https://www.googleapis.com/compute/v1/projects/p1/global/networks/isolated"


def public_node(name="node-1", ip="203.0.113.7", network=NETWORK, **extra):
    return {
        "name": name,
        "networkInterfaces": [{"network": network, "accessConfigs": [{"natIP": ip}]}],
        **extra,
    }


def world_open(name="allow-ssh", ports=("22",), **extra):
    return {
        "name": name,
        "network": NETWORK,
        "direction": "INGRESS",
        "sourceRanges": ["0.0.0.0/0"],
        "allowed": [{"IPProtocol": "tcp", "ports": list(ports)}],
        **extra,
    }


class WorldOpenIngressTest(unittest.TestCase):
    def test_flags_a_world_open_management_port_reaching_a_public_instance(self):
        hits = na.check_world_open_ingress([world_open()], [public_node()], "p1")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "FirewallRule/allow-ssh")
        self.assertIn("22 (SSH)", hits[0]["excerpt"])
        self.assertIn("node-1 (203.0.113.7)", hits[0]["excerpt"])

    def test_a_web_port_is_never_a_finding(self):
        """GKE writes one `k8s-fw-<hash>` rule per LoadBalancer Service, each
        opening 80 or 443 to 0.0.0.0/0 because that is what the Service asked
        for. Five of them sit on this fleet; admitting the web ports would
        report every one as critical, daily."""
        for port in ("80", "443", "8080"):
            with self.subTest(port):
                rule = world_open(name=f"k8s-fw-{port}", ports=(port,))
                self.assertEqual(na.check_world_open_ingress([rule], [public_node()], "p1"), [])

    def test_a_private_source_range_is_never_a_finding(self):
        rule = world_open(**{"sourceRanges": ["10.128.0.0/9"]})
        self.assertEqual(na.check_world_open_ingress([rule], [public_node()], "p1"), [])

    def test_a_non_tcp_protocol_is_never_a_finding(self):
        rule = dict(world_open(), allowed=[{"IPProtocol": "icmp"}])
        self.assertEqual(na.check_world_open_ingress([rule], [public_node()], "p1"), [])

    def test_a_disabled_or_egress_rule_is_never_a_finding(self):
        for label, overrides in (
            ("disabled", {"disabled": True}),
            ("egress", {"direction": "EGRESS"}),
        ):
            with self.subTest(label):
                self.assertEqual(
                    na.check_world_open_ingress([world_open(**overrides)], [public_node()], "p1"), []
                )

    def test_no_instance_holds_an_external_ip_so_nothing_is_reachable(self):
        """A firewall grants reach only to instances the source range can dial.
        The rule is a latent misconfiguration then, not the exposure this
        check's impact describes -- the same reading §2.5 gives a Cloud Armor
        policy attached to no backend."""
        private = {"name": "node-1", "networkInterfaces": [{"network": NETWORK, "accessConfigs": []}]}
        self.assertEqual(na.check_world_open_ingress([world_open()], [private], "p1"), [])

    def test_a_public_instance_on_another_network_does_not_count(self):
        elsewhere = public_node(network=OTHER_NETWORK)
        self.assertEqual(na.check_world_open_ingress([world_open()], [elsewhere], "p1"), [])

    def test_target_tags_scope_the_rule_to_the_instances_carrying_them(self):
        rule = world_open(targetTags=["bastion"])
        untagged = public_node(name="node-1")
        tagged = public_node(name="bastion-1", ip="203.0.113.9", tags={"items": ["bastion"]})
        self.assertEqual(na.check_world_open_ingress([rule], [untagged], "p1"), [])
        hits = na.check_world_open_ingress([rule], [untagged, tagged], "p1")
        self.assertEqual(len(hits), 1)
        self.assertIn("bastion-1 (203.0.113.9)", hits[0]["excerpt"])
        self.assertNotIn("node-1 (", hits[0]["excerpt"])

    def test_target_service_accounts_scope_the_rule_the_same_way(self):
        rule = world_open(targetServiceAccounts=["ops@p1.iam.gserviceaccount.com"])
        other = public_node(name="node-1", serviceAccounts=[{"email": "apps@p1.iam.gserviceaccount.com"}])
        match = public_node(name="ops-1", ip="203.0.113.9", serviceAccounts=[{"email": "ops@p1.iam.gserviceaccount.com"}])
        self.assertEqual(na.check_world_open_ingress([rule], [other], "p1"), [])
        hits = na.check_world_open_ingress([rule], [other, match], "p1")
        self.assertIn("ops-1", hits[0]["excerpt"])

    def test_an_absent_ports_key_opens_every_management_port(self):
        """`{"IPProtocol": "tcp"}` with no `ports` is the API's encoding for
        every port, not an omission. Reading it as "names none, opens none"
        would let the rule that opens all 65535 read cleaner than one naming
        22."""
        rule = dict(world_open(), allowed=[{"IPProtocol": "tcp"}])
        hits = na.check_world_open_ingress([rule], [public_node()], "p1")
        self.assertEqual(len(hits), 1)
        for port, label in na.MANAGEMENT_PORTS.items():
            self.assertIn(f"{port} ({label})", hits[0]["excerpt"])

    def test_a_port_range_is_expanded_and_a_protocol_of_all_carries_tcp(self):
        for label, allowed in (
            ("range", [{"IPProtocol": "tcp", "ports": ["20-30"]}]),
            ("protocol all", [{"IPProtocol": "all", "ports": ["22"]}]),
            ("protocol number", [{"IPProtocol": "6", "ports": ["22"]}]),
        ):
            with self.subTest(label):
                rule = dict(world_open(), allowed=allowed)
                hits = na.check_world_open_ingress([rule], [public_node()], "p1")
                self.assertIn("22 (SSH)", hits[0]["excerpt"])

    def test_one_finding_per_rule_however_many_ports_it_opens(self):
        rule = world_open(ports=("22", "3389", "5432"))
        hits = na.check_world_open_ingress([rule], [public_node()], "p1")
        self.assertEqual(len(hits), 1)
        for fragment in ("22 (SSH)", "3389 (RDP)", "5432 (PostgreSQL)"):
            self.assertIn(fragment, hits[0]["excerpt"])

    def test_a_blanket_higher_priority_deny_shadows_the_rule(self):
        """A hardened VPC is routinely a deny-all ingress over narrow allows.
        Reporting the allows underneath it would be wrong about every one."""
        allow = world_open(priority=1000)
        deny = {
            "name": "deny-all",
            "network": NETWORK,
            "direction": "INGRESS",
            "priority": 100,
            "sourceRanges": ["0.0.0.0/0"],
            "denied": [{"IPProtocol": "tcp"}],
        }
        self.assertEqual(na.check_world_open_ingress([allow, deny], [public_node()], "p1"), [])

    def test_a_deny_that_does_not_outrank_the_allow_shadows_nothing(self):
        allow = world_open(priority=100)
        deny = {
            "name": "deny-all",
            "network": NETWORK,
            "direction": "INGRESS",
            "priority": 1000,
            "sourceRanges": ["0.0.0.0/0"],
            "denied": [{"IPProtocol": "tcp"}],
        }
        self.assertEqual(len(na.check_world_open_ingress([allow, deny], [public_node()], "p1")), 1)

    def test_a_target_scoped_deny_is_not_treated_as_blanket(self):
        """Whether it covers the instances found reachable is undecidable here,
        and assuming it does hides a live exposure."""
        allow = world_open(priority=1000)
        deny = {
            "name": "deny-bastion",
            "network": NETWORK,
            "direction": "INGRESS",
            "priority": 100,
            "targetTags": ["bastion"],
            "sourceRanges": ["0.0.0.0/0"],
            "denied": [{"IPProtocol": "tcp"}],
        }
        self.assertEqual(len(na.check_world_open_ingress([allow, deny], [public_node()], "p1")), 1)

    def test_a_deny_covering_only_some_ports_leaves_the_rest_reported(self):
        allow = world_open(ports=("22", "3389"), priority=1000)
        deny = {
            "name": "deny-ssh",
            "network": NETWORK,
            "direction": "INGRESS",
            "priority": 100,
            "sourceRanges": ["0.0.0.0/0"],
            "denied": [{"IPProtocol": "tcp", "ports": ["22"]}],
        }
        hits = na.check_world_open_ingress([allow, deny], [public_node()], "p1")
        self.assertEqual(len(hits), 1)
        self.assertIn("3389 (RDP)", hits[0]["excerpt"])
        self.assertNotIn("22 (SSH)", hits[0]["excerpt"])

    def test_the_excerpt_caps_the_names_and_counts_the_rest(self):
        nodes = [public_node(name=f"node-{i}", ip=f"203.0.113.{i}") for i in range(10)]
        hits = na.check_world_open_ingress([world_open()], nodes, "p1")
        self.assertIn("10 instance(s)", hits[0]["excerpt"])
        self.assertIn(f"+{10 - na.MAX_NAMED_EXPOSED_INSTANCES} more", hits[0]["excerpt"])

    def test_the_excerpt_attributes_the_count_to_what_the_identity_can_see(self):
        # The count is a floor: `compute instances list` hides GKE Autopilot
        # node VMs from a `roles/compute.viewer` caller, four of eighteen on the
        # reference install. Claiming it as the project's total would overstate
        # what the read establishes, so the wording is pinned here rather than
        # left to whoever next edits the f-string.
        hits = na.check_world_open_ingress([world_open()], [public_node()], "p1")
        self.assertIn("visible to the audit identity", hits[0]["excerpt"])

    def test_the_excerpt_says_when_nothing_narrows_the_target_scope(self):
        hits = na.check_world_open_ingress([world_open()], [public_node()], "p1")
        self.assertIn("no target restriction", hits[0]["excerpt"])

    def test_an_unparseable_port_spec_is_skipped_rather_than_crashing(self):
        rule = dict(world_open(), allowed=[{"IPProtocol": "tcp", "ports": ["not-a-port"]}])
        self.assertEqual(na.check_world_open_ingress([rule], [public_node()], "p1"), [])

    def test_severity_and_impact_are_registered_for_the_slug(self):
        emitted = na.emit("firewall-world-open-ingress", {"object": "FirewallRule/x", "excerpt": "y"}, "gcloud compute firewall-rules list")
        self.assertEqual(emitted["severity"], "critical")
        self.assertTrue(emitted["impact"])



PROJECT_SLUGS = [
    "cloud-nat-exhaustion",
    "psc-routing-deadlock",
    "mtu-packet-fragmentation",
    "cloud-armor-false-positive",
    "firewall-world-open-ingress",
]


def dynamic_router(name="nat-router", nat="nat-gw"):
    return {
        "name": name,
        "region": "https://www.googleapis.com/compute/v1/projects/p1/regions/us-central1",
        "nats": [{"name": nat, "natIpAllocateOption": "MANUAL_ONLY", "enableDynamicPortAllocation": True,
                  "maxPortsPerVm": 4096, "natIps": ["34.1.2.3"]}],
    }


HOST_NETWORK = "https://www.googleapis.com/compute/v1/projects/host/global/networks/shared"


class WorldOpenPrecedenceAndFamilyTest(unittest.TestCase):
    def deny(self, priority, source):
        return {
            "name": "deny-all",
            "network": NETWORK,
            "direction": "INGRESS",
            "priority": priority,
            "sourceRanges": [source],
            "denied": [{"IPProtocol": "tcp"}],
        }

    def test_an_equal_priority_deny_blocks_the_allow(self):
        """GCP lets a deny win at equal priority."""
        allow = world_open(priority=1000)
        self.assertEqual(na.check_world_open_ingress([allow, self.deny(1000, "0.0.0.0/0")], [public_node()], "p1"), [])

    def test_an_ipv6_only_deny_does_not_cancel_an_ipv4_allow(self):
        allow = world_open(priority=1000)
        hits = na.check_world_open_ingress([allow, self.deny(100, "::/0")], [public_node()], "p1")
        self.assertEqual(len(hits), 1)

    def test_an_ipv6_only_allow_does_not_reach_an_ipv4_address(self):
        rule = world_open(sourceRanges=["::/0"])
        self.assertEqual(na.check_world_open_ingress([rule], [public_node()], "p1"), [])

    def test_an_ipv6_only_allow_reaches_an_external_ipv6_address(self):
        rule = world_open(sourceRanges=["::/0"])
        node = {
            "name": "v6-node",
            "networkInterfaces": [{"network": NETWORK, "ipv6AccessConfigs": [{"externalIpv6": "2001:db8::7"}]}],
        }
        self.assertEqual(len(na.check_world_open_ingress([rule], [node], "p1")), 1)


class WorldOpenDenyScopeTest(unittest.TestCase):
    """A DENY applies only to the instances it covers: its target and its
    destination ranges decide which ones."""

    def deny(self, ports=("22",), priority=100, **extra):
        return {
            "name": "deny-some",
            "network": NETWORK,
            "direction": "INGRESS",
            "priority": priority,
            "sourceRanges": ["0.0.0.0/0"],
            "denied": [{"IPProtocol": "tcp", "ports": list(ports)}],
            **extra,
        }

    def test_a_deny_for_one_destination_does_not_cancel_the_allow_for_all(self):
        rules = [world_open(), self.deny(destinationRanges=["10.99.0.5/32"])]
        self.assertEqual(len(na.check_world_open_ingress(rules, [public_node()], "p1")), 1)

    def test_a_deny_for_the_instance_address_blocks_that_instance(self):
        rules = [world_open(), self.deny(destinationRanges=["203.0.113.7/32"])]
        self.assertEqual(na.check_world_open_ingress(rules, [public_node()], "p1"), [])

    def test_a_target_scoped_deny_blocks_only_the_instances_it_targets(self):
        """The fixture's shape: a stock `default-allow-ssh` and a DENY on the
        tag of the planted VM."""
        allow = world_open(name="default-allow-ssh", ports=("22",), priority=65534)
        deny = self.deny(ports=("22", "3389"), priority=900, targetTags=["x"])
        tagged = public_node(name="node-1", tags={"items": ["x"]})
        self.assertEqual(na.check_world_open_ingress([allow, deny], [tagged], "p1"), [])
        other = public_node(name="node-2", ip="203.0.113.8")
        hits = na.check_world_open_ingress([allow, deny], [tagged, other], "p1")
        self.assertEqual(len(hits), 1)
        self.assertIn("node-2", hits[0]["excerpt"])
        self.assertNotIn("node-1", hits[0]["excerpt"])

    def test_a_stopped_target_is_decided_and_clear(self):
        rule = world_open(name="scoped", targetTags=["x"])
        stopped = public_node(name="old-1", status="TERMINATED", tags={"items": ["x"]})
        self.assertEqual(na.world_open_ingress([rule], [stopped], "p1"), ([], []))


class WorldOpenExternalAddressTest(unittest.TestCase):
    """Internet traffic arrives at an external address. The rules decide
    reachability for each external address on the rule's network."""

    def deny(self, ports=("22",), priority=100, source="0.0.0.0/0", **extra):
        return {
            "name": "deny-some",
            "network": NETWORK,
            "direction": "INGRESS",
            "priority": priority,
            "sourceRanges": [source],
            "denied": [{"IPProtocol": "tcp", "ports": list(ports)}],
            **extra,
        }

    def test_a_deny_for_an_internal_range_does_not_clear_the_external_address(self):
        node = {
            "name": "node-1",
            "networkInterfaces": [
                {"network": NETWORK, "networkIP": "10.128.0.5", "accessConfigs": [{"natIP": "203.0.113.7"}]}
            ],
        }
        rules = [world_open(), self.deny(destinationRanges=["10.128.0.0/9"])]
        self.assertEqual(len(na.check_world_open_ingress(rules, [node], "p1")), 1)

    def test_a_deny_that_matches_an_interface_on_another_network_does_not_clear(self):
        node = {
            "name": "node-1",
            "networkInterfaces": [
                {"network": NETWORK, "accessConfigs": [{"natIP": "203.0.113.7"}]},
                {"network": OTHER_NETWORK, "accessConfigs": [{"natIP": "198.51.100.9"}]},
            ],
        }
        rules = [world_open(), self.deny(destinationRanges=["198.51.100.9/32"])]
        self.assertEqual(len(na.check_world_open_ingress(rules, [node], "p1")), 1)

    def test_an_ipv4_deny_blocks_an_ipv4_only_instance_of_a_dual_stack_allow(self):
        allow = world_open(sourceRanges=["0.0.0.0/0", "::/0"], priority=1000)
        self.assertEqual(na.check_world_open_ingress([allow, self.deny()], [public_node()], "p1"), [])

    def test_the_allow_destination_ranges_must_contain_the_address(self):
        allow = world_open(destinationRanges=["198.51.100.0/24"])
        self.assertEqual(na.check_world_open_ingress([allow], [public_node()], "p1"), [])
        inside = public_node(ip="198.51.100.20")
        self.assertEqual(len(na.check_world_open_ingress([allow], [inside], "p1")), 1)

    def test_the_excerpt_names_only_the_ports_that_are_reachable(self):
        # A target-scoped DENY blocks 22 on the one instance it targets.
        allow = world_open(ports=("22", "3389"))
        deny = self.deny(ports=("22",), targetTags=["x"])
        node = public_node(tags={"items": ["x"]})
        hits = na.check_world_open_ingress([allow, deny], [node], "p1")
        self.assertEqual(len(hits), 1)
        self.assertIn("3389 (RDP)", hits[0]["excerpt"])
        self.assertNotIn("22 (SSH)", hits[0]["excerpt"])

    def test_a_staging_or_repairing_instance_is_live(self):
        for status in ("STAGING", "PROVISIONING", "REPAIRING"):
            with self.subTest(status=status):
                node = public_node(status=status)
                self.assertEqual(len(na.check_world_open_ingress([world_open()], [node], "p1")), 1)


class WorldOpenUndecidedTest(unittest.TestCase):
    def test_a_target_scoped_rule_reaching_no_visible_instance_is_undecided(self):
        rule = world_open(name="allow-ssh-autopilot", targetTags=["gk3-pool"])
        hits, undecided = na.world_open_ingress([rule], [public_node()], "p1")
        self.assertEqual(hits, [])
        self.assertEqual(undecided, ["allow-ssh-autopilot"])

    def test_a_target_whose_instances_have_no_external_ip_is_not_undecided(self):
        """The audit sees the tagged instance, so the rule is decided: nothing
        on the internet can dial an instance with no external IP."""
        rule = world_open(name="db-open", ports=("5432",), targetTags=["db"])
        vm = {"name": "db-1", "tags": {"items": ["db"]}, "networkInterfaces": [{"network": NETWORK}]}
        hits, undecided = na.world_open_ingress([rule], [vm], "p1")
        self.assertEqual((hits, undecided), ([], []))

    def test_an_unscoped_rule_reaching_nothing_is_not_undecided(self):
        hits, undecided = na.world_open_ingress([world_open()], [], "p1")
        self.assertEqual((hits, undecided), ([], []))

    def test_the_project_target_names_an_undecided_rule_in_limitations(self):
        answers = project_answers(**{
            "firewall-rules list": [world_open(name="allow-ssh-autopilot", targetTags=["gk3-pool"])],
        })
        with patch.object(networking_audit, "run_cmd", side_effect=fake_run_cmd({"p1": answers})), \
                patch("sys.stderr", new_callable=io.StringIO):
            entry = networking_audit.collect_project_target("p1")
        self.assertIn("allow-ssh-autopilot", entry["limitations"])
        self.assertIn("confirm by hand", entry["limitations"])


class SharedVpcFirewallTest(unittest.TestCase):
    def setUp(self):
        networking_audit.PROJECT_NUMBERS.clear()
        self.addCleanup(networking_audit.PROJECT_NUMBERS.clear)
        self.addCleanup(networking_audit.FIREWALL_READS.clear)

    def test_a_host_project_rule_reaches_a_service_project_vm(self):
        """The rule lives in the host project and the external-IP VM in a
        service project on the host's network: measured against its own
        project's instances alone, the host never fires."""
        rule = dict(world_open(name="host-allow-ssh"), network=HOST_NETWORK)
        vm = public_node(name="svc-vm", network=HOST_NETWORK)
        per_project = {
            "host": project_answers(**{"firewall-rules list": [rule]}),
            "svc": project_answers(**{"instances list": [vm]}),
        }
        with patch.object(networking_audit, "get_target_projects", return_value=["host", "svc"]), \
                patch.object(networking_audit, "run_cmd", side_effect=fake_run_cmd(per_project)), \
                patch("sys.stderr", new_callable=io.StringIO):
            manifest = networking_audit.collect_fleet()
        by_name = {e["name"]: e for e in manifest["clusters"]}
        objects = [c["object"] for c in by_name["project/host"]["candidates"]]
        self.assertEqual(objects, ["FirewallRule/host-allow-ssh"])
        self.assertIn("svc-vm", by_name["project/host"]["candidates"][0]["excerpt"])
        command = by_name["project/host"]["candidates"][0]["command"]
        self.assertIn("gcloud compute firewall-rules list --project=host", command)
        self.assertIn("gcloud compute instances list --project=svc", command)


class FleetInstancesUnreadTest(unittest.TestCase):
    def setUp(self):
        networking_audit.PROJECT_NUMBERS.clear()
        self.addCleanup(networking_audit.PROJECT_NUMBERS.clear)
        self.addCleanup(networking_audit.FIREWALL_READS.clear)

    def test_a_failed_instance_read_in_another_project_is_named_on_the_host(self):
        """The host's rules are measured against the fleet's instances. A
        service project whose `instances list` failed is missing from that
        list, so the host's verdict names it rather than read clean."""
        rule = dict(world_open(name="host-allow-ssh"), network=HOST_NETWORK)
        per_project = {
            "host": project_answers(**{"firewall-rules list": [rule]}),
            "svc": project_answers(**{"instances list": (1, "ERROR: deadline exceeded")}),
        }
        with patch.object(networking_audit, "get_target_projects", return_value=["host", "svc"]), \
                patch.object(networking_audit, "run_cmd", side_effect=fake_run_cmd(per_project)), \
                patch("sys.stderr", new_callable=io.StringIO):
            manifest = networking_audit.collect_fleet()
        host = {e["name"]: e for e in manifest["clusters"]}["project/host"]
        self.assertEqual(host["candidates"], [])
        self.assertIn("without the instances of 1 other project(s)", host["limitations"])
        self.assertIn("svc", host["limitations"])

    def test_a_failed_firewall_read_in_another_project_is_named_with_the_right_cause(self):
        rule = dict(world_open(name="host-allow-ssh"), network=HOST_NETWORK)
        per_project = {
            "host": project_answers(**{"firewall-rules list": [rule]}),
            "svc": project_answers(**{"firewall-rules list": (1, "ERROR: PERMISSION_DENIED")}),
        }
        with patch.object(networking_audit, "get_target_projects", return_value=["host", "svc"]), \
                patch.object(networking_audit, "run_cmd", side_effect=fake_run_cmd(per_project)), \
                patch("sys.stderr", new_callable=io.StringIO):
            manifest = networking_audit.collect_fleet()
        host = {e["name"]: e for e in manifest["clusters"]}["project/host"]
        self.assertIn("whose firewall or instance reads failed: svc", host["limitations"])
        self.assertNotIn("because their `instances list` read failed", host["limitations"])

    def test_one_run_lists_each_project_s_instances_once(self):
        calls = []
        per_project = {"p1": project_answers(), "p2": project_answers()}
        with patch.object(networking_audit, "get_target_projects", return_value=["p1", "p2"]), \
                patch.object(networking_audit, "run_cmd", side_effect=fake_run_cmd(per_project, calls)), \
                patch("sys.stderr", new_callable=io.StringIO):
            networking_audit.collect_fleet()
        for project in ("p1", "p2"):
            reads = [c for c in calls if c[2:4] == ["instances", "list"] and f"--project={project}" in c]
            self.assertEqual(len(reads), 1, project)


class ForwardingRulesUnreadTest(unittest.TestCase):
    """The firewall check uses the `forwarding-rules list` read for its
    load-balancer path. When that read fails, the check says so."""

    def setUp(self):
        networking_audit.PROJECT_NUMBERS.clear()
        self.addCleanup(networking_audit.PROJECT_NUMBERS.clear)
        self.addCleanup(networking_audit.FIREWALL_READS.clear)

    def manifest(self, per_project, projects):
        with patch.object(networking_audit, "get_target_projects", return_value=projects), \
                patch.object(networking_audit, "run_cmd", side_effect=fake_run_cmd(per_project)), \
                patch("sys.stderr", new_callable=io.StringIO):
            return {e["name"]: e for e in networking_audit.collect_fleet()["clusters"]}

    def test_a_failed_forwarding_read_names_the_unmeasured_load_balancer_path(self):
        per_project = {
            "p1": project_answers(**{
                "firewall-rules list": [gke_lb_rule()],
                "forwarding-rules list": (1, "ERROR: deadline exceeded"),
            }),
        }
        target = self.manifest(per_project, ["p1"])["project/p1"]
        self.assertIn(networking_audit.FORWARDING_UNREAD_LIMITATION, target["limitations"])
        self.assertIn("firewall-world-open-ingress", [c["check"] for c in target["commands"]])

    def test_a_passed_forwarding_read_is_part_of_the_firewall_record(self):
        target = self.manifest({"p1": project_answers()}, ["p1"])["project/p1"]
        record = next(c for c in target["commands"] if c["check"] == "firewall-world-open-ingress")
        self.assertIn("gcloud compute forwarding-rules list --project=p1 --format=json", record["command"])
        self.assertNotIn("limitations", target)

    def test_a_load_balancer_hit_names_the_forwarding_read(self):
        node = dict(private_node(), selfLink="https://x/projects/p1/zones/us-central1-a/instances/node-1")
        per_project = {
            "p1": project_answers(**{
                "firewall-rules list": [gke_lb_rule()],
                "instances list": [node],
                "forwarding-rules list": [passthrough()],
            }),
        }
        target = self.manifest(per_project, ["p1"])["project/p1"]
        hit = next(c for c in target["candidates"] if c["check"] == "firewall-world-open-ingress")
        self.assertIn("gcloud compute forwarding-rules list --project=p1 --format=json", hit["command"])

    def test_another_project_s_failed_forwarding_read_is_named_on_the_host(self):
        rule = dict(world_open(name="host-allow-ssh"), network=HOST_NETWORK)
        per_project = {
            "host": project_answers(**{"firewall-rules list": [rule]}),
            "svc": project_answers(**{
                "forwarding-rules list": (1, "ERROR: deadline exceeded"),
            }),
        }
        host = self.manifest(per_project, ["host", "svc"])["project/host"]
        self.assertIn("without the forwarding rules of 1 other project(s)", host["limitations"])
        self.assertIn("svc", host["limitations"])


class NonProductionTokenTest(unittest.TestCase):
    def test_a_token_match_not_a_substring_match(self):
        for name in ("device-gateway", "payments-latest", "backstage-portal", "qatar-checkout", "contest-api"):
            with self.subTest(name):
                self.assertFalse(na._looks_non_production(name))
        for name in ("api-dev", "test-web", "staging-db", "web_qa", "api-dev2", "Sandbox.svc"):
            with self.subTest(name):
                self.assertTrue(na._looks_non_production(name))


LB_IP = "34.1.2.3"


def gke_lb_rule(ports=("5432",), **extra):
    """The allow a GKE LoadBalancer Service writes: the internet to the node
    tag, with the load balancer IP as the only destination range."""
    return world_open(
        name="k8s-fw-abc", ports=ports, targetTags=["gke-node"], destinationRanges=[f"{LB_IP}/32"], **extra
    )


def passthrough(scheme="EXTERNAL", ports=("5432",), **extra):
    fields = {
        "name": "a1b2c3",
        "IPAddress": LB_IP,
        "loadBalancingScheme": scheme,
        "IPProtocol": "TCP",
        "ports": list(ports),
        "target": "https://www.googleapis.com/compute/v1/projects/p1/regions/us-central1/targetPools/a1b2c3",
    }
    fields.update(extra)
    return fields


def private_node(name="node-1"):
    return {
        "name": name,
        "networkInterfaces": [{"network": NETWORK, "networkIP": "10.128.0.5"}],
        "tags": {"items": ["gke-node"]},
    }


class WorldOpenLoadBalancerTest(unittest.TestCase):
    """An external passthrough load balancer keeps its IP as the packet
    destination, so the backends get internet traffic with or without an
    external IP."""

    def test_a_private_backend_behind_a_passthrough_load_balancer_is_reachable(self):
        hits = na.check_world_open_ingress([gke_lb_rule()], [private_node()], "p1", [passthrough()])
        self.assertEqual(len(hits), 1)
        self.assertIn(f"through load balancer a1b2c3 {LB_IP}", hits[0]["excerpt"])
        self.assertIn("5432 (PostgreSQL)", hits[0]["excerpt"])

    def test_the_excerpt_does_not_say_a_private_backend_holds_an_external_ip(self):
        excerpt = na.check_world_open_ingress([gke_lb_rule()], [private_node()], "p1", [passthrough()])[0]["excerpt"]
        self.assertNotIn("hold an external IP", excerpt)
        self.assertIn(na.LOAD_BALANCER_PATH_NOTE, excerpt)

    def test_a_wide_destination_range_names_no_load_balancer(self):
        wide = {**gke_lb_rule(), "destinationRanges": ["34.0.0.0/8"]}
        self.assertEqual(na.check_world_open_ingress([wide], [private_node()], "p1", [passthrough()]), [])

    def test_a_tcp_proxy_forwarding_rule_is_not_a_passthrough_path(self):
        proxy = passthrough(target="https://www.googleapis.com/compute/v1/projects/p1/global/targetTcpProxies/x")
        self.assertEqual(na.check_world_open_ingress([gke_lb_rule()], [private_node()], "p1", [proxy]), [])

    def test_a_backend_service_forwarding_rule_is_a_passthrough_path(self):
        regional = passthrough(target="", backendService="projects/p1/regions/us-central1/backendServices/b")
        self.assertEqual(len(na.check_world_open_ingress([gke_lb_rule()], [private_node()], "p1", [regional])), 1)

    def test_without_the_forwarding_rule_the_private_backend_is_not_reachable(self):
        self.assertEqual(na.check_world_open_ingress([gke_lb_rule()], [private_node()], "p1", []), [])

    def test_a_public_backend_is_reachable_on_the_load_balancer_ip(self):
        node = public_node(tags={"items": ["gke-node"]})
        hits = na.check_world_open_ingress([gke_lb_rule()], [node], "p1", [passthrough()])
        self.assertEqual(len(hits), 1)

    def test_a_proxy_load_balancer_is_not_a_passthrough_path(self):
        managed = passthrough(scheme="EXTERNAL_MANAGED")
        self.assertEqual(na.check_world_open_ingress([gke_lb_rule()], [private_node()], "p1", [managed]), [])

    def test_the_forwarding_rule_must_carry_the_management_port(self):
        web = passthrough(ports=("443",))
        self.assertEqual(na.check_world_open_ingress([gke_lb_rule()], [private_node()], "p1", [web]), [])
        everything = passthrough(ports=(), allPorts=True)
        self.assertEqual(len(na.check_world_open_ingress([gke_lb_rule()], [private_node()], "p1", [everything])), 1)


class WorldOpenAutopilotNetworkTest(unittest.TestCase):
    """The audit identity does not see GKE Autopilot nodes, and a rule without
    a target applies to every node on its network."""

    AUTOPILOT = {na._network_key(NETWORK, "p1"): {"p1/us-central1/ap-1"}}

    def test_an_untargeted_rule_on_an_autopilot_network_is_undecided(self):
        rule = world_open(name="allow-kubelet", ports=("10250",))
        hits, undecided = na.world_open_ingress([rule], [], "p1", [], self.AUTOPILOT)
        self.assertEqual(hits, [])
        self.assertEqual(undecided, ["allow-kubelet (Autopilot cluster(s) p1/us-central1/ap-1)"])

    def test_an_untargeted_rule_where_the_clusters_read_failed_is_undecided(self):
        rule = world_open(name="allow-kubelet", ports=("10250",))
        hits, undecided = na.world_open_ingress([rule], [], "p1", [], {}, ["p1"])
        self.assertEqual(hits, [])
        self.assertEqual(undecided, ["allow-kubelet (clusters read failed in p1)"])

    def test_a_failed_clusters_read_is_reported_by_the_subnet_sweep(self):
        per_project = {"p1": project_answers(**{"clusters list": (1, "PERMISSION_DENIED")})}
        with patch.object(networking_audit, "run_cmd", side_effect=fake_run_cmd(per_project)), \
                patch("sys.stderr", new_callable=io.StringIO):
            usage = networking_audit.read_subnet_usage(["p1"])
        self.assertEqual(usage["clusters_unread"], ["p1"])

    def test_an_untargeted_rule_without_an_autopilot_cluster_is_clear(self):
        rule = world_open(name="allow-kubelet", ports=("10250",))
        self.assertEqual(na.world_open_ingress([rule], [], "p1", [], {}), ([], []))

    def test_the_collector_finds_the_autopilot_network_in_its_clusters_read(self):
        cluster = {
            "name": "ap-1",
            "location": "us-central1",
            "autopilot": {"enabled": True},
            "networkConfig": {"network": "projects/p1/global/networks/default"},
        }
        per_project = {
            "p1": project_answers(**{
                "clusters list": [cluster],
                "firewall-rules list": [world_open(name="allow-kubelet", ports=("10250",))],
            }),
        }
        with patch.object(networking_audit, "get_target_projects", return_value=["p1"]), \
                patch.object(networking_audit, "run_cmd", side_effect=fake_run_cmd(per_project)), \
                patch("sys.stderr", new_callable=io.StringIO):
            manifest = networking_audit.collect_fleet()
        target = {e["name"]: e for e in manifest["clusters"]}["project/p1"]
        self.assertIn("allow-kubelet (Autopilot cluster(s) p1/us-central1/ap-1)", target["limitations"])


class ManagementPortsPinTest(unittest.TestCase):
    def test_the_networking_port_set_matches_the_compliance_collectors(self):
        """`collect.py` checks LoadBalancer Services against the same ports; a
        port admitted on one side only is a rule reported daily for a Service
        the other side leaves open."""
        import importlib.util

        path = os.path.join(os.path.dirname(__file__), "..", "..", "fleet-audit", "scripts", "collect.py")
        spec = importlib.util.spec_from_file_location("collect_for_pin", path)
        collect = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collect)
        self.assertEqual(set(na.MANAGEMENT_PORTS), set(collect._WORLD_OPEN_LB_PORTS))


class ProjectTargetTest(unittest.TestCase):
    def setUp(self):
        networking_audit.PROJECT_NUMBERS.clear()
        self.addCleanup(networking_audit.PROJECT_NUMBERS.clear)

    def collect(self, calls=None, **answers):
        with patch.object(networking_audit, "run_cmd",
                          side_effect=fake_run_cmd({"p1": project_answers(**answers)}, calls)), \
                patch("sys.stderr", new_callable=io.StringIO):
            return networking_audit.collect_project_target("p1")

    def test_a_quiet_project_records_all_five_checks_as_run(self):
        entry = self.collect()
        self.assertEqual(entry["name"], "project/p1")
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual([c["check"] for c in entry["commands"]], PROJECT_SLUGS)
        self.assertEqual(entry["candidates"], [])
        self.assertEqual(entry["checks_not_applicable"], [])
        for command in entry["commands"]:
            self.assertEqual(command["rc"], 0)
            self.assertRegex(command["output_sha256"], r"^[0-9a-f]{64}$")

    def test_each_dynamic_gateway_is_read_by_name(self):
        calls = []
        mapping = [{"instanceName": "busy-vm", "interfaceNatMappings": [{"numTotalNatPorts": 4000}]}]
        entry = self.collect(calls, **{"routers list": [dynamic_router()], "get-nat-mapping-info": mapping})
        reads = [" ".join(c) for c in calls if "get-nat-mapping-info" in c]
        self.assertEqual(len(reads), 1)
        self.assertIn("--nat-name=nat-gw", reads[0])
        self.assertIn("--region=us-central1", reads[0])
        candidate = entry["candidates"][0]
        self.assertEqual(candidate["object"], "Router/us-central1/nat-router")
        self.assertIn("busy-vm using 4000/4096", candidate["excerpt"])
        # The router's own reads, not every router's.
        self.assertIn("gcloud compute routers get-status nat-router", candidate["command"])
        self.assertIn("--nat-name=nat-gw", candidate["command"])

    def test_a_static_gateway_is_not_read_for_its_mapping(self):
        calls = []
        router = dynamic_router()
        router["nats"][0]["enableDynamicPortAllocation"] = False
        self.collect(calls, **{"routers list": [router]})
        self.assertFalse([c for c in calls if "get-nat-mapping-info" in c])
        self.assertTrue([c for c in calls if "get-status" in c])

    def test_a_router_without_nats_is_not_read_further(self):
        calls = []
        self.collect(calls, **{"routers list": [{"name": "bgp-only", "region": "regions/us-central1"}]})
        self.assertFalse([c for c in calls if "get-status" in c])

    def test_a_failed_read_costs_only_its_own_check(self):
        entry = self.collect(**{"networks list": (1, "PERMISSION_DENIED compute.networks.list")})
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual(unevaluated_slugs(entry), ["mtu-packet-fragmentation"])
        self.assertIn("PERMISSION_DENIED", entry["checks_unevaluated"][0]["reason"])
        self.assertIn("mtu-packet-fragmentation could not be evaluated", entry["limitations"])
        self.assertNotIn("mtu-packet-fragmentation", [c["check"] for c in entry["commands"]])

    def test_a_nat_read_failure_keeps_the_other_checks_and_their_findings(self):
        entry = self.collect(**{
            "routers list": [dynamic_router()],
            "get-status": (1, "DEADLINE_EXCEEDED"),
            "firewall-rules list": [world_open()],
            "instances list": [public_node()],
            "forwarding-rules list": [{
                "name": "psc-ep-1",
                "target": "projects/p1/regions/us-central1/serviceAttachments/sa-1",
                "pscConnectionStatus": "REJECTED",
            }],
        })
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual(unevaluated_slugs(entry), ["cloud-nat-exhaustion"])
        self.assertEqual(
            [c["check"] for c in entry["commands"]],
            ["psc-routing-deadlock", "mtu-packet-fragmentation", "cloud-armor-false-positive",
             "firewall-world-open-ingress"],
        )
        self.assertEqual(
            sorted(c["check"] for c in entry["candidates"]),
            ["firewall-world-open-ingress", "psc-routing-deadlock"],
        )

    def test_a_firewall_read_failure_lists_only_the_firewall_check(self):
        entry = self.collect(**{"firewall-rules list": (1, "PERMISSION_DENIED compute.firewalls.list")})
        self.assertEqual(unevaluated_slugs(entry), ["firewall-world-open-ingress"])
        self.assertEqual(len(entry["commands"]), 4)

    def test_a_get_status_that_is_not_an_object_costs_the_nat_check(self):
        entry = self.collect(**{"routers list": [dynamic_router()], "get-status": []})
        self.assertEqual(unevaluated_slugs(entry), ["cloud-nat-exhaustion"])
        self.assertIn("returned list", entry["checks_unevaluated"][0]["reason"])

    def test_a_nat_mapping_that_is_not_a_list_costs_the_nat_check(self):
        mapping = {"n": {"result": [{"instanceName": "busy-vm", "interfaceNatMappings": [{"numTotalNatPorts": 4000}]}]}}
        entry = self.collect(**{"routers list": [dynamic_router()], "get-nat-mapping-info": mapping})
        self.assertEqual(unevaluated_slugs(entry), ["cloud-nat-exhaustion"])
        self.assertIn("returned dict", entry["checks_unevaluated"][0]["reason"])

    def test_every_read_failing_gates_the_target(self):
        entry = self.collect(**{phrase: (1, "PERMISSION_DENIED") for phrase in (
            "routers list", "forwarding-rules list", "networks list", "security-policies list",
            "firewall-rules list")})
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("cloud-nat-exhaustion", entry["error"])
        self.assertNotIn("commands", entry)

    def test_each_candidate_names_the_reads_that_produced_it(self):
        entry = self.collect(**{
            "firewall-rules list": [world_open()],
            "instances list": [public_node()],
            "forwarding-rules list": [{
                "name": "psc-ep-1",
                "target": "projects/p1/regions/us-central1/serviceAttachments/sa-1",
                "pscConnectionStatus": "REJECTED",
            }],
        })
        by_check = {c["check"]: c for c in entry["candidates"]}
        self.assertEqual(by_check["psc-routing-deadlock"]["command"],
                         "gcloud compute forwarding-rules list --project=p1 --format=json")
        self.assertEqual(
            by_check["firewall-world-open-ingress"]["command"],
            "gcloud compute firewall-rules list --project=p1 --format=json && "
            "gcloud compute instances list --project=p1 "
            "'--format=json(name,selfLink,zone,status,networkInterfaces,tags,serviceAccounts)'",
        )
        self.assertEqual(by_check["firewall-world-open-ingress"]["severity"], "critical")


def unevaluated_slugs(entry):
    return [e["check"] for e in entry.get("checks_unevaluated") or []]


class FirewallReadNarrowingTest(unittest.TestCase):
    def test_a_stopped_instance_with_a_static_ip_is_not_reachable(self):
        stopped = public_node(name="stopped-vm", status="TERMINATED")
        running = public_node(name="running-vm", ip="203.0.113.8", status="RUNNING")
        hits = na.check_world_open_ingress([world_open()], [stopped, running], "p1")
        self.assertEqual(len(hits), 1)
        self.assertIn("running-vm", hits[0]["excerpt"])
        self.assertNotIn("stopped-vm", hits[0]["excerpt"])
        self.assertEqual(na.check_world_open_ingress([world_open()], [stopped], "p1"), [])

    def test_the_excerpt_names_the_policy_blind_spot(self):
        hit = na.check_world_open_ingress([world_open()], [public_node()], "p1")[0]
        self.assertIn("network firewall rules only; firewall policies were not read", hit["excerpt"])

    def test_the_instance_read_is_narrowed_to_the_fields_used(self):
        argv = networking_audit.project_commands("p1")["instances"]
        self.assertEqual(argv[-1], "--format=json(name,selfLink,zone,status,networkInterfaces,tags,serviceAccounts)")


class SlugReadsTest(unittest.TestCase):
    def test_the_digest_is_the_concatenations_and_no_body_is_kept(self):
        reads = networking_audit.SlugReads()
        bodies = ["a" * 5000, "b" * 7000]
        for i, body in enumerate(bodies):
            reads.add(f"cmd-{i}", body, 0.5)
        self.assertEqual(reads.hexdigest(), hashlib.sha256("".join(bodies).encode()).hexdigest())
        self.assertEqual(reads.commands, ["cmd-0", "cmd-1"])
        self.assertEqual(reads.duration_s, 1.0)
        self.assertNotIn("a" * 5000, repr(vars(reads)))

    def test_a_long_join_is_clipped_at_a_boundary_and_counts_the_rest(self):
        parts = [f"gcloud compute routers get-status r{i} --region=us-central1 --project=p1" for i in range(60)]
        joined = networking_audit.joined_command(parts)
        self.assertLessEqual(len(joined), networking_audit.MAX_COMMAND_CHARS)
        self.assertRegex(joined, r"# and \d+ more read\(s\) of the same shape$")


class NothingCollectedTest(unittest.TestCase):
    def setUp(self):
        networking_audit.PROJECT_NUMBERS.clear()
        self.addCleanup(networking_audit.PROJECT_NUMBERS.clear)

    def fleet(self, run, projects=("p1",)):
        with patch.object(networking_audit, "get_target_projects", return_value=list(projects)), \
                patch.object(networking_audit, "run_cmd", side_effect=run), \
                patch("sys.stderr", new_callable=io.StringIO):
            return networking_audit.collect_fleet()

    def test_a_run_that_read_no_target_carries_a_top_level_error(self):
        denied = {phrase: (1, "PERMISSION_DENIED") for phrase in project_answers()}
        manifest = self.fleet(fake_run_cmd({"p1": denied}))
        self.assertIn("no target could be read", manifest["error"])
        self.assertFalse([e for e in manifest["clusters"] if e["outcome"] == "collected"])

    def test_a_run_with_a_collected_target_has_no_error(self):
        manifest = self.fleet(fake_run_cmd({"p1": project_answers()}))
        self.assertNotIn("error", manifest)

    def test_an_api_off_project_is_named_as_the_cause(self):
        def run(cmd, *args, **kwargs):
            return (1, "", "ERROR: Compute Engine API has not been used in project p1 before. SERVICE_DISABLED")
        manifest = self.fleet(run)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("the Compute Engine API is off in p1", manifest["error"])


class ReadsPassCommandPolicyTest(unittest.TestCase):
    def test_every_project_level_read_passes_command_policy(self):
        sys.path.insert(0, PLATFORM_SCRIPTS)
        self.addCleanup(sys.path.remove, PLATFORM_SCRIPTS)
        import command_policy

        project = "p" * 30
        argvs = [
            *networking_audit.project_commands(project).values(),
            networking_audit.router_commands(project, "nat-router", "us-central1"),
            networking_audit.router_commands(project, "nat-router", "us-central1", "nat-gw"),
        ]
        for argv in argvs:
            decision = command_policy.evaluate(argv)
            self.assertTrue(decision.allowed, f"{argv}: {decision}")


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    """The document SOP §2 tells the worker to write from the manifest passes
    `validate_findings` and `cross_check_manifest`."""

    def setUp(self):
        sys.path.insert(0, FLEET_AUDIT_SCRIPTS)
        self.addCleanup(sys.path.remove, FLEET_AUDIT_SCRIPTS)
        networking_audit.PROJECT_NUMBERS.clear()
        self.addCleanup(networking_audit.PROJECT_NUMBERS.clear)

    def document(self, manifest):
        clusters, skipped, findings = [], [], []
        for entry in manifest["clusters"]:
            if entry["outcome"] != "collected":
                skipped.append({"cluster": entry["name"], "reason": entry["error"]})
                continue
            na_slugs = {d["check"] for d in entry["checks_not_applicable"]}
            target = {
                "name": entry["name"],
                "location": entry["location"],
                "project": entry["project"],
                "checks_run": [{"check": c["check"], "command": c["command"]}
                               for c in entry["commands"] if c["check"] not in na_slugs],
                "checks_not_applicable": entry["checks_not_applicable"],
            }
            if entry.get("limitations"):
                target["limitations"] = entry["limitations"]
            clusters.append(target)
            for candidate in entry["candidates"]:
                findings.append({
                    "check": candidate["check"],
                    "severity": candidate["severity"],
                    "title": f"{candidate['check']} on {candidate['object']}",
                    "cluster": entry["name"],
                    "namespace": candidate["namespace"],
                    "object": candidate["object"],
                    "impact": candidate["impact"],
                    "evidence": {"command": candidate["command"], "excerpt": candidate["excerpt"]},
                    "recommendation": {"action": "Fix it in Terraform.", "rationale": "Because.", "risk": "Low."},
                    "remediation": {"kind": "manual"},
                })
        return {"audit": "gcp-networking-fabric-audit",
                "scope": {"clusters": clusters, "skipped": skipped}, "findings": findings}

    def test_a_collected_fleet_validates_and_cross_checks(self):
        import audit_report

        small = SUBNET_LINK.format("small")
        answers = project_answers(**{
            "subnets list": [subnet("small", "10.0.0.0/29")],
            "instances list": [
                {"name": f"vm-{i}", "networkInterfaces": [{"networkIP": f"10.0.0.{i}", "subnetwork": small}]}
                for i in (2, 3, 4)
            ],
            "firewall-rules list": [world_open(network="https://www.googleapis.com/compute/v1/projects/p1/global/networks/default")],
        })
        answers["instances list"].append(public_node())
        with patch.object(networking_audit, "get_target_projects", return_value=["p1"]), \
                patch.object(networking_audit, "run_cmd", side_effect=fake_run_cmd({"p1": answers})), \
                patch("sys.stderr", new_callable=io.StringIO):
            manifest = networking_audit.collect_fleet()
        names = sorted(e["name"] for e in manifest["clusters"])
        self.assertEqual(names, ["p1/us-central1/small", "project/p1"])
        doc = json.loads(json.dumps(self.document(manifest)))
        self.assertEqual(sorted(f["object"] for f in doc["findings"]), ["FirewallRule/allow-ssh", "Subnet/small"])
        audit_report.validate_findings(doc, "gcp-networking-fabric-audit")
        audit_report.cross_check_manifest(doc, manifest)
        self.assertEqual(audit_report.coverage_gaps(doc), [])

    def test_the_roster_is_partitioned_by_target_kind(self):
        import audit_report

        self.assertEqual(audit_report.audit_target_checks("gcp-networking-fabric-audit", "p1/us-central1/small"),
                         ("subnet-ip-exhaustion",))
        self.assertEqual(list(audit_report.audit_target_checks("gcp-networking-fabric-audit", "project/p1")),
                         PROJECT_SLUGS)



if __name__ == "__main__":
    unittest.main()
