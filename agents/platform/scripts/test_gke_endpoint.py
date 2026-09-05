"""Unit tests for gke_endpoint.dns_endpoint_args (the --dns-endpoint decision).

Run: python3 -m unittest agents.platform.scripts.test_gke_endpoint

Every case drives a fake runner rather than gcloud, so the predicate is pinned
without a project or a network. The shapes below are real describe output with
the identifying values replaced: an endpoint hostname carries the project number
of the cluster it names, so these are synthetic and the IPs come from the
documentation ranges. The `allowExternalTraffic: false` case is the one that
proved passing the flag blindly yields a kubeconfig which 403s.
"""

import io
import json
import os
import subprocess
import sys
import unittest
import unittest.mock
from contextlib import contextmanager, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gke_endpoint  # noqa: E402
import sandbox_exec  # noqa: E402

HELP_WITH_FLAG = "    --dns-endpoint\n        Whether to use the DNS-based endpoint.\n"
HELP_WITHOUT_FLAG = "    --internal-ip\n        Use the internal IP address.\n"

# Both endpoints present, DNS open to the outside: the case this feature exists for.
DNS_EXTERNAL = {
    "controlPlaneEndpointsConfig": {
        "dnsEndpointConfig": {
            "allowExternalTraffic": True,
            "endpoint": "gke-abc123.us-central1.gke.goog",
        },
        "ipEndpointsConfig": {"enabled": True, "enablePublicEndpoint": True},
    }
}

# A DNS endpoint exists but refuses external traffic. gcloud only errors for
# non-Googlers here, so the flag must be withheld on the configuration, not on
# whether the command happened to fail.
DNS_INTERNAL_ONLY = {
    "controlPlaneEndpointsConfig": {
        "dnsEndpointConfig": {
            "allowExternalTraffic": False,
            "endpoint": "gke-0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b-123456789012.us-central1.gke.goog",
        },
        "ipEndpointsConfig": {
            "enabled": True,
            "enablePublicEndpoint": True,
            "privateEndpoint": "10.0.0.2",
            "publicEndpoint": "203.0.113.10",
        },
    }
}

# A cluster old enough to predate DNS endpoints entirely.
NO_DNS_BLOCK = {"controlPlaneEndpointsConfig": {"ipEndpointsConfig": {"enabled": True}}}


class FakeRunner:
    """Answers the help probe and the describe, and records what it was asked."""

    def __init__(self, describe=None, help_text=HELP_WITH_FLAG, describe_exit=0):
        self.describe = describe
        self.help_text = help_text
        self.describe_exit = describe_exit
        self.help_exit = 0
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        if "--help" in argv:
            return self.help_exit, ("" if self.help_exit else self.help_text)
        if "describe" in argv:
            if self.describe_exit != 0:
                return self.describe_exit, ""
            payload = self.describe if isinstance(self.describe, str) else json.dumps(self.describe)
            return 0, payload
        raise AssertionError(f"unexpected command: {argv}")

    @property
    def describe_calls(self):
        return [c for c in self.calls if "describe" in c]


@contextmanager
def expired_cache():
    """Run with every memoised endpoint answer already past its window.

    A zero TTL rather than a fake clock: the module reads `time.monotonic()`
    directly, and the property under test is "an answer older than the window is
    re-read", which a window of zero states without a second mechanism to trust.
    """
    original = gke_endpoint._ENDPOINT_TTL_SECONDS
    gke_endpoint._ENDPOINT_TTL_SECONDS = 0.0
    try:
        yield
    finally:
        gke_endpoint._ENDPOINT_TTL_SECONDS = original


def decide(runner, project="p", cluster="c", location="us-central1"):
    """Run the decision with a clean cache and stderr swallowed."""
    gke_endpoint.reset_cache()
    with redirect_stderr(io.StringIO()):
        return gke_endpoint.dns_endpoint_args(project, cluster, location, run=runner)


