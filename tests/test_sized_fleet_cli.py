"""hack/sized_fleet.py's own logic: the region, the host check, the teardown record, the churn warm-up, the image lookup, the sweep and the soak window's eval cases. The cloud calls are stubbed; nothing here creates or deletes a resource."""

import argparse
import importlib.util


import json
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


class PublishedTest(unittest.TestCase):
    def test_the_newest_commit_with_images_is_chosen(self):
        log = mock.Mock(stdout="aaa\nbbb\nccc\n")
        with mock.patch.object(cli.subprocess, "run", return_value=log), \
                mock.patch.object(cli, "image_published", side_effect=lambda sha: sha != "aaa"):
            self.assertEqual(cli.newest_published(), "bbb")

    def test_no_published_commit_stops_the_run(self):
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(stdout="aaa\n")), \
                mock.patch.object(cli, "image_published", return_value=False):
            with self.assertRaises(SystemExit):
                cli.newest_published()


class SweepTest(unittest.TestCase):
    def test_only_sizes_with_a_cluster_older_than_the_limit_are_deleted(self):
        clusters = [
            {"createTime": "2026-11-10T00:00:00Z", "resourceLabels": {cli.SIZE_LABEL: "m"}},
            {"createTime": "2026-11-11T09:00:00Z", "resourceLabels": {cli.SIZE_LABEL: "s"}},
        ]
        deleted = []
        now = cli.datetime.fromisoformat("2026-11-11T10:00:00+00:00").timestamp()
        with mock.patch.object(cli.kube, "gcloud", return_value=json.dumps(clusters)), \
                mock.patch.object(cli.time, "time", return_value=now), \
                mock.patch.object(cli, "cmd_down", side_effect=lambda a: deleted.append(a.size)):
            cli.cmd_sweep(args())
        self.assertEqual(deleted, ["M"])

    def test_a_failed_size_does_not_stop_the_others_and_the_sweep_says_so(self):
        old = "2026-11-10T00:00:00Z"
        clusters = [{"createTime": old, "resourceLabels": {cli.SIZE_LABEL: size}} for size in ("l", "m", "x")]
        tried = []

        def down(a):
            tried.append(a.size)
            if a.size == "L":
                raise cli.TofuFailed("destroy", 1, "state lock")

        now = cli.datetime.fromisoformat("2026-11-11T10:00:00+00:00").timestamp()
        with mock.patch.object(cli.kube, "gcloud", return_value=json.dumps(clusters)), \
                mock.patch.object(cli.time, "time", return_value=now), \
                mock.patch.object(cli, "cmd_down", side_effect=down):
            rc = cli.cmd_sweep(args())
        self.assertEqual(tried, ["L", "M"])
        self.assertEqual(rc, cli.EXIT_UNSOUND)


class EvalTest(unittest.TestCase):
    def test_the_cases_file_lists_paths_and_skips_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.txt"
            path.write_text("# header\n\n./tasks/a/task.yaml\n  ./tasks/b/task.yaml  \n")
            self.assertEqual(cli.eval_cases(path), ["./tasks/a/task.yaml", "./tasks/b/task.yaml"])

    def test_the_shipped_cases_file_names_cases_that_exist(self):
        cases = cli.eval_cases(cli.EVAL_CASES_FILE)
        self.assertTrue(cases)
        for case in cases:
            self.assertTrue((cli.BENCH_DIR / case).is_file(), case)

    def test_a_case_passes_only_when_every_objective_passed(self):
        passed = {"role": "objective", "success": True}
        failed = {"role": "objective", "success": False}
        safeguard = {"role": "safeguard", "success": False}
        self.assertTrue(cli.case_passed({"verification_report": [passed, safeguard]}))
        self.assertFalse(cli.case_passed({"verification_report": [passed, failed]}))
        self.assertFalse(cli.case_passed({"verification_report": []}))


