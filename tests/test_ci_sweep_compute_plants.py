"""Tests for hack/ci_sweep_compute_plants.py (#2552).

Verifies that:
1. Resource identification strictly matches the fixed description prefix
   ("kube-agents-bench plant") with age gating rooted on the parent VPC network
   (and standalone/orphan resources).
2. Deletion order is strictly dependency-ordered:
   Addresses -> Subnets -> Networks.
3. Regional vs global addresses are correctly scoped.
4. Dry-run runs list commands and invokes no delete commands.
5. Failures (list error, delete failure) are handled and reported without unhandled crashes.
6. Boskos pool walk, stranded resets, and report generation follow repository standards.
"""

from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "hack" / "ci_sweep_compute_plants.py"

_spec = importlib.util.spec_from_file_location("ci_sweep_compute_plants", _MODULE_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"could not load {_MODULE_PATH}")
sweep = importlib.util.module_from_spec(_spec)
sys.modules["ci_sweep_compute_plants"] = sweep
_spec.loader.exec_module(sweep)


class MatchesPlantDescriptionTest(unittest.TestCase):
    def test_exact_prefix_matches(self):
        desc = "kube-agents-bench plant (networking-audit-subnet-range-exhaustion); safe to delete"
        self.assertTrue(sweep.matches_plant_description(desc))

    def test_short_prefix_matches(self):
        self.assertTrue(sweep.matches_plant_description("kube-agents-bench plant"))

    def test_whitespace_padded_prefix_matches(self):
        self.assertTrue(sweep.matches_plant_description("   kube-agents-bench plant custom   "))

    def test_unrelated_description_does_not_match(self):
        self.assertFalse(sweep.matches_plant_description("default VPC network"))
        self.assertFalse(sweep.matches_plant_description("GKE cluster network"))
        self.assertFalse(sweep.matches_plant_description("user-created plant"))

    def test_empty_or_none_does_not_match(self):
        self.assertFalse(sweep.matches_plant_description(""))
        self.assertFalse(sweep.matches_plant_description("   "))
        self.assertFalse(sweep.matches_plant_description(None))
        self.assertFalse(sweep.matches_plant_description(123))  # type: ignore


class ResourceNameTest(unittest.TestCase):
    def test_extract_from_gcp_url(self):
        self.assertEqual(
            sweep.resource_name("https://www.googleapis.com/compute/v1/projects/p/global/networks/vpc-1"),
            "vpc-1",
        )
        self.assertEqual(
            sweep.resource_name("projects/p/regions/us-west4/subnetworks/sub-1"),
            "sub-1",
        )

    def test_bare_name_and_empty(self):
        self.assertEqual(sweep.resource_name("net-1"), "net-1")
        self.assertEqual(sweep.resource_name(""), "")
        self.assertEqual(sweep.resource_name(None), "")


class TimestampAgeTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)

    def test_parse_timestamp_formats(self):
        # UTC Z format
        dt1 = sweep.parse_timestamp("2026-10-07T10:00:00Z")
        self.assertIsNotNone(dt1)
        self.assertEqual(dt1.tzinfo, timezone.utc)

        # Offset format
        dt2 = sweep.parse_timestamp("2026-10-07T05:00:00-07:00")
        self.assertIsNotNone(dt2)
        self.assertEqual(dt2.astimezone(timezone.utc), datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc))

        # Invalid formats
        self.assertIsNone(sweep.parse_timestamp("not-a-timestamp"))
        self.assertIsNone(sweep.parse_timestamp(""))
        self.assertIsNone(sweep.parse_timestamp(None))

    def test_is_older_than_threshold(self):
        # 5 hours ago, threshold 4 hours -> True
        five_hours_ago = (self.now - timedelta(hours=5)).isoformat()
        self.assertTrue(sweep.is_older_than(five_hours_ago, 4.0, now=self.now))

        # 1 hour ago, threshold 4 hours -> False (active run protection)
        one_hour_ago = (self.now - timedelta(hours=1)).isoformat()
        self.assertFalse(sweep.is_older_than(one_hour_ago, 4.0, now=self.now))

        # Exactly 4 hours ago -> True
        four_hours_ago = (self.now - timedelta(hours=4)).isoformat()
        self.assertTrue(sweep.is_older_than(four_hours_ago, 4.0, now=self.now))

        # Unparseable or missing timestamp -> True (treated as old so leaks are swept)
        self.assertTrue(sweep.is_older_than("invalid", 4.0, now=self.now))
        self.assertTrue(sweep.is_older_than(None, 4.0, now=self.now))
        self.assertTrue(sweep.is_older_than("", 4.0, now=self.now))


