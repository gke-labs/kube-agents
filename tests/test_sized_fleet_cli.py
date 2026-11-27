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


class RunDownTest(unittest.TestCase):
    def test_a_run_that_raises_still_deletes_the_fleet(self):
        with mock.patch.object(cli, "run_once", side_effect=RuntimeError("boom")), \
                mock.patch.object(cli, "cmd_down") as down, \
                mock.patch.object(cli, "leftover_clusters", return_value=[]), \
                mock.patch.object(cli, "interruptible"), mock.patch.object(cli, "shielded"):
            with self.assertRaises(RuntimeError):
                cli.run_and_delete(args(results_bucket=None))
        down.assert_called_once()


class LeftoverTest(unittest.TestCase):
    def test_leftovers_are_matched_by_run_and_size(self):
        with mock.patch.object(cli.kube, "gcloud", return_value="") as gcloud:
            cli.leftover_clusters(args(size="M", run_id="manual"))
        query = next(a for a in gcloud.call_args.args if a.startswith("--filter="))
        self.assertIn("resourceLabels.kube-agents-scale-run=manual", query)
        self.assertIn("resourceLabels.kube-agents-scale-size=m", query)


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
        with mock.patch.dict(cli.os.environ, {cli.REGISTRY_PREFIX_ENV: ""}):
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


AR_PREFIX = "us-docker.pkg.dev/some-project/some-repository"