class PredicateTest(unittest.TestCase):
    def test_external_dns_endpoint_gets_the_flag(self):
        self.assertEqual(decide(FakeRunner(DNS_EXTERNAL)), ["--dns-endpoint"])

    def test_external_traffic_disabled_gets_no_flag(self):
        # The regression this whole module guards: gcloud would have accepted the
        # flag for an internal caller and produced a kubeconfig that 403s.
        self.assertEqual(decide(FakeRunner(DNS_INTERNAL_ONLY)), [])

    def test_cluster_without_a_dns_endpoint_gets_no_flag(self):
        self.assertEqual(decide(FakeRunner(NO_DNS_BLOCK)), [])

    def test_empty_describe_gets_no_flag(self):
        self.assertEqual(decide(FakeRunner({})), [])

    def test_endpoint_present_but_allow_external_traffic_absent(self):
        # Absent is a no, not a maybe.
        shape = {"controlPlaneEndpointsConfig": {"dnsEndpointConfig": {"endpoint": "x.gke.goog"}}}
        self.assertEqual(decide(FakeRunner(shape)), [])

    def test_allow_external_traffic_true_but_no_endpoint(self):
        shape = {
            "controlPlaneEndpointsConfig": {
                "dnsEndpointConfig": {"allowExternalTraffic": True, "endpoint": ""}
            }
        }
        self.assertEqual(decide(FakeRunner(shape)), [])


class DegradesQuietlyTest(unittest.TestCase):
    """A cluster we cannot ask about must behave exactly as it did before."""

    def test_describe_failure_is_not_fatal(self):
        self.assertEqual(decide(FakeRunner(DNS_EXTERNAL, describe_exit=1)), [])

    def test_unparseable_describe_is_not_fatal(self):
        self.assertEqual(decide(FakeRunner("not json at all")), [])

    def test_runner_raising_is_not_fatal(self):
        def explode(argv):
            if "--help" in argv:
                return 0, HELP_WITH_FLAG
            raise subprocess.TimeoutExpired(argv, 30)

        self.assertEqual(decide(explode), [])

    def test_oserror_from_missing_gcloud_is_not_fatal(self):
        def no_gcloud(argv):
            raise OSError("No such file or directory: 'gcloud'")

        self.assertEqual(decide(no_gcloud), [])

    def test_incomplete_target_is_not_described(self):
        runner = FakeRunner(DNS_EXTERNAL)
        gke_endpoint.reset_cache()
        self.assertEqual(gke_endpoint.dns_endpoint_args("p", "", "us-central1", run=runner), [])
        self.assertEqual(runner.calls, [])


class GcloudSupportTest(unittest.TestCase):
    def test_old_gcloud_gets_no_flag_and_is_never_asked_to_describe(self):
        runner = FakeRunner(DNS_EXTERNAL, help_text=HELP_WITHOUT_FLAG)
        self.assertEqual(decide(runner), [])
        self.assertEqual(runner.describe_calls, [])

    def test_support_probe_is_memoised(self):
        runner = FakeRunner(DNS_EXTERNAL)
        gke_endpoint.reset_cache()
        with redirect_stderr(io.StringIO()):
            gke_endpoint.dns_endpoint_args("p", "c1", "us-central1", run=runner)
            gke_endpoint.dns_endpoint_args("p", "c2", "us-central1", run=runner)
        self.assertEqual(len([c for c in runner.calls if "--help" in c]), 1)

    def test_a_probe_that_could_not_run_is_retried_rather_than_memoised(self):
        """Only gcloud's answer is worth keeping, never our failure to get one.

        The credential proxy is a daemon. A probe that failed once — the fork
        lost a race, the binary was mid-upgrade — cached as "unsupported" would
        switch the endpoint detection off for the life of the pod.
        """
        attempts = []

        def runner(argv):
            if "--help" in argv:
                attempts.append(argv)
                if len(attempts) == 1:
                    raise OSError("Resource temporarily unavailable")
                return 0, HELP_WITH_FLAG
            return 0, json.dumps(DNS_EXTERNAL)

        gke_endpoint.reset_cache()
        with redirect_stderr(io.StringIO()):
            first = gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
            second = gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
        self.assertEqual(first, [])
        self.assertEqual(second, ["--dns-endpoint"])
        self.assertEqual(len(attempts), 2)

    def test_a_probe_that_exits_nonzero_is_not_taken_as_unsupported(self):
        runner = FakeRunner(DNS_EXTERNAL)
        runner.help_exit = 1
        gke_endpoint.reset_cache()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner), [])
        runner.help_exit = 0
        with redirect_stderr(io.StringIO()):
            self.assertEqual(
                gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner),
                ["--dns-endpoint"],
            )