class PoolProjectsTest(unittest.TestCase):
    def test_extracts_mapped_projects(self):
        script_content = """#!/usr/bin/env bash
gitops_repo_for_project() {
  case "$1" in
    kube-agents-evals) echo "gke-agentic/kube-agents-evals-infra" ;;
    kube-agents-evals-2) echo "gke-agentic/kube-agents-evals-2-infra" ;;
  esac
}
"""
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
            tf.write(script_content)
            temp_path = tf.name
        try:
            projects = sweep.pool_projects(temp_path)
            self.assertEqual(projects, {"kube-agents-evals", "kube-agents-evals-2"})
        finally:
            pathlib.Path(temp_path).unlink(missing_ok=True)

    def test_missing_function_raises_sweep_error(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
            tf.write("#!/usr/bin/env bash\necho hello\n")
            temp_path = tf.name
        try:
            with self.assertRaises(sweep.SweepError):
                sweep.pool_projects(temp_path)
        finally:
            pathlib.Path(temp_path).unlink(missing_ok=True)


class ListComputeResourcesTest(unittest.TestCase):
    def test_successful_list(self):
        fake_runner = mock.Mock()
        fake_runner.return_value = mock.Mock(
            returncode=0,
            stdout=json.dumps([{"name": "res-1"}]),
            stderr="",
        )
        items = sweep.list_compute_resources("test-proj", "addresses", runner=fake_runner)
        self.assertEqual(items, [{"name": "res-1"}])
        fake_runner.assert_called_once_with(
            ["gcloud", "compute", "addresses", "list", "--project=test-proj", "--format=json"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_list_failure_raises_sweep_error(self):
        fake_runner = mock.Mock()
        fake_runner.return_value = mock.Mock(
            returncode=1,
            stdout="",
            stderr="permission denied",
        )
        with self.assertRaises(sweep.SweepError) as ctx:
            sweep.list_compute_resources("test-proj", "networks", runner=fake_runner)
        self.assertIn("could not list networks in test-proj: permission denied", str(ctx.exception))

    def test_non_json_output_raises_sweep_error(self):
        fake_runner = mock.Mock()
        fake_runner.return_value = mock.Mock(
            returncode=0,
            stdout="<html>502 Bad Gateway</html>",
            stderr="",
        )
        with self.assertRaises(sweep.SweepError) as ctx:
            sweep.list_compute_resources("test-proj", "addresses", runner=fake_runner)
        self.assertIn("is not JSON", str(ctx.exception))


class SweepProjectTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        self.old_ts = (self.now - timedelta(hours=6)).isoformat()
        self.recent_ts = (self.now - timedelta(hours=1)).isoformat()
        self.plant_desc = "kube-agents-bench plant (subnet-range-exhaustion); safe to delete"

    def test_dependency_order_addresses_then_subnets_then_networks(self):
        """Proves addresses are deleted first, then subnets, then networks."""
        commands_run = []

        def mock_runner(cmd, capture_output=True, text=True, check=False):
            commands_run.append(cmd)
            # Handle list calls
            if "list" in cmd:
                if "addresses" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{
                            "name": "bench-addr-1",
                            "description": self.plant_desc,
                            "creationTimestamp": self.old_ts,
                            "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4",
                        }]),
                        stderr="",
                    )
                if "subnets" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{
                            "name": "bench-subnet-1",
                            "description": self.plant_desc,
                            "creationTimestamp": self.old_ts,
                            "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4",
                        }]),
                        stderr="",
                    )
                if "networks" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{
                            "name": "bench-vpc-1",
                            "description": self.plant_desc,
                            "creationTimestamp": self.old_ts,
                        }]),
                        stderr="",
                    )
            # Handle delete calls
            return mock.Mock(returncode=0, stdout="", stderr="")

        res = sweep.sweep_project("my-project", max_age_hours=4.0, runner=mock_runner, now=self.now)
        self.assertEqual(res["addresses"], ["bench-addr-1"])
        self.assertEqual(res["subnets"], ["bench-subnet-1"])
        self.assertEqual(res["networks"], ["bench-vpc-1"])

        # Extract only delete commands
        delete_cmds = [cmd for cmd in commands_run if "delete" in cmd]
        self.assertEqual(len(delete_cmds), 3)

        # 1st delete must be address
        self.assertEqual(delete_cmds[0], [
            "gcloud", "compute", "addresses", "delete", "bench-addr-1",
            "--project=my-project", "--region=us-west4", "--quiet",
        ])
        # 2nd delete must be subnet
        self.assertEqual(delete_cmds[1], [
            "gcloud", "compute", "networks", "subnets", "delete", "bench-subnet-1",
            "--project=my-project", "--region=us-west4", "--quiet",
        ])
        # 3rd delete must be network
        self.assertEqual(delete_cmds[2], [
            "gcloud", "compute", "networks", "delete", "bench-vpc-1",
            "--project=my-project", "--quiet",
        ])

    def test_global_address_command_shape(self):
        commands_run = []

        def mock_runner(cmd, capture_output=True, text=True, check=False):
            commands_run.append(cmd)
            if "list" in cmd and "addresses" in cmd:
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps([{
                        "name": "bench-global-addr",
                        "description": self.plant_desc,
                        "creationTimestamp": self.old_ts,
                        # No region field
                    }]),
                    stderr="",
                )
            if "list" in cmd:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        res = sweep.sweep_project("my-project", runner=mock_runner, now=self.now)
        self.assertEqual(res["addresses"], ["bench-global-addr"])

        delete_cmds = [cmd for cmd in commands_run if "delete" in cmd]
        self.assertEqual(len(delete_cmds), 1)
        self.assertEqual(delete_cmds[0], [
            "gcloud", "compute", "addresses", "delete", "bench-global-addr",
            "--project=my-project", "--global", "--quiet",
        ])

    def test_recent_or_unmatched_resources_are_not_touched(self):
        commands_run = []

        def mock_runner(cmd, capture_output=True, text=True, check=False):
            commands_run.append(cmd)
            if "list" in cmd and "addresses" in cmd:
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps([
                        # Recent plant (1h old) - should NOT be deleted (active eval protection)
                        {"name": "recent-addr", "description": self.plant_desc, "creationTimestamp": self.recent_ts},
                        # Old non-plant (6h old) - should NOT be deleted
                        {"name": "prod-addr", "description": "production IP", "creationTimestamp": self.old_ts},
                    ]),
                    stderr="",
                )
            if "list" in cmd:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        res = sweep.sweep_project("my-project", max_age_hours=4.0, runner=mock_runner, now=self.now)
        self.assertEqual(res["addresses"], [])
        self.assertEqual(res["subnets"], [])
        self.assertEqual(res["networks"], [])

        delete_cmds = [cmd for cmd in commands_run if "delete" in cmd]
        self.assertEqual(len(delete_cmds), 0)

    def test_plant_resource_with_unparseable_or_missing_timestamp_treated_as_old(self):
        commands_run = []

        def mock_runner(cmd, capture_output=True, text=True, check=False):
            commands_run.append(cmd)
            if "list" in cmd and "networks" in cmd and "subnets" not in cmd:
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps([{
                        "name": "bench-vpc-notime",
                        "description": self.plant_desc,
                        "creationTimestamp": "",
                    }]),
                    stderr="",
                )
            if "list" in cmd and "subnets" in cmd:
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps([{
                        "name": "bench-sub-notime",
                        "description": self.plant_desc,
                        "creationTimestamp": None,
                        "network": "https://www.googleapis.com/compute/v1/projects/my-project/global/networks/bench-vpc-notime",
                        "region": "https://www.googleapis.com/compute/v1/projects/my-project/regions/us-west4",
                    }]),
                    stderr="",
                )
            if "list" in cmd and "addresses" in cmd:
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps([{
                        "name": "bench-addr-notime",
                        "description": self.plant_desc,
                        "creationTimestamp": "garbage-date",
                        "subnetwork": "https://www.googleapis.com/compute/v1/projects/my-project/regions/us-west4/subnetworks/bench-sub-notime",
                        "region": "https://www.googleapis.com/compute/v1/projects/my-project/regions/us-west4",
                    }]),
                    stderr="",
                )
            return mock.Mock(returncode=0, stdout="", stderr="")

        res = sweep.sweep_project("my-project", max_age_hours=4.0, runner=mock_runner, now=self.now)
        self.assertEqual(res["addresses"], ["bench-addr-notime"])
        self.assertEqual(res["subnets"], ["bench-sub-notime"])
        self.assertEqual(res["networks"], ["bench-vpc-notime"])

    def test_dry_run_executes_no_deletes(self):
        commands_run = []

        def mock_runner(cmd, capture_output=True, text=True, check=False):
            commands_run.append(cmd)
            if "list" in cmd and "networks" in cmd and "subnets" not in cmd:
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps([{
                        "name": "bench-net-1",
                        "description": self.plant_desc,
                        "creationTimestamp": self.old_ts,
                    }]),
                    stderr="",
                )
            if "list" in cmd:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        res = sweep.sweep_project("my-project", dry_run=True, runner=mock_runner, now=self.now)
        self.assertEqual(res["networks"], ["bench-net-1"])

        delete_cmds = [cmd for cmd in commands_run if "delete" in cmd]
        self.assertEqual(len(delete_cmds), 0)

    def test_dependency_chain_age_skew_deletes_all_resources(self):
        """Proves that younger addresses on an older plant network are swept together."""
        commands_run = []

        def mock_runner(cmd, capture_output=True, text=True, check=False):
            commands_run.append(cmd)
            if "list" in cmd:
                if "networks" in cmd and "subnets" not in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{
                            "name": "bench-vpc-skew",
                            "description": self.plant_desc,
                            "creationTimestamp": self.old_ts,
                        }]),
                        stderr="",
                    )
                if "subnets" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{
                            "name": "bench-subnet-skew",
                            "description": self.plant_desc,
                            # Created 1 hour ago (younger than 4h threshold, but belonging to old network)
                            "creationTimestamp": self.recent_ts,
                            "network": "https://www.googleapis.com/compute/v1/projects/p/global/networks/bench-vpc-skew",
                            "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4",
                        }]),
                        stderr="",
                    )
                if "addresses" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{
                            "name": "bench-addr-skew",
                            "description": self.plant_desc,
                            # Created 1 hour ago (younger than 4h threshold, but belonging to old network)
                            "creationTimestamp": self.recent_ts,
                            "subnetwork": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4/subnetworks/bench-subnet-skew",
                            "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4",
                        }]),
                        stderr="",
                    )
            return mock.Mock(returncode=0, stdout="", stderr="")

        res = sweep.sweep_project("my-project", max_age_hours=4.0, runner=mock_runner, now=self.now)
        self.assertEqual(res["addresses"], ["bench-addr-skew"])
        self.assertEqual(res["subnets"], ["bench-subnet-skew"])
        self.assertEqual(res["networks"], ["bench-vpc-skew"])

        delete_cmds = [cmd for cmd in commands_run if "delete" in cmd]
        self.assertEqual(len(delete_cmds), 3)
        self.assertIn("bench-addr-skew", delete_cmds[0])
        self.assertIn("bench-subnet-skew", delete_cmds[1])
        self.assertIn("bench-vpc-skew", delete_cmds[2])

    def test_active_network_protects_child_subnets_and_addresses(self):
        """Proves that a recent plant network keeps its subnets and addresses intact."""
        commands_run = []

        def mock_runner(cmd, capture_output=True, text=True, check=False):
            commands_run.append(cmd)
            if "list" in cmd:
                if "networks" in cmd and "subnets" not in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{
                            "name": "active-vpc",
                            "description": self.plant_desc,
                            "creationTimestamp": self.recent_ts,
                        }]),
                        stderr="",
                    )
                if "subnets" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{
                            "name": "active-subnet",
                            "description": self.plant_desc,
                            # Missing/None timestamp: would be treated as old if evaluated alone,
                            # but protected because parent network is recent.
                            "creationTimestamp": None,
                            "network": "https://www.googleapis.com/compute/v1/projects/p/global/networks/active-vpc",
                            "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4",
                        }]),
                        stderr="",
                    )
                if "addresses" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([
                            {
                                "name": "active-addr",
                                "description": self.plant_desc,
                                "creationTimestamp": None,
                                "subnetwork": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4/subnetworks/active-subnet",
                            },
                            {
                                "name": "active-addr-net",
                                "description": self.plant_desc,
                                "creationTimestamp": None,
                                "network": "https://www.googleapis.com/compute/v1/projects/p/global/networks/active-vpc",
                            },
                        ]),
                        stderr="",
                    )
            return mock.Mock(returncode=0, stdout="", stderr="")

        res = sweep.sweep_project("my-project", max_age_hours=4.0, runner=mock_runner, now=self.now)
        self.assertEqual(res["addresses"], [])
        self.assertEqual(res["subnets"], [])
        self.assertEqual(res["networks"], [])
        delete_cmds = [cmd for cmd in commands_run if "delete" in cmd]
        self.assertEqual(len(delete_cmds), 0)

    def test_addresses_differentiated_by_region_for_same_subnet_name(self):
        """Proves that addresses matching a subnet name in a different region on an active VPC are preserved."""
        commands_run = []

        def mock_runner(cmd, capture_output=True, text=True, check=False):
            commands_run.append(cmd)
            if "list" in cmd:
                if "networks" in cmd and "subnets" not in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([
                            {"name": "bench-vpc-old", "description": self.plant_desc, "creationTimestamp": self.old_ts},
                            {"name": "bench-vpc-active", "description": self.plant_desc, "creationTimestamp": self.recent_ts},
                        ]),
                        stderr="",
                    )
                if "subnets" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([
                            {
                                "name": "bench-sub",
                                "description": self.plant_desc,
                                "creationTimestamp": self.old_ts,
                                "network": "https://www.googleapis.com/compute/v1/projects/p/global/networks/bench-vpc-old",
                                "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4",
                            },
                            {
                                "name": "bench-sub",
                                "description": self.plant_desc,
                                "creationTimestamp": self.recent_ts,
                                "network": "https://www.googleapis.com/compute/v1/projects/p/global/networks/bench-vpc-active",
                                "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-east1",
                            },
                        ]),
                        stderr="",
                    )
                if "addresses" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([
                            {
                                "name": "bench-addr-west",
                                "description": self.plant_desc,
                                "creationTimestamp": self.recent_ts,
                                "subnetwork": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4/subnetworks/bench-sub",
                                "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4",
                            },
                            {
                                "name": "bench-addr-east",
                                "description": self.plant_desc,
                                "creationTimestamp": self.recent_ts,
                                "subnetwork": "https://www.googleapis.com/compute/v1/projects/p/regions/us-east1/subnetworks/bench-sub",
                                "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-east1",
                            },
                        ]),
                        stderr="",
                    )
            return mock.Mock(returncode=0, stdout="", stderr="")

        res = sweep.sweep_project("my-project", max_age_hours=4.0, runner=mock_runner, now=self.now)
        self.assertEqual(res["addresses"], ["bench-addr-west"])
        self.assertEqual(res["subnets"], ["bench-sub"])
        self.assertEqual(res["networks"], ["bench-vpc-old"])

        delete_cmds = [cmd for cmd in commands_run if "delete" in cmd]
        deleted_names = [cmd[4] for cmd in delete_cmds]
        self.assertIn("bench-addr-west", deleted_names)
        self.assertNotIn("bench-addr-east", deleted_names)

    def test_aged_plant_resources_under_non_plant_parent_are_deleted(self):
        """Proves that stamped subnets/addresses under non-plant parents (e.g. default VPC) are deleted by age."""
        commands_run = []

        def mock_runner(cmd, capture_output=True, text=True, check=False):
            commands_run.append(cmd)
            if "list" in cmd:
                if "networks" in cmd and "subnets" not in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{
                            "name": "default",
                            "description": "Default network for project",
                            "creationTimestamp": self.old_ts,
                        }]),
                        stderr="",
                    )
                if "subnets" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([
                            {
                                "name": "bench-sub",
                                "description": self.plant_desc,
                                "creationTimestamp": self.old_ts,
                                "network": "https://www.googleapis.com/compute/v1/projects/p/global/networks/default",
                                "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4",
                            },
                            {
                                "name": "standing-sub",
                                "description": "Standing non-plant subnet",
                                "creationTimestamp": self.old_ts,
                                "network": "https://www.googleapis.com/compute/v1/projects/p/global/networks/default",
                                "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4",
                            },
                        ]),
                        stderr="",
                    )
                if "addresses" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{
                            "name": "bench-addr-in-standing-sub",
                            "description": self.plant_desc,
                            "creationTimestamp": self.old_ts,
                            "subnetwork": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4/subnetworks/standing-sub",
                            "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-west4",
                        }]),
                        stderr="",
                    )
            return mock.Mock(returncode=0, stdout="", stderr="")

        res = sweep.sweep_project("my-project", max_age_hours=4.0, runner=mock_runner, now=self.now)
        self.assertEqual(res["addresses"], ["bench-addr-in-standing-sub"])
        self.assertEqual(res["subnets"], ["bench-sub"])
        self.assertEqual(res["networks"], [])

        delete_cmds = [cmd for cmd in commands_run if "delete" in cmd]
        self.assertTrue(any("bench-addr-in-standing-sub" in cmd for cmd in delete_cmds))
        self.assertTrue(any("bench-sub" in cmd for cmd in delete_cmds))
        self.assertFalse(any("default" in cmd for cmd in delete_cmds))
        self.assertFalse(any("standing-sub" in cmd for cmd in delete_cmds))

    def test_sweep_project_terminated_attaches_partial_deletions(self):
        """Proves that a SIGTERM mid-sweep attaches accumulated deletions to Terminated."""
        def mock_runner(cmd, capture_output=True, text=True, check=False):
            if "list" in cmd:
                if "networks" in cmd and "subnets" not in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{"name": "bench-vpc-1", "description": self.plant_desc, "creationTimestamp": self.old_ts}]),
                        stderr="",
                    )
                if "subnets" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{
                            "name": "bench-sub-1",
                            "description": self.plant_desc,
                            "creationTimestamp": self.old_ts,
                            "network": "bench-vpc-1",
                            "region": "us-west4",
                        }]),
                        stderr="",
                    )
                if "addresses" in cmd:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps([{
                            "name": "bench-addr-1",
                            "description": self.plant_desc,
                            "creationTimestamp": self.old_ts,
                            "region": "us-west4",
                        }]),
                        stderr="",
                    )
            if "addresses" in cmd and "delete" in cmd:
                return mock.Mock(returncode=0, stdout="", stderr="")
            if "subnets" in cmd and "delete" in cmd:
                raise sweep.Terminated("signal 15")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with self.assertRaises(sweep.Terminated) as ctx:
            sweep.sweep_project("my-project", max_age_hours=4.0, runner=mock_runner, now=self.now)

        del_counts = getattr(ctx.exception, "deleted", None)
        self.assertIsNotNone(del_counts)
        assert del_counts is not None
        self.assertEqual(del_counts["addresses"], ["bench-addr-1"])
        self.assertEqual(del_counts["subnets"], [])
        self.assertEqual(del_counts["networks"], [])

    def test_delete_failure_raises_sweep_error_at_end(self):
        def mock_runner(cmd, capture_output=True, text=True, check=False):
            if "list" in cmd and "addresses" in cmd:
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps([{
                        "name": "locked-addr",
                        "description": self.plant_desc,
                        "creationTimestamp": self.old_ts,
                    }]),
                    stderr="",
                )
            if "list" in cmd:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            if "delete" in cmd:
                return mock.Mock(returncode=1, stdout="", stderr="resourceInUseByAnotherResource")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with self.assertRaises(sweep.SweepError) as ctx:
            sweep.sweep_project("my-project", runner=mock_runner, now=self.now)
        self.assertIn("locked-addr", str(ctx.exception))
        self.assertIn("resourceInUseByAnotherResource", str(ctx.exception))

    def test_partial_deletion_failure_attaches_deleted_resources_to_sweep_error(self):
        def mock_runner(cmd, capture_output=True, text=True, check=False):
            if "list" in cmd and "addresses" in cmd:
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps([{
                        "name": "addr-1",
                        "description": self.plant_desc,
                        "creationTimestamp": self.old_ts,
                    }]),
                    stderr="",
                )
            if "list" in cmd and "networks" in cmd and "subnets" not in cmd:
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps([{
                        "name": "net-1",
                        "description": self.plant_desc,
                        "creationTimestamp": self.old_ts,
                    }]),
                    stderr="",
                )
            if "list" in cmd:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            if "delete" in cmd and "addresses" in cmd:
                return mock.Mock(returncode=0, stdout="", stderr="")
            if "delete" in cmd and "networks" in cmd:
                return mock.Mock(returncode=1, stdout="", stderr="networkInUse")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with self.assertRaises(sweep.SweepError) as ctx:
            sweep.sweep_project("my-project", runner=mock_runner, now=self.now)
        self.assertIn("net-1 (networkInUse)", str(ctx.exception))
        self.assertEqual(ctx.exception.deleted["addresses"], ["addr-1"])
        self.assertEqual(ctx.exception.deleted["networks"], [])


