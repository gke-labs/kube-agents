"""hack/sized_fleet.py's own logic: the region and the firewall fallback. The cloud calls are stubbed; nothing here creates or deletes a resource."""

import argparse
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("sized_fleet_cli", REPO / "hack" / "sized_fleet.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def args(**overrides):
    base = dict(size="S", project="p", zone="us-central1-f", node_sa=None, run_id="r", max_age_hours=8.0)
    return argparse.Namespace(**{**base, **overrides})


class RegionTest(unittest.TestCase):
    def test_the_region_is_the_zone_without_its_letter(self):
        self.assertEqual(cli.region_of("us-central1-f"), "us-central1")
        self.assertEqual(cli.region_of("europe-west4-a"), "europe-west4")
        self.assertIn("-var=region=europe-west4", cli.tofu_vars(args(zone="europe-west4-a")))


class DownTest(unittest.TestCase):
    def run_down(self, first_error):
        calls = []

        def fake_tofu(argv, timeout, capture=False):
            calls.append(argv[0])
            if argv[0] == "destroy" and calls.count("destroy") == 1 and first_error is not None:
                raise cli.TofuFailed("destroy", 1, first_error)
            return ""

        with mock.patch.object(cli, "tofu", side_effect=fake_tofu), \
                mock.patch.object(cli, "delete_network_firewalls", return_value=["fw-1"]) as fw:
            try:
                cli.cmd_down(args())
            except cli.TofuFailed:
                pass
        return calls, fw

    def test_a_firewall_holding_the_network_is_deleted_and_the_destroy_retried(self):
        error = "The network resource '.../networks/sized-fleet-s' is already being used by '.../firewalls/fw-1'"
        calls, fw = self.run_down(error)
        fw.assert_called_once()
        self.assertEqual(calls.count("destroy"), 2)

    def test_any_other_failure_leaves_the_firewall_rules_alone(self):
        calls, fw = self.run_down("Error acquiring the state lock")
        fw.assert_not_called()
        self.assertEqual(calls.count("destroy"), 1)


if __name__ == "__main__":
    unittest.main()


class HostTest(unittest.TestCase):
    def test_a_host_without_the_label_is_refused(self):
        with mock.patch.object(cli.kube, "cluster_labels", return_value={"purpose": "prod"}):
            with self.assertRaises(SystemExit):
                cli.check_host(args(host_cluster="h", host_location="l"))

    def test_a_labelled_host_passes(self):
        with mock.patch.object(cli.kube, "cluster_labels", return_value={"purpose": "harness-scale"}):
            cli.check_host(args(host_cluster="h", host_location="l"))


class TeardownTest(unittest.TestCase):
    def test_a_failed_down_marks_the_teardown_red_and_returns_unsound(self):
        import json
        import tempfile
        out = Path(tempfile.mkdtemp())
        (out / cli.VERDICT_FILE).write_text(json.dumps({"green": True, "checks": {}}))
        with mock.patch.object(cli, "run_once", return_value=(0, out)), \
                mock.patch.object(cli, "cmd_down", side_effect=cli.TofuFailed("destroy", 1, "lock held")), \
                mock.patch.object(cli, "leftover_clusters", return_value=["sized-s-01"]), \
                mock.patch.object(cli, "interruptible"), mock.patch.object(cli, "shielded"):
            rc = cli.run_and_delete(args(results_bucket=None))
        verdict = json.loads((out / cli.VERDICT_FILE).read_text())
        self.assertEqual(rc, cli.EXIT_UNSOUND)
        self.assertFalse(verdict["green"])
        self.assertIn("sized-s-01 still exists", verdict["checks"]["teardown"]["failures"])
        self.assertTrue(any("down failed" in f for f in verdict["checks"]["teardown"]["failures"]))