class CacheTest(unittest.TestCase):
    def test_same_cluster_is_described_once(self):
        runner = FakeRunner(DNS_EXTERNAL)
        gke_endpoint.reset_cache()
        with redirect_stderr(io.StringIO()):
            first = gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
            second = gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
        self.assertEqual(first, ["--dns-endpoint"])
        self.assertEqual(second, ["--dns-endpoint"])
        self.assertEqual(len(runner.describe_calls), 1)

    def test_distinct_clusters_are_described_separately(self):
        runner = FakeRunner(DNS_EXTERNAL)
        gke_endpoint.reset_cache()
        with redirect_stderr(io.StringIO()):
            gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
            gke_endpoint.dns_endpoint_args("p", "c", "europe-west1", run=runner)
        self.assertEqual(len(runner.describe_calls), 2)

    def test_a_failed_describe_is_retried_rather_than_cached(self):
        """"Could not find out" must not be remembered as "no".

        Caching it pinned a cluster to its IP endpoint for the life of the
        process, so a describe that failed once — a transient API error, or a
        request the credential proxy rejected before the profile's kubeconfig
        existed — outlived its cause by the lifetime of the pod.
        """
        runner = FakeRunner(DNS_EXTERNAL, describe_exit=1)
        gke_endpoint.reset_cache()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner), [])
            runner.describe_exit = 0
            self.assertEqual(
                gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner),
                ["--dns-endpoint"],
            )
        self.assertEqual(len(runner.describe_calls), 2)

    def test_a_definite_no_is_cached_for_the_window(self):
        runner = FakeRunner(DNS_INTERNAL_ONLY)
        gke_endpoint.reset_cache()
        with redirect_stderr(io.StringIO()):
            gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
            gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
        self.assertEqual(len(runner.describe_calls), 1)

    def test_the_answer_is_re_read_once_it_expires(self):
        """The remedy this repository documents has to be able to take effect.

        The `gke-networking` footer tells the agent to run `clusters update
        --enable-dns-access` when a cluster's endpoint refuses external traffic,
        and the MCP server and the credential proxy both outlive any number of
        such changes. An answer kept for the life of the process would make the
        remedy look like it did nothing.
        """
        runner = FakeRunner(DNS_INTERNAL_ONLY)
        gke_endpoint.reset_cache()
        with expired_cache(), redirect_stderr(io.StringIO()):
            self.assertEqual(gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner), [])
            runner.describe = DNS_EXTERNAL  # the operator ran --enable-dns-access
            self.assertEqual(
                gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner),
                ["--dns-endpoint"],
            )
        self.assertEqual(len(runner.describe_calls), 2)

    def test_the_reverse_change_is_picked_up_too(self):
        # --no-enable-dns-access, the reversal. Keeping the flag past it means a
        # kubeconfig whose every request comes back 403.
        runner = FakeRunner(DNS_EXTERNAL)
        gke_endpoint.reset_cache()
        with expired_cache(), redirect_stderr(io.StringIO()):
            self.assertEqual(
                gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner),
                ["--dns-endpoint"],
            )
            runner.describe = DNS_INTERNAL_ONLY
            self.assertEqual(gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner), [])

    def test_a_failed_refresh_serves_the_answer_gcloud_last_gave(self):
        """Expiry must not turn a transient error into a downgrade.

        Falling back to `[]` here would be the failure mistaken for a
        configuration: a cluster reachable only over its DNS endpoint would get
        an IP-endpoint kubeconfig it cannot route to, because one describe
        happened to fail after the window closed.
        """
        runner = FakeRunner(DNS_EXTERNAL)
        gke_endpoint.reset_cache()
        with expired_cache(), redirect_stderr(io.StringIO()):
            self.assertEqual(
                gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner),
                ["--dns-endpoint"],
            )
            runner.describe_exit = 1
            self.assertEqual(
                gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner),
                ["--dns-endpoint"],
            )
            # The stale entry keeps its timestamp, so the next call retries
            # rather than waiting out a second window.
            runner.describe_exit = 0
            runner.describe = DNS_INTERNAL_ONLY
            self.assertEqual(gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner), [])
        self.assertEqual(len(runner.describe_calls), 3)

    def test_caller_cannot_mutate_the_cached_answer(self):
        runner = FakeRunner(DNS_EXTERNAL)
        gke_endpoint.reset_cache()
        with redirect_stderr(io.StringIO()):
            first = gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
            first.append("--internal-ip")
            second = gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
        self.assertEqual(second, ["--dns-endpoint"])