class SweepPoolTest(unittest.TestCase):
    def test_sweep_pool_walks_boskos_and_cleans(self):
        visited = []

        def fake_walk(server, owner, state, limit, visit_callback, heartbeat=True, release_failures=None):
            self.assertEqual(state, "cleaning")
            self.assertEqual(owner, "test-owner")
            self.assertTrue(heartbeat)
            visit_callback("proj-1")
            visit_callback("proj-unmapped")

        def fake_sweep_project(name, max_age_hours=4.0, dry_run=False, runner=None, now=None):
            visited.append(name)
            return {"addresses": ["addr-1"], "subnets": [], "networks": ["net-1"]}

        with (
            mock.patch.object(sweep.boskos_pool, "walk", side_effect=fake_walk),
            mock.patch.object(sweep, "boskos_reset_stranded") as mock_reset,
            mock.patch.object(sweep, "sweep_project", side_effect=fake_sweep_project),
        ):
            report = {}
            deleted, failures, unmapped = sweep.sweep_pool(
                "http://fake-boskos",
                "test-owner",
                4.0,
                {"proj-1"},
                report=report,
            )
            mock_reset.assert_called_once_with("http://fake-boskos")
            self.assertEqual(visited, ["proj-1"])
            self.assertEqual(unmapped, ["proj-unmapped"])
            self.assertIn("proj-1", deleted)
            self.assertEqual(deleted["proj-1"]["addresses"], 1)
            self.assertEqual(deleted["proj-1"]["networks"], 1)

    def test_sweep_pool_partial_failure_retains_deleted_in_report(self):
        def fake_walk(server, owner, state, limit, visit_callback, heartbeat=True, release_failures=None):
            visit_callback("proj-1")

        def fake_sweep_project(name, max_age_hours=4.0, dry_run=False, runner=None, now=None):
            raise sweep.SweepError(
                "proj-1: networks: net-1 (inUse)",
                deleted={"addresses": ["addr-1"], "subnets": [], "networks": []},
            )

        with (
            mock.patch.object(sweep.boskos_pool, "walk", side_effect=fake_walk),
            mock.patch.object(sweep, "boskos_reset_stranded"),
            mock.patch.object(sweep, "sweep_project", side_effect=fake_sweep_project),
        ):
            report = {}
            deleted, failures, unmapped = sweep.sweep_pool(
                "http://fake-boskos",
                "test-owner",
                4.0,
                {"proj-1"},
                report=report,
            )
            self.assertIn("proj-1", deleted)
            self.assertEqual(deleted["proj-1"]["addresses"], 1)
            self.assertEqual(deleted["proj-1"]["subnets"], 0)
            self.assertEqual(deleted["proj-1"]["networks"], 0)
            self.assertIn("proj-1", failures)
            self.assertIn("net-1 (inUse)", failures["proj-1"])

    def test_sweep_pool_termination_records_ended_early_and_re_raises(self):
        def fake_walk(server, owner, state, limit, visit_callback, heartbeat=True, release_failures=None):
            visit_callback("proj-1")

        def fake_sweep_project(name, max_age_hours=4.0, dry_run=False, runner=None, now=None):
            raise sweep.Terminated("signal 15")

        with (
            mock.patch.object(sweep.boskos_pool, "walk", side_effect=fake_walk),
            mock.patch.object(sweep, "boskos_reset_stranded"),
            mock.patch.object(sweep, "sweep_project", side_effect=fake_sweep_project),
        ):
            report = {}
            with self.assertRaises(sweep.Terminated):
                sweep.sweep_pool(
                    "http://fake-boskos",
                    "test-owner",
                    4.0,
                    {"proj-1"},
                    report=report,
                )
            self.assertEqual(report.get("ended_early"), "signal 15")
            self.assertEqual(report.get("failures", {}).get("proj-1"), "signal 15")

    def test_sweep_pool_termination_retains_partial_deletions_in_report(self):
        def fake_walk(server, owner, state, limit, visit_callback, heartbeat=True, release_failures=None):
            visit_callback("proj-1")

        def fake_sweep_project(name, max_age_hours=4.0, dry_run=False, runner=None, now=None):
            term = sweep.Terminated("signal 15")
            setattr(term, "deleted", {"addresses": ["addr-1"], "subnets": [], "networks": []})
            raise term

        with (
            mock.patch.object(sweep.boskos_pool, "walk", side_effect=fake_walk),
            mock.patch.object(sweep, "boskos_reset_stranded"),
            mock.patch.object(sweep, "sweep_project", side_effect=fake_sweep_project),
        ):
            report = {}
            with self.assertRaises(sweep.Terminated):
                sweep.sweep_pool(
                    "http://fake-boskos",
                    "test-owner",
                    4.0,
                    {"proj-1"},
                    report=report,
                )
            self.assertEqual(report.get("ended_early"), "signal 15")
            self.assertEqual(report.get("deleted", {}).get("proj-1", {}).get("addresses"), 1)
            self.assertEqual(report.get("failures", {}).get("proj-1"), "signal 15")

    def test_sweep_pool_release_failure_joins_with_sweep_failure_without_prefix_doubling(self):
        def fake_walk(server, owner, state, limit, visit_callback, heartbeat=True, release_failures=None):
            visit_callback("proj-1")
            if release_failures is not None:
                before = release_failures.get("proj-1")
                release_failures["proj-1"] = ("%s; " % before if before else "") + "release failed: HTTP 500"

        def fake_sweep_project(name, max_age_hours=4.0, dry_run=False, runner=None, now=None):
            raise sweep.SweepError(
                "proj-1: networks: net-1 (inUse)",
                deleted={"addresses": ["addr-1"], "subnets": [], "networks": []},
            )

        with (
            mock.patch.object(sweep.boskos_pool, "walk", side_effect=fake_walk),
            mock.patch.object(sweep, "boskos_reset_stranded"),
            mock.patch.object(sweep, "sweep_project", side_effect=fake_sweep_project),
        ):
            report = {}
            deleted, failures, unmapped = sweep.sweep_pool(
                "http://fake-boskos",
                "test-owner",
                4.0,
                {"proj-1"},
                report=report,
            )
            self.assertIn("proj-1", failures)
            self.assertEqual(
                failures["proj-1"],
                "proj-1: networks: net-1 (inUse); release failed: HTTP 500",
            )
            self.assertNotIn("release failed: release failed:", failures["proj-1"])