class ImagesTest(unittest.TestCase):
    def test_every_first_party_image_an_install_pulls_is_checked(self):
        repos = cli.image_repositories()
        # The chart's five tags, and the bus images the operator renders at its own tag.
        for name in ("platform-agent", "k8s-operator", "a2a-gateway", "a2a-worker", "hermes-bridge", "web-console"):
            self.assertIn(f"gke-labs/kube-agents/{name}", repos)
        self.assertFalse(any("litellm" in r for r in repos))

    def test_a_commit_counts_as_published_only_when_every_image_is(self):
        with mock.patch.object(cli, "manifest_exists", side_effect=lambda repo, sha: "operator" in repo):
            self.assertFalse(cli.image_published("abc"))
        with mock.patch.object(cli, "manifest_exists", return_value=True):
            self.assertTrue(cli.image_published("abc"))


class EvalCaseIsolationTest(unittest.TestCase):
    def test_a_case_that_fails_to_run_does_not_cost_the_others_their_records(self):
        calls = []

        def run_case(case, name, env, evals):
            calls.append(name)
            if name == "a":
                raise cli.subprocess.TimeoutExpired("devops-bench", 1)
            return {"passed": True}

        relay = mock.Mock(port=1)
        with tempfile.TemporaryDirectory() as tmp:
            cases = Path(tmp) / "cases.txt"
            cases.write_text("./tasks/a/task.yaml\n./tasks/b/task.yaml\n")
            with mock.patch.object(cli.operations, "Relay", return_value=relay), \
                    mock.patch.object(cli.operations, "agent_env", return_value={}), \
                    mock.patch.object(cli, "run_case", side_effect=run_case):
                summary = cli.run_evals(args(eval_cases=str(cases), namespace="ns", host_cluster="h"), "ctx",
                                        Path(tmp))
            self.assertTrue((Path(tmp) / "evals" / "summary.json").is_file())
        self.assertEqual(calls, ["a", "b"])
        self.assertFalse(summary["a"]["passed"])
        self.assertTrue(summary["b"]["passed"])
        relay.stop.assert_called_once()


class TestBedValuesTest(unittest.TestCase):
    def test_a_release_with_chat_or_gitops_is_refused(self):
        values = {"platformAgent": {"integration": {"slack": {"enabled": True}, "github": {"gitRepo": "o/r"},
                                                    "repositories": [{"url": "x"}]}}}
        problems, _ = cli.test_bed_values(values)
        self.assertEqual(len(problems), 3)

    def test_auto_promote_is_turned_off_and_other_env_kept(self):
        values = {"platformAgent": {"deployment": {"env": [{"name": "A", "value": "1"},
                                                          {"name": cli.AUTO_PROMOTE_ENV, "value": "true"}]}}}
        problems, env = cli.test_bed_values(values)
        self.assertEqual(problems, [])
        self.assertEqual(env, [{"name": "A", "value": "1"}, {"name": cli.AUTO_PROMOTE_ENV, "value": "false"}])


class RunCaseInterruptTest(unittest.TestCase):
    def test_an_interrupt_stops_the_case_process_group_and_is_raised(self):
        proc = mock.Mock(pid=4242)
        proc.communicate.side_effect = [SystemExit(143), ("partial", None)]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cli.subprocess, "Popen", return_value=proc), \
                mock.patch.object(cli.os, "killpg") as killpg:
            with self.assertRaises(SystemExit):
                cli.run_case("./tasks/a/task.yaml", "a", {}, Path(tmp))
            self.assertEqual((Path(tmp) / "a.log").read_text(), "partial")
        killpg.assert_called_once_with(4242, cli.signal.SIGTERM)


class CapabilityUsageTest(unittest.TestCase):
    def test_each_window_is_attributed_from_one_read_with_its_own_counters(self):
        windows = {"burst": {"start": "2026-11-11T19:00:00+00:00", "seconds": 3600, "tokens": {"tokens_input": 1}},
                   "soak": {"start": "2026-11-11T20:00:00+00:00", "seconds": 3600, "tokens": {}}}
        seen = []
        with mock.patch.object(cli.attribution, "read_stores", return_value={"sessions": []}) as read, \
                mock.patch.object(cli.attribution, "attribute",
                                  side_effect=lambda s, start, end, litellm: seen.append((end - start, litellm)) or {}):
            usage = cli.capability_usage("ctx", "ns", windows)
        read.assert_called_once()
        self.assertEqual(list(usage), ["burst", "soak"])
        self.assertEqual(seen, [(3600, {"tokens_input": 1}), (3600, {})])