class DescribeCommandTest(unittest.TestCase):
    def test_describe_is_scoped_to_the_named_cluster(self):
        runner = FakeRunner(DNS_EXTERNAL)
        decide(runner, project="proj", cluster="clus", location="europe-west1")
        argv = runner.describe_calls[0]
        self.assertEqual(argv[:5], ["gcloud", "container", "clusters", "describe", "clus"])
        self.assertIn("--location=europe-west1", argv)
        self.assertIn("--project=proj", argv)


class RunnerEnvironmentTest(unittest.TestCase):
    """The default runner must not hand gcloud a KUBECONFIG.

    `gcloud` is the credential-proxy shim, and the shim forwards `$KUBECONFIG`
    on every gcloud call. `describe` is not `get-credentials`, so the proxy
    resolves that path through `_target_of`, which stats the file and returns
    HTTP 400 when it is missing. Both callers pass the kubeconfig their
    `get-credentials` is about to *create*, so it is reliably missing — which
    made the describe fail every time and the whole detection a constant
    "no flag" inside the pod.

    Only the unsandboxed path can get this wrong now. With a sandbox the
    command carries no environment at all, which is why the sandboxed case
    below asserts the routing rather than the scrubbing.
    """

    def _env_seen_by_gcloud(self, passed_env):
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0, stdout=HELP_WITH_FLAG, stderr="")

        gke_endpoint.reset_cache()
        original = gke_endpoint.subprocess.run
        gke_endpoint.subprocess.run = fake_run
        try:
            # Pinned rather than inherited: inside the agent pod the managed
            # config exists and this would take the ssh path, so the test would
            # pass or fail depending on where it ran.
            with unittest.mock.patch.object(sandbox_exec, "sandbox_enabled", return_value=False):
                with redirect_stderr(io.StringIO()):
                    gke_endpoint.dns_endpoint_args("p", "c", "us-central1", env=passed_env)
        finally:
            gke_endpoint.subprocess.run = original
        return seen

    def test_kubeconfig_is_stripped_from_a_caller_supplied_env(self):
        seen = self._env_seen_by_gcloud(
            {"HOME": "/tmp", "KUBECONFIG": "/opt/data/home/does-not-exist-yet.yaml"}
        )
        self.assertNotIn("KUBECONFIG", seen)
        self.assertEqual(seen.get("HOME"), "/tmp")

    def test_with_a_sandbox_gcloud_runs_there_rather_than_in_the_agent_pod(self):
        """The agent image carries no gcloud, so a local run would be the bug."""
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=HELP_WITH_FLAG, stderr="")

        gke_endpoint.reset_cache()
        with unittest.mock.patch.object(sandbox_exec, "sandbox_enabled", return_value=True), \
             unittest.mock.patch.object(sandbox_exec, "ssh_argv",
                                        side_effect=lambda argv, **kw: ["ssh", "hermes@sandbox",
                                                                        " ".join(argv)]), \
             unittest.mock.patch.object(sandbox_exec.subprocess, "run", fake_run), \
             redirect_stderr(io.StringIO()):
            gke_endpoint.dns_endpoint_args("p", "c", "us-central1",
                                           env={"KUBECONFIG": "/nope"})

        self.assertTrue(calls, "gcloud was never run")
        for argv in calls:
            self.assertEqual(argv[0], "ssh")
            self.assertTrue(argv[1].startswith("hermes@"))

    def test_kubeconfig_is_stripped_from_the_inherited_environment(self):
        original = os.environ.get("KUBECONFIG")
        os.environ["KUBECONFIG"] = "/nowhere/kubeconfig.yaml"
        try:
            seen = self._env_seen_by_gcloud(None)
        finally:
            if original is None:
                del os.environ["KUBECONFIG"]
            else:
                os.environ["KUBECONFIG"] = original
        self.assertNotIn("KUBECONFIG", seen)


if __name__ == "__main__":
    unittest.main()