class WriteReportTest(unittest.TestCase):
    def test_report_structure(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            report_path = tf.name

        try:
            args = mock.Mock(project="p1", dry_run=False, max_age_hours=4.0)
            run = {
                "deleted": {"p1": {"addresses": 3, "subnets": 1, "networks": 1}},
                "failures": {},
                "unmapped": [],
                "skipped": [],
                "ended_early": None,
            }
            sweep.write_report(report_path, args, run, 0, None, 1000.0)

            data = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["mode"], "project")
            self.assertFalse(data["dry_run"])
            self.assertEqual(data["max_age_hours"], 4.0)
            self.assertEqual(data["exit"], "ok")
            self.assertEqual(data["exit_code"], 0)
            self.assertIsNone(data["error"])
            self.assertIsNone(data["ended_early"])
            self.assertEqual(data["deleted"]["p1"]["addresses"], 3)
        finally:
            pathlib.Path(report_path).unlink(missing_ok=True)


class MainCliTest(unittest.TestCase):
    def setUp(self):
        super().setUp()
        # No --report and no ARTIFACTS from the shell: a main() run here writes no file.
        env = {k: v for k, v in sweep.os.environ.items() if k != sweep.ARTIFACTS_ENV}
        env_patch = mock.patch.dict(sweep.os.environ, env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self._orig_signals = {
            sig: sweep.signal.getsignal(sig)
            for sig in sweep.boskos_pool.TERMINATION_SIGNALS
        }
        # Default Boskos mocks so CLI tests don't make network requests
        patch_acquire = mock.patch.object(
            sweep.boskos_pool, "acquire", side_effect=lambda server, owner, state, name=None: name or "p1"
        )
        patch_acquire.start()
        self.addCleanup(patch_acquire.stop)
        patch_release = mock.patch.object(sweep.boskos_pool, "release_settled")
        patch_release.start()
        self.addCleanup(patch_release.stop)
        patch_reset = mock.patch.object(sweep, "boskos_reset_stranded")
        patch_reset.start()
        self.addCleanup(patch_reset.stop)

    def tearDown(self):
        for sig, handler in self._orig_signals.items():
            sweep.signal.signal(sig, handler)
        super().tearDown()

    def test_main_without_report_or_artifacts_writes_no_file(self):
        """A run without --report and without ARTIFACTS writes no report file."""
        with (
            mock.patch.object(sweep, "sweep_project", return_value={"addresses": [], "subnets": [], "networks": []}),
            mock.patch.object(sweep, "write_report") as mock_write,
        ):
            code = sweep.main(["--project", "p1"])
            self.assertEqual(code, 0)
            mock_write.assert_called_once_with(None, mock.ANY, mock.ANY, 0, None, mock.ANY)

    def test_signals_installed(self):
        with (
            mock.patch.object(sweep.signal, "signal") as mock_signal,
            mock.patch.object(sweep, "sweep_project", return_value={"addresses": [], "subnets": [], "networks": []}),
        ):
            sweep.main(["--project", "p1"])
            installed = {call.args[0]: call.args[1] for call in mock_signal.call_args_list}
            for sig in sweep.boskos_pool.TERMINATION_SIGNALS:
                self.assertIs(installed.get(sig), sweep.boskos_pool.terminate)

    def test_pool_walk_termination_releases_held_project_and_exits_143(self):
        """Proves that a termination during pool walk releases the held project and exits 143."""
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            report_path = tf.name

        try:
            with (
                mock.patch.object(sweep, "pool_projects", return_value={"p1"}),
                mock.patch.object(sweep, "boskos_reset_stranded"),
                mock.patch.object(sweep.boskos_pool, "acquire", return_value="p1"),
                mock.patch.object(sweep.boskos_pool, "release_settled") as mock_release_settled,
                mock.patch.object(sweep, "sweep_project", side_effect=sweep.Terminated("signal 15")),
            ):
                code = sweep.main(["--pool", "--report", report_path])
                self.assertEqual(code, sweep.TERMINATED_EXIT_CODE)
                mock_release_settled.assert_called_once()
                self.assertEqual(mock_release_settled.call_args[0][2], "p1")
                data = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
                self.assertEqual(data["exit"], "terminated")
                self.assertEqual(data["exit_code"], sweep.TERMINATED_EXIT_CODE)
                self.assertEqual(data["ended_early"], "signal 15")
                self.assertEqual(data["failures"]["p1"], "signal 15")
        finally:
            pathlib.Path(report_path).unlink(missing_ok=True)

    def test_boskos_env_defaults(self):
        env = {"BOSKOS_SERVER": "http://env-boskos:8888", "BOSKOS_OWNER": "env-owner-user"}
        with (
            mock.patch.dict(sweep.os.environ, env),
            mock.patch.object(sweep, "pool_projects", return_value={"p1"}),
            mock.patch.object(sweep, "sweep_pool", return_value=({}, {}, [])) as mock_pool,
        ):
            sweep.main(["--pool"])
            mock_pool.assert_called_once()
            args = mock_pool.call_args.args
            self.assertEqual(args[0], "http://env-boskos:8888")
            self.assertEqual(args[1], "env-owner-user")

    def test_termination_returns_143_and_writes_report(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            report_path = tf.name

        try:
            with (
                mock.patch.object(sweep, "pool_projects", return_value={"p1"}),
                mock.patch.object(sweep, "sweep_pool", side_effect=sweep.Terminated("signal 15")),
            ):
                code = sweep.main(["--pool", "--report", report_path])
                self.assertEqual(code, sweep.TERMINATED_EXIT_CODE)
                data = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
                self.assertEqual(data["exit"], "terminated")
                self.assertEqual(data["exit_code"], sweep.TERMINATED_EXIT_CODE)
                self.assertIn("signal 15", data["error"])
        finally:
            pathlib.Path(report_path).unlink(missing_ok=True)

    def test_project_partial_failure_records_deleted_in_report(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            report_path = tf.name

        try:
            err = sweep.SweepError(
                "p1: networks: net-1 (inUse)",
                deleted={"addresses": ["addr-1"], "subnets": [], "networks": []},
            )
            with mock.patch.object(sweep, "sweep_project", side_effect=err):
                code = sweep.main(["--project", "p1", "--report", report_path])
                self.assertEqual(code, 1)
                data = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
                self.assertEqual(data["exit"], "failed")
                self.assertEqual(data["deleted"]["p1"]["addresses"], 1)
                self.assertEqual(data["failures"]["p1"], str(err))
        finally:
            pathlib.Path(report_path).unlink(missing_ok=True)

    def test_project_mode_termination_records_partial_deletions_in_report(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            report_path = tf.name

        try:
            term = sweep.Terminated("signal 15")
            setattr(term, "deleted", {"addresses": ["addr-1"], "subnets": [], "networks": []})
            with mock.patch.object(sweep, "sweep_project", side_effect=term):
                code = sweep.main(["--project", "p1", "--report", report_path])
                self.assertEqual(code, sweep.TERMINATED_EXIT_CODE)
                data = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
                self.assertEqual(data["exit"], "terminated")
                self.assertEqual(data["deleted"]["p1"]["addresses"], 1)
                self.assertEqual(data["failures"]["p1"], "signal 15")
        finally:
            pathlib.Path(report_path).unlink(missing_ok=True)

    def test_project_mode_invocation(self):
        with mock.patch.object(sweep, "sweep_project", return_value={"addresses": [], "subnets": [], "networks": []}) as mock_sp:
            code = sweep.main(["--project", "p1", "--dry-run", "--max-age-hours", "2.5"])
            self.assertEqual(code, 0)
            mock_sp.assert_called_once_with("p1", max_age_hours=2.5, dry_run=True)

    def test_project_mode_not_free_in_boskos_refuses(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            report_path = tf.name

        try:
            with (
                mock.patch.object(sweep.boskos_pool, "acquire", return_value=None),
                mock.patch.object(sweep, "sweep_project") as mock_sp,
            ):
                code = sweep.main(["--project", "p1", "--report", report_path])
                self.assertEqual(code, 1)
                self.assertFalse(mock_sp.called)
                data = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
                self.assertEqual(data["exit"], "failed")
                self.assertEqual(data["failures"]["p1"], "project p1 is not free in Boskos (leased or busy)")
        finally:
            pathlib.Path(report_path).unlink(missing_ok=True)

    def test_project_mode_acquires_by_name_and_releases(self):
        with (
            mock.patch.object(sweep.boskos_pool, "acquire", return_value="p1") as mock_acquire,
            mock.patch.object(sweep.boskos_pool, "release_settled") as mock_release,
            mock.patch.object(sweep, "sweep_project", return_value={"addresses": [], "subnets": [], "networks": []}),
        ):
            code = sweep.main(["--project", "p1"])
            self.assertEqual(code, 0)
            mock_acquire.assert_called_once()
            self.assertEqual(mock_acquire.call_args.kwargs.get("name"), "p1")
            mock_release.assert_called_once()
            self.assertEqual(mock_release.call_args.args[2], "p1")

    def test_project_mode_release_failure_recorded_in_report(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            report_path = tf.name

        try:
            err = sweep.boskos_pool.BoskosError("release rejected")
            with (
                mock.patch.object(sweep.boskos_pool, "acquire", return_value="p1"),
                mock.patch.object(sweep.boskos_pool, "release_settled", side_effect=err),
                mock.patch.object(sweep, "sweep_project", return_value={"addresses": [], "subnets": [], "networks": []}),
            ):
                code = sweep.main(["--project", "p1", "--report", report_path])
                self.assertEqual(code, 1)
                data = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
                self.assertEqual(data["exit"], "failed")
                self.assertEqual(data["failures"]["p1"], "release failed: release rejected")
        finally:
            pathlib.Path(report_path).unlink(missing_ok=True)

    def test_project_mode_sweep_fault_and_release_failure_joined_without_double_prefix(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            report_path = tf.name

        try:
            err = sweep.boskos_pool.BoskosError("release rejected")
            sweep_err = sweep.SweepError("p1: networks: net-1 (inUse)")
            with (
                mock.patch.object(sweep.boskos_pool, "acquire", return_value="p1"),
                mock.patch.object(sweep.boskos_pool, "release_settled", side_effect=err),
                mock.patch.object(sweep, "sweep_project", side_effect=sweep_err),
            ):
                code = sweep.main(["--project", "p1", "--report", report_path])
                self.assertEqual(code, 1)
                data = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
                self.assertEqual(data["exit"], "failed")
                self.assertEqual(data["failures"]["p1"], "p1: networks: net-1 (inUse); release failed: release rejected")
                self.assertNotIn("release failed: release failed", data["failures"]["p1"])
        finally:
            pathlib.Path(report_path).unlink(missing_ok=True)

    def test_pool_mode_invocation(self):
        with (
            mock.patch.object(sweep, "pool_projects", return_value={"p1"}),
            mock.patch.object(sweep, "sweep_pool", return_value=({}, {}, [])) as mock_pool,
        ):
            code = sweep.main(["--pool", "--max-age-hours", "4.0"])
            self.assertEqual(code, 0)
            self.assertTrue(mock_pool.called)

    def test_parse_max_age_hours_valid(self):
        self.assertEqual(sweep.parse_max_age_hours("2.0"), 2.0)
        self.assertEqual(sweep.parse_max_age_hours("2.5"), 2.5)
        self.assertEqual(sweep.parse_max_age_hours("4.0"), 4.0)

    def test_parse_max_age_hours_rejects_sub_minimum_negatives_nan_and_inf(self):
        for val in ["0", "-1", "nan", "NaN", "inf", "Infinity", "1.9"]:
            with self.subTest(val=val):
                with self.assertRaises(sweep.argparse.ArgumentTypeError):
                    sweep.parse_max_age_hours(val)

    def test_main_rejects_invalid_max_age_hours(self):
        for val in ["0", "-1", "nan", "inf", "1.9"]:
            with self.subTest(val=val):
                with self.assertRaises(SystemExit) as ctx:
                    with mock.patch("sys.stderr", io.StringIO()):
                        sweep.main(["--project", "p1", "--max-age-hours", val])
                self.assertEqual(ctx.exception.code, 2)

    def test_main_fault_outside_visit_returns_1_without_traceback(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            report_path = tf.name

        try:
            err = sweep.SweepError("could not read ci-deploy.sh")
            with mock.patch.object(sweep, "pool_projects", side_effect=err):
                with mock.patch("sys.stderr", io.StringIO()) as fake_stderr:
                    code = sweep.main(["--pool", "--report", report_path])
                self.assertEqual(code, 1)
                self.assertIn("SweepError", fake_stderr.getvalue())
                data = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
                self.assertEqual(data["exit"], "failed")
                self.assertEqual(data["exit_code"], 1)
                self.assertIn("could not read ci-deploy.sh", data["error"])
        finally:
            pathlib.Path(report_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