class RegistryPrefixTest(unittest.TestCase):
    """REGISTRY_PREFIX names where a commit's images are looked up and installed from."""

    def _manifest_request(self, prefix_env, token):
        """The manifest request manifest_exists sends, with the token source stubbed."""
        sent = []

        def urlopen(request, timeout=None):
            sent.append(request)
            return mock.MagicMock()

        with mock.patch.dict(cli.os.environ, prefix_env), \
                mock.patch.object(cli, "registry_token", return_value=token) as token_for, \
                mock.patch.object(cli.urllib.request, "urlopen", side_effect=urlopen):
            self.assertTrue(cli.manifest_exists(cli.image_repositories()[0], "abc"))
        return sent[0], token_for.call_args.args

    def test_unset_it_is_the_public_default(self):
        with mock.patch.dict(cli.os.environ, {cli.REGISTRY_PREFIX_ENV: ""}):
            self.assertEqual(cli.registry_prefix(), cli.DEFAULT_REGISTRY_PREFIX)

    def test_each_image_keeps_its_name_under_the_prefix(self):
        with mock.patch.dict(cli.os.environ, {cli.REGISTRY_PREFIX_ENV: AR_PREFIX + "/"}):
            repos = cli.image_repositories()
        self.assertIn("some-project/some-repository/platform-agent", repos)
        self.assertTrue(all(r.startswith("some-project/some-repository/") for r in repos))

    def test_the_manifest_is_asked_of_the_prefixes_host_with_its_token(self):
        request, token_args = self._manifest_request({cli.REGISTRY_PREFIX_ENV: AR_PREFIX}, "google-token")
        self.assertTrue(request.full_url.startswith("https://us-docker.pkg.dev/v2/some-project/some-repository/"))
        self.assertTrue(request.full_url.endswith("/manifests/abc"))
        self.assertEqual(request.get_header("Authorization"), "Bearer google-token")
        self.assertEqual(token_args[0], "us-docker.pkg.dev")
        request, token_args = self._manifest_request({cli.REGISTRY_PREFIX_ENV: ""}, "anonymous-token")
        self.assertTrue(request.full_url.startswith("https://ghcr.io/v2/gke-labs/kube-agents/"))
        self.assertEqual(token_args[0], "ghcr.io")

    def test_artifact_registry_takes_the_callers_google_token_and_ghcr_an_anonymous_one(self):
        self.addCleanup(cli.google_access_token.cache_clear)
        cli.google_access_token.cache_clear()
        with mock.patch.object(cli.kube, "run", return_value="google-token\n") as run, \
                mock.patch.object(cli.urllib.request, "urlopen") as urlopen:
            self.assertEqual(cli.registry_token("us-docker.pkg.dev", "p/r/platform-agent"), "google-token")
            self.assertEqual(cli.registry_token("us-docker.pkg.dev", "p/r/k8s-operator"), "google-token")
        self.assertEqual(run.call_args.args[0], cli.ACCESS_TOKEN_COMMAND)
        self.assertIsNotNone(run.call_args.kwargs["timeout"])
        self.assertEqual(run.call_count, 1, "the token is asked for once a run, not once an image")
        urlopen.assert_not_called()

    def test_a_token_blip_is_retried_and_a_failure_carries_gclouds_message(self):
        """The token goes through the runner every other gcloud call uses."""
        self.addCleanup(cli.google_access_token.cache_clear)
        cli.google_access_token.cache_clear()
        blip = mock.Mock(returncode=1, stdout="", stderr="ERROR: There was a problem refreshing your current auth tokens")
        token = mock.Mock(returncode=0, stdout="google-token\n", stderr="")
        with mock.patch.object(cli.kube.subprocess, "run", side_effect=[blip, token]) as run, \
                mock.patch.object(cli.kube.time, "sleep"):
            self.assertEqual(cli.google_access_token(), "google-token")
        self.assertEqual(run.call_count, 2)
        cli.google_access_token.cache_clear()
        denied = mock.Mock(returncode=1, stdout="", stderr="ERROR: (gcloud.auth.print-access-token) no active account")
        with mock.patch.object(cli.kube.subprocess, "run", return_value=denied):
            with self.assertRaises(cli.kube.CommandError) as failure:
                cli.google_access_token()
        self.assertIn("no active account", str(failure.exception))
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"token": "anonymous-token"}'
        with mock.patch.object(cli.subprocess, "run") as run, \
                mock.patch.object(cli.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertEqual(cli.registry_token("ghcr.io", "gke-labs/kube-agents/platform-agent"), "anonymous-token")
        run.assert_not_called()
        self.assertIn("https://ghcr.io/token?", urlopen.call_args.args[0])

    def test_the_google_token_goes_to_no_host_that_only_resembles_artifact_registry(self):
        """A host that is neither Artifact Registry nor GHCR is refused, with no request made."""
        for host in ("us-docker.pkg.dev.example.com", "evil.example/us-docker.pkg.dev", "docker.pkg.dev",
                     "registry.example.com"):
            with self.subTest(host=host), \
                    mock.patch.object(cli.subprocess, "run") as run, \
                    mock.patch.object(cli.urllib.request, "urlopen") as urlopen:
                with self.assertRaises(SystemExit) as refused:
                    cli.registry_token(host, "p/r/platform-agent")
                self.assertIn(host, str(refused.exception))
                run.assert_not_called()
                urlopen.assert_not_called()

    def test_the_default_prefix_and_the_host_pattern_are_the_ones_the_installer_and_release_scripts_use(self):
        defaults = (REPO / "install.defaults.env").read_text()
        self.assertIn(f'DEFAULT_REGISTRY_PREFIX="{cli.DEFAULT_REGISTRY_PREFIX}"', defaults)
        common = (REPO / "scripts" / "release" / "common.sh").read_text()
        self.assertIn(f"AR_REGISTRY_HOST_PATTERN='{cli.ARTIFACT_REGISTRY_HOST.pattern}'", common)
        self.assertTrue(cli.DEFAULT_REGISTRY_PREFIX.startswith(cli.GHCR_HOST + "/"))

    def _helm_upgrade_argv(self, prefix_env):
        seen = []

        def run(argv, **kwargs):
            if argv[0] == "helm":
                seen.append(argv)
            return mock.Mock(returncode=0, stderr="", stdout=b"")

        with mock.patch.dict(cli.os.environ, prefix_env), \
                mock.patch.object(cli.kube, "gcloud"), mock.patch.object(cli.kube, "run", return_value="{}"), \
                mock.patch.object(cli, "check_host"), mock.patch.object(cli, "host_lease"), \
                mock.patch.object(cli.subprocess, "run", side_effect=run):
            self.assertEqual(cli.cmd_install(args(sha="abc", host_cluster="h", host_location="z", namespace="ns")), 0)
        (argv,) = seen
        return argv

    def test_the_install_pulls_from_the_prefix_the_commit_was_looked_up_in(self):
        argv = self._helm_upgrade_argv({cli.REGISTRY_PREFIX_ENV: AR_PREFIX})
        self.assertIn(f"--set-string={cli.IMAGE_REGISTRY_KEY}={AR_PREFIX}", argv)

    def test_the_default_resets_a_registry_the_release_kept(self):
        """--reset-then-reuse-values keeps the release's values, another run's registry among them."""
        argv = self._helm_upgrade_argv({cli.REGISTRY_PREFIX_ENV: ""})
        self.assertIn(f"--set-string={cli.IMAGE_REGISTRY_KEY}=", argv)


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


class UsageWindowTest(unittest.TestCase):
    def test_a_window_that_was_not_measured_costs_only_its_own_record(self):
        windows = {"burst": {"start": "2026-11-11T19:00:00+00:00", "seconds": 3600, "tokens": {}}, "soak": {}}
        with mock.patch.object(cli.attribution, "read_stores", return_value={"sessions": []}):
            usage = cli.capability_usage("ctx", "ns", windows)
        self.assertIn("overview", usage["burst"])
        self.assertIn("error", usage["soak"])
        self.assertIn("soak window (not read)", cli.attribution.markdown(usage))

    def test_an_unread_memory_base_is_a_note_on_every_window(self):
        windows = {"burst": {"start": "2026-11-11T19:00:00+00:00", "seconds": 3600, "tokens": {}},
                   "soak": {"start": "2026-11-11T20:00:00+00:00", "seconds": 3600, "tokens": {}}}
        with mock.patch.object(cli.attribution, "container_memory", side_effect=RuntimeError("monitoring down")), \
                mock.patch.object(cli.model_counters, "vertex_tokens", return_value={}), \
                mock.patch.object(cli.model_counters, "requests_by_account", return_value={}):
            usage = cli.capability_usage("ctx", "ns", windows, project="p", stores={"sessions": []},
                                         install=mock.Mock())
        self.assertEqual(list(usage), ["burst", "soak"])
        for window in usage.values():
            self.assertIn("overview", window)
            self.assertTrue(any(e.startswith("memory base") for e in window["errors"]))

    def test_a_period_without_litellm_counters_is_checked_against_vertex_ai(self):
        stores = {"sessions": [{"id": "s1", "source": "cli", "started_at": 10.0, "input_tokens": 70,
                                "cache_read_tokens": 30, "output_tokens": 5}]}
        with mock.patch.object(cli.model_counters, "vertex_tokens",
                               return_value={cli.INSTALL_MODEL: {"input": 100, "output": 5}}), \
                mock.patch.object(cli.model_counters, "requests_by_account", side_effect=RuntimeError("iam denied")):
            usage = cli.measure_period(stores, "p", 0, 60)
        self.assertEqual(usage["checks"]["sum"], {"source": "vertex", "ratio": {"input": 1.0, "output": 1.0},
                                                  "passed": True})
        # An unread caller list is a note on the period, not a lost period.
        self.assertEqual(usage["requests_by_account"], {})
        self.assertTrue(any(e.startswith("requests by account") for e in usage["errors"]))


class InstallTimeoutTest(unittest.TestCase):
    def test_a_helm_wait_that_runs_out_fails_once_and_is_not_retried(self):
        calls = []

        def helm(argv, **kwargs):
            calls.append(argv[0])
            return mock.Mock(returncode=1, stderr="Error: UPGRADE FAILED: context deadline exceeded", stdout=b"")

        archive = mock.Mock(returncode=0, stdout=b"")
        with tempfile.TemporaryDirectory(), \
                mock.patch.object(cli.kube, "gcloud"), mock.patch.object(cli.kube, "run", return_value="{}"), \
                mock.patch.object(cli, "check_host"), mock.patch.object(cli, "host_lease"), \
                mock.patch.object(cli.subprocess, "run",
                                  side_effect=lambda argv, **kw: helm(argv) if argv[0] == "helm" else archive):
            with self.assertRaises(SystemExit):
                cli.cmd_install(args(sha="abc", host_cluster="h", host_location="z", namespace="ns"))
        self.assertEqual(calls, ["helm"])
