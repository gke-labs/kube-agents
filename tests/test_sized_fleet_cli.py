"""hack/sized_fleet.py's own logic: the region, the host check, the teardown record and the churn warm-up. The cloud calls are stubbed; nothing here creates or deletes a resource."""

import argparse
import importlib.util
import tempfile
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


class WarmupTest(unittest.TestCase):
    def test_waits_until_every_client_has_fired_once(self):
        reads = iter([
            {"items": [{"status": {}}, {"status": {"lastScheduleTime": "t"}}]},
            {"items": [{"status": {"lastScheduleTime": "t"}}, {"status": {"lastScheduleTime": "t"}}]},
        ])
        with mock.patch.object(cli.kube, "kubectl_json", side_effect=lambda *a, **k: next(reads)), \
                mock.patch.object(cli.time, "sleep") as sleep:
            cli.wait_clients_scheduled({"c": "ctx"}, "sel")
        self.assertEqual(sleep.call_count, 1)


class CredentialsTest(unittest.TestCase):
    def test_the_host_is_in_the_same_kubeconfig_as_the_fleet(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(cli.os.environ, {}, clear=False), \
                mock.patch.object(cli.kube, "gcloud", side_effect=lambda *a, **k: calls.append(a[3])):
            cli.credentials(args(kubeconfig=str(Path(tmp) / "k"), host_cluster="scale-host",
                                 host_location="us-central1-c"), cli.sf.fleet("S"))
            self.assertEqual(cli.os.environ[cli.kube.KUBECONFIG_ENV], str(Path(tmp) / "k"))
        self.assertEqual(calls[-1], "scale-host")
        self.assertEqual(len(calls), len(cli.sf.fleet("S").clusters) + 1)
