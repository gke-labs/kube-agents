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

OWN_NETWORK = "projects/host-proj/global/networks/shared-vpc"
OWN_SUBNETWORK = "projects/host-proj/regions/us-central1/subnetworks/mgmt"
OWN_POD_CIDR = "10.92.0.0/14"
OTHER_NETWORK = "projects/other-proj/global/networks/default"
TARGET_SUBNETWORK = "projects/host-proj/regions/us-central1/subnetworks/payments"
OWN_CLUSTER_ENV = {
    "GKE_PROJECT_ID": "mgmt-proj",
    "GKE_LOCATION": "us-central1",
    "GKE_CLUSTER_NAME": "platform-agent-host",
}
OWN_NETWORK_FORMAT = "--format=value(networkConfig.network,networkConfig.subnetwork,clusterIpv4Cidr)"
# What the own-cluster describe answers: the three fields, tab-separated, as
# gcloud's value() renders them.
OWN_ROW = f"{OWN_NETWORK}\t{OWN_SUBNETWORK}\t{OWN_POD_CIDR}"

# The enterprise shape this change exists for: private nodes, public endpoint on
# but Master Authorized Networks restricted to corporate ranges, DNS endpoint
# closed. The private endpoint is one hop away on the VPC the agent sits in.
PRIVATE_SAME_VPC = {
    "controlPlaneEndpointsConfig": {
        "dnsEndpointConfig": {
            "allowExternalTraffic": False,
            "endpoint": "gke-0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b-123456789012.us-central1.gke.goog",
        },
        "ipEndpointsConfig": {
            "authorizedNetworksConfig": {
                "cidrBlocks": [{"cidrBlock": "10.0.0.0/8", "displayName": "corp"},
                               {"cidrBlock": "172.16.0.0/12", "displayName": "vpn"}],
                "enabled": True,
                "privateEndpointEnforcementEnabled": True,
            },
            "enablePublicEndpoint": True,
            "enabled": True,
            "privateEndpoint": "10.10.0.2",
            "publicEndpoint": "203.0.113.10",
        },
    },
    "endpoint": "203.0.113.10",
    "masterAuthorizedNetworksConfig": {
        "cidrBlocks": [{"cidrBlock": "10.0.0.0/8", "displayName": "corp"},
                       {"cidrBlock": "172.16.0.0/12", "displayName": "vpn"}],
        "enabled": True,
        "privateEndpointEnforcementEnabled": True,
    },
    "networkConfig": {"network": OWN_NETWORK, "subnetwork": TARGET_SUBNETWORK},
    "privateClusterConfig": {
        "enablePrivateNodes": True,
        "privateEndpoint": "10.10.0.2",
        "publicEndpoint": "203.0.113.10",
    },
}


def _variant(base, **changes):
    """A deep copy of `base` with top-level keys replaced."""
    document = json.loads(json.dumps(base))
    document.update(changes)
    return document


PRIVATE_OTHER_VPC = _variant(PRIVATE_SAME_VPC, networkConfig={"network": OTHER_NETWORK})

PRIVATE_SAME_VPC_DNS_OPEN = _variant(
    PRIVATE_SAME_VPC,
    controlPlaneEndpointsConfig={
        **PRIVATE_SAME_VPC["controlPlaneEndpointsConfig"],
        "dnsEndpointConfig": {
            "allowExternalTraffic": True,
            "endpoint": "gke-0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b-123456789012.us-central1.gke.goog",
        },
    },
)

# IP endpoints switched off: gcloud refuses --internal-ip outright
# (IPEndpointsIsDisabledError) and selects the DNS endpoint on its own.
PRIVATE_SAME_VPC_IP_DISABLED = _variant(
    PRIVATE_SAME_VPC,
    controlPlaneEndpointsConfig={
        **PRIVATE_SAME_VPC["controlPlaneEndpointsConfig"],
        "ipEndpointsConfig": {**PRIVATE_SAME_VPC["controlPlaneEndpointsConfig"]["ipEndpointsConfig"],
                              "enabled": False},
    },
)

# Same network, but the cluster reports no private endpoint at all.
SAME_VPC_NO_PRIVATE_ENDPOINT = _variant(
    PRIVATE_SAME_VPC,
    privateClusterConfig={},
    controlPlaneEndpointsConfig={
        **PRIVATE_SAME_VPC["controlPlaneEndpointsConfig"],
        "ipEndpointsConfig": {"enabled": True, "enablePublicEndpoint": True,
                              "publicEndpoint": "203.0.113.10"},
    },
)

# Authorized networks switched on with no ranges listed: still restricted.
PRIVATE_SAME_VPC_EMPTY_LIST = _variant(
    PRIVATE_SAME_VPC,
    masterAuthorizedNetworksConfig={"enabled": True, "privateEndpointEnforcementEnabled": True},
)


def _with_authorized(base, blocks, enforced=True, **more):
    """`base` with both copies of the authorized-networks block replaced."""
    man = {"enabled": True, "privateEndpointEnforcementEnabled": enforced,
           "cidrBlocks": [{"cidrBlock": b} for b in blocks]}
    endpoints = json.loads(json.dumps(base["controlPlaneEndpointsConfig"]))
    endpoints["ipEndpointsConfig"]["authorizedNetworksConfig"] = dict(man)
    return _variant(base, masterAuthorizedNetworksConfig=dict(man),
                    controlPlaneEndpointsConfig=endpoints, **more)


# The estate that followed the old remedy: only the agent's NAT address is
# listed, the private endpoint enforces the list, and the agent's Pod range is
# not on it. The public IP works today and the private one would not.
PRIVATE_SAME_VPC_NAT_LISTED = _with_authorized(PRIVATE_SAME_VPC, ["203.0.113.5/32"])

# Same list, but the target sits in the agent cluster's own subnet, whose
# ranges GKE always admits on the private endpoint.
PRIVATE_SAME_SUBNET_NAT_LISTED = _with_authorized(
    PRIVATE_SAME_VPC, ["203.0.113.5/32"],
    networkConfig={"network": OWN_NETWORK, "subnetwork": OWN_SUBNETWORK},
)

# Same list, but the private endpoint explicitly does not enforce it.
PRIVATE_SAME_VPC_NOT_ENFORCED = _with_authorized(PRIVATE_SAME_VPC, ["203.0.113.5/32"], enforced=False)


def _without_enforcement_field(base):
    """`base` with privateEndpointEnforcementEnabled absent from both copies."""
    document = json.loads(json.dumps(base))
    document["masterAuthorizedNetworksConfig"].pop("privateEndpointEnforcementEnabled", None)
    document["controlPlaneEndpointsConfig"]["ipEndpointsConfig"]["authorizedNetworksConfig"].pop(
        "privateEndpointEnforcementEnabled", None)
    return document


# Same NAT-only list, enforcement field absent: a legacy cluster whose server-side
# default is not documented, so it is read as enforced.
PRIVATE_SAME_VPC_ENFORCEMENT_UNKNOWN = _without_enforcement_field(PRIVATE_SAME_VPC_NAT_LISTED)

# A public endpoint nothing restricts: it works today, so nothing moves it.
PRIVATE_SAME_VPC_LIST_OFF = _variant(
    PRIVATE_SAME_VPC,
    masterAuthorizedNetworksConfig={},
    controlPlaneEndpointsConfig={
        **PRIVATE_SAME_VPC["controlPlaneEndpointsConfig"],
        "ipEndpointsConfig": {
            **{k: v for k, v in PRIVATE_SAME_VPC["controlPlaneEndpointsConfig"]["ipEndpointsConfig"].items()
               if k != "authorizedNetworksConfig"},
            "authorizedNetworksConfig": {},
        },
    },
)

# No public endpoint at all and no list: gcloud's default is already the private
# IP, and rule 3 says so explicitly.
PRIVATE_ONLY_LIST_OFF = _variant(
    PRIVATE_SAME_VPC_LIST_OFF,
    endpoint="10.10.0.2",
    controlPlaneEndpointsConfig={
        **PRIVATE_SAME_VPC_LIST_OFF["controlPlaneEndpointsConfig"],
        "ipEndpointsConfig": {**PRIVATE_SAME_VPC_LIST_OFF["controlPlaneEndpointsConfig"]["ipEndpointsConfig"],
                              "enablePublicEndpoint": False},
    },
    privateClusterConfig={"enablePrivateNodes": True, "enablePrivateEndpoint": True,
                          "privateEndpoint": "10.10.0.2"},
)

# ipEndpointsConfig present but without `enabled`: gcloud reads that as disabled.
PRIVATE_SAME_VPC_IP_ENABLED_ABSENT = _variant(
    PRIVATE_SAME_VPC,
    controlPlaneEndpointsConfig={
        **PRIVATE_SAME_VPC["controlPlaneEndpointsConfig"],
        "ipEndpointsConfig": {k: v for k, v in PRIVATE_SAME_VPC["controlPlaneEndpointsConfig"]["ipEndpointsConfig"].items()
                              if k != "enabled"},
    },
)

# Control-plane global access on: the private endpoint answers from any region.
PRIVATE_SAME_VPC_GLOBAL = _variant(
    PRIVATE_SAME_VPC,
    privateClusterConfig={**PRIVATE_SAME_VPC["privateClusterConfig"],
                          "masterGlobalAccessConfig": {"enabled": True}},
)


class FakeRunner:
    """Answers the help probe and the describe, and records what it was asked."""

    def __init__(self, describe=None, help_text=HELP_WITH_FLAG, describe_exit=0,
                 own_network=OWN_ROW):
        self.describe = describe
        self.help_text = help_text
        self.describe_exit = describe_exit
        # The row the agent's own cluster describe answers; None makes that
        # describe fail, "" makes it answer an empty line.
        self.own_network = own_network
        self.help_exit = 0
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        if "--help" in argv:
            return self.help_exit, ("" if self.help_exit else self.help_text)
        if OWN_NETWORK_FORMAT in argv:
            if self.own_network is None:
                return 1, ""
            return 0, self.own_network + "\n"
        if "describe" in argv:
            if self.describe_exit != 0:
                return self.describe_exit, ""
            payload = self.describe if isinstance(self.describe, str) else json.dumps(self.describe)
            return 0, payload
        raise AssertionError(f"unexpected command: {argv}")

    @property
    def describe_calls(self):
        return [c for c in self.calls if "describe" in c and OWN_NETWORK_FORMAT not in c]

    @property
    def own_network_calls(self):
        return [c for c in self.calls if OWN_NETWORK_FORMAT in c]


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
    """Run the flag-only decision with a clean cache, no own-cluster identity in
    the environment, and stderr swallowed."""
    gke_endpoint.reset_cache()
    scrubbed = {k: v for k, v in os.environ.items() if k not in OWN_CLUSTER_ENV}
    with unittest.mock.patch.dict(os.environ, scrubbed, clear=True), \
            redirect_stderr(io.StringIO()):
        return gke_endpoint.dns_endpoint_args(project, cluster, location, run=runner)


def decision(runner, project="p", cluster="c", location="us-central1", env=None):
    """Run the structured decision with a clean cache, stderr swallowed, and the
    agent's own identity in the environment unless `env` says otherwise."""
    gke_endpoint.reset_cache()
    environment = {**os.environ, **OWN_CLUSTER_ENV} if env is None else env
    with unittest.mock.patch.dict(os.environ, environment, clear=True), \
            redirect_stderr(io.StringIO()):
        return gke_endpoint.endpoint_decision(project, cluster, location, run=runner)


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
    def test_old_gcloud_is_a_settled_answer_not_an_undecided_one(self):
        # The installed gcloud cannot grow the flag while we run, so "no flag"
        # from it is final: a decision, empty and settled, rather than None,
        # which the credential proxy would otherwise read as "decide again in a
        # minute" for every cluster for the life of the pod.
        runner = FakeRunner(DNS_EXTERNAL, help_text=HELP_WITHOUT_FLAG)
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            gke_endpoint.reset_cache()
            d = gke_endpoint.endpoint_decision("p", "c", "us-central1", run=runner)
        self.assertIsNotNone(d)
        self.assertEqual(d.flags, ())
        self.assertFalse(d.provisional)
        self.assertEqual(d.address, "", "nothing was described, so nothing is claimed")
        self.assertEqual(runner.describe_calls, [])

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
    def test_a_probe_that_could_not_answer_leaves_the_decision_undecided(self):
        # Three things make the support predicate say False, and only one of
        # them is settled: the help text that lacks the flag. A probe that
        # exited non-zero or could not run says nothing about which gcloud is
        # installed, so the decision is None -- undecided, which the credential
        # proxy marks provisional -- not the settled empty answer, which it
        # would file for the life of the pod.
        gke_endpoint.reset_cache()
        runner = FakeRunner(PRIVATE_SAME_VPC)
        runner.help_exit = 1
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            self.assertIsNone(gke_endpoint.endpoint_decision("p", "c", "us-central1", run=runner))
        self.assertIsNone(gke_endpoint._support_cache, "a probe that did not answer is not memoised")

        class Raising(FakeRunner):
            def __call__(self, argv):
                if "--help" in argv:
                    raise OSError("no gcloud")
                return super().__call__(argv)

        gke_endpoint.reset_cache()
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            self.assertIsNone(gke_endpoint.endpoint_decision("p", "c", "us-central1", run=Raising(PRIVATE_SAME_VPC)))

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


class PrivateEndpointTest(unittest.TestCase):
    """Rule 3 of docs/designs/private-endpoint-selection.md: --internal-ip when the
    target is on the agent's own network and publishes a private endpoint."""

    def test_same_network_private_cluster_gets_internal_ip(self):
        d = decision(FakeRunner(PRIVATE_SAME_VPC))
        self.assertEqual(d.flags, (gke_endpoint.INTERNAL_IP_FLAG,))
        self.assertEqual(d.kind, gke_endpoint.KIND_INTERNAL_IP)
        self.assertEqual(d.address, "10.10.0.2")
        self.assertIs(d.same_network, True)
        self.assertEqual(d.authorized_networks, ("10.0.0.0/8", "172.16.0.0/12"))
        # Chosen because the list admits the agent's Pod range, so nothing to add.
        self.assertEqual(d.remedy, "")

    def test_the_flag_only_wrapper_returns_internal_ip_too(self):
        gke_endpoint.reset_cache()
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            args = gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=FakeRunner(PRIVATE_SAME_VPC))
        self.assertEqual(args, ["--internal-ip"])

    def test_an_open_dns_endpoint_still_wins_on_the_same_network(self):
        d = decision(FakeRunner(PRIVATE_SAME_VPC_DNS_OPEN))
        self.assertEqual(d.flags, ("--dns-endpoint",))
        self.assertEqual(d.kind, gke_endpoint.KIND_DNS)
        self.assertEqual(d.remedy, "")

    def test_another_network_gets_no_flag(self):
        d = decision(FakeRunner(PRIVATE_OTHER_VPC))
        self.assertEqual(d.flags, ())
        self.assertEqual(d.kind, gke_endpoint.KIND_IP)
        self.assertEqual(d.address, "203.0.113.10")
        self.assertIs(d.same_network, False)
        self.assertIn("not on this cluster's VPC", d.remedy)

    def test_same_network_without_a_private_endpoint_gets_no_flag(self):
        d = decision(FakeRunner(SAME_VPC_NO_PRIVATE_ENDPOINT))
        self.assertEqual(d.flags, ())
        self.assertEqual(d.kind, gke_endpoint.KIND_IP)

    def test_ip_endpoints_disabled_is_the_dns_endpoint_with_no_flag(self):
        # gcloud picks the DNS endpoint by itself here and would refuse --internal-ip,
        # so the kubeconfig names the DNS host and authorized networks do not apply.
        d = decision(FakeRunner(PRIVATE_SAME_VPC_IP_DISABLED))
        self.assertEqual(d.flags, ())
        self.assertEqual(d.kind, gke_endpoint.KIND_DNS)
        self.assertTrue(d.address.endswith(".gke.goog"))
        self.assertEqual(d.remedy, "")

    def test_another_region_without_global_access_gets_no_flag(self):
        # The private endpoint answers only from its own region unless control-plane
        # global access is on; a same-VPC cluster elsewhere keeps the public IP.
        d = decision(FakeRunner(PRIVATE_SAME_VPC), location="europe-west1")
        self.assertEqual(d.flags, ())
        self.assertEqual(d.kind, gke_endpoint.KIND_IP)

    def test_another_region_without_global_access_names_global_access_in_the_remedy(self):
        # Adding an address cannot make a private endpoint answer from another
        # region; the remedy has to name control-plane global access instead.
        d = decision(FakeRunner(PRIVATE_SAME_VPC), location="europe-west1")
        self.assertIn("global access", d.remedy)
        self.assertNotIn("egress address", d.remedy)

    def test_another_region_with_global_access_gets_internal_ip(self):
        d = decision(FakeRunner(PRIVATE_SAME_VPC_GLOBAL), location="europe-west1")
        self.assertEqual(d.flags, ("--internal-ip",))

    def test_a_zonal_location_in_the_same_region_counts_as_the_same_region(self):
        d = decision(FakeRunner(PRIVATE_SAME_VPC), location="us-central1-a")
        self.assertEqual(d.flags, ("--internal-ip",))

    def test_a_zone_with_a_long_suffix_is_still_its_region(self):
        # The first two segments are the region, whatever the zone suffix is.
        d = decision(FakeRunner(PRIVATE_SAME_VPC), location="us-central1-ai1a")
        self.assertEqual(d.flags, ("--internal-ip",))

    def test_an_empty_ip_endpoints_block_reads_as_disabled(self):
        # gcloud tests the block for `is not None`, then `not enabled`.
        shape = _variant(PRIVATE_SAME_VPC, controlPlaneEndpointsConfig={
            **PRIVATE_SAME_VPC["controlPlaneEndpointsConfig"], "ipEndpointsConfig": {}})
        d = decision(FakeRunner(shape))
        self.assertEqual(d.flags, ())
        self.assertEqual(d.kind, gke_endpoint.KIND_DNS)

    def test_a_private_only_cluster_in_another_region_without_a_list_names_global_access(self):
        # No list, no public endpoint, same VPC, other region: unreachable
        # today, and only global access or the DNS endpoint can change that.
        d = decision(FakeRunner(PRIVATE_ONLY_LIST_OFF), location="europe-west1")
        self.assertEqual(d.flags, ())
        self.assertIn("global access", d.remedy)

    def test_a_private_only_cluster_on_another_network_gets_a_remedy_that_can_work(self):
        # No public endpoint, so an address on the list helps nobody; the DNS
        # endpoint is the way in, and the remedy must lead with it.
        shape = _variant(PRIVATE_ONLY_LIST_OFF, networkConfig={"network": OTHER_NETWORK})
        d = decision(FakeRunner(shape))
        self.assertEqual(d.flags, ())
        self.assertIn("--enable-dns-access", d.remedy)
        self.assertNotIn("egress address", d.remedy)

    def test_a_list_that_admits_only_the_nat_address_keeps_the_public_ip(self):
        # The estate that followed the old remedy must not regress on upgrade.
        d = decision(FakeRunner(PRIVATE_SAME_VPC_NAT_LISTED))
        self.assertEqual(d.flags, ())
        self.assertEqual(d.kind, gke_endpoint.KIND_IP)
        self.assertEqual(d.address, "203.0.113.10")
        self.assertIn(OWN_POD_CIDR, d.remedy, "the remedy names the range to add")
        self.assertIn("private endpoint", d.remedy)

    def test_the_same_subnet_is_admitted_whatever_the_list_says(self):
        d = decision(FakeRunner(PRIVATE_SAME_SUBNET_NAT_LISTED))
        self.assertEqual(d.flags, ("--internal-ip",))

    def test_a_private_endpoint_that_does_not_enforce_the_list_is_admitted(self):
        d = decision(FakeRunner(PRIVATE_SAME_VPC_NOT_ENFORCED))
        self.assertEqual(d.flags, ("--internal-ip",))
        self.assertEqual(d.remedy, "", "the list does not gate the endpoint chosen")

    def test_a_private_endpoint_only_in_the_nested_block_gets_no_flag(self):
        # gcloud reads privateClusterConfig.privateEndpoint alone before it
        # accepts --internal-ip, so the nested copy must not trigger the flag.
        shape = _variant(PRIVATE_SAME_VPC, privateClusterConfig={"enablePrivateNodes": True})
        d = decision(FakeRunner(shape))
        self.assertEqual(d.flags, ())

    def test_global_access_in_the_nested_block_counts(self):
        endpoints = json.loads(json.dumps(PRIVATE_SAME_VPC["controlPlaneEndpointsConfig"]))
        endpoints["ipEndpointsConfig"]["globalAccess"] = True
        d = decision(FakeRunner(_variant(PRIVATE_SAME_VPC, controlPlaneEndpointsConfig=endpoints)),
                     location="europe-west1")
        self.assertEqual(d.flags, ("--internal-ip",))

    def test_an_own_describe_without_the_pod_range_is_not_admitted_by_the_list(self):
        # Two of three fields: the network and subnetwork compare, the range cannot.
        runner = FakeRunner(PRIVATE_SAME_VPC, own_network=f"{OWN_NETWORK}\t{OWN_SUBNETWORK}\t")
        d = decision(runner)
        self.assertEqual(d.flags, ())

    def test_without_an_own_cluster_identity_the_rule_is_inert(self):
        runner = FakeRunner(PRIVATE_SAME_VPC)
        d = decision(runner, env={k: v for k, v in os.environ.items() if k not in OWN_CLUSTER_ENV})
        self.assertEqual(d.flags, ())
        self.assertIsNone(d.same_network)
        self.assertEqual(runner.own_network_calls, [], "no identity, so nothing to describe")

    def test_a_partial_own_cluster_identity_is_no_identity(self):
        partial = {k: v for k, v in os.environ.items() if k not in OWN_CLUSTER_ENV}
        partial["GKE_PROJECT_ID"] = "mgmt-proj"
        runner = FakeRunner(PRIVATE_SAME_VPC)
        self.assertEqual(decision(runner, env=partial).flags, ())
        self.assertEqual(runner.own_network_calls, [])

    def test_a_public_cluster_never_describes_the_own_cluster(self):
        # The own-cluster describe is paid only when rule 3 could fire.
        runner = FakeRunner(DNS_EXTERNAL)
        decision(runner)
        self.assertEqual(runner.own_network_calls, [])

    def test_enabled_authorized_networks_with_no_blocks_reads_as_restricted(self):
        d = decision(FakeRunner(PRIVATE_SAME_VPC_EMPTY_LIST))
        self.assertEqual(d.authorized_networks, ())
        self.assertEqual(d.flags, (), "an empty list admits no range of ours")
        self.assertNotEqual(d.remedy, "")

    def test_a_public_endpoint_nothing_restricts_is_left_alone(self):
        # The issue is a public endpoint the list blocks. One the list does not
        # gate works today, and moving it to the private endpoint could only
        # break it (a firewall that denies internal egress, for one).
        d = decision(FakeRunner(PRIVATE_SAME_VPC_LIST_OFF))
        self.assertEqual(d.flags, ())
        self.assertEqual(d.kind, gke_endpoint.KIND_IP)
        self.assertIsNone(d.authorized_networks)
        self.assertEqual(d.remedy, "")

    def test_a_private_only_cluster_with_no_list_gets_internal_ip(self):
        d = decision(FakeRunner(PRIVATE_ONLY_LIST_OFF))
        self.assertEqual(d.flags, ("--internal-ip",))
        self.assertEqual(d.remedy, "")

    def test_an_absent_enforcement_field_is_read_as_enforced(self):
        # The server-side default on a cluster that omits the field is not
        # documented; reading it as enforced can only keep today's endpoint.
        d = decision(FakeRunner(PRIVATE_SAME_VPC_ENFORCEMENT_UNKNOWN))
        self.assertEqual(d.flags, ())
        self.assertIn(OWN_POD_CIDR, d.remedy)

    def test_an_ip_endpoints_block_without_enabled_reads_as_disabled(self):
        # gcloud's own test is `not ipEndpointsConfig.enabled`, so a missing
        # value refuses --internal-ip too.
        d = decision(FakeRunner(PRIVATE_SAME_VPC_IP_ENABLED_ABSENT))
        self.assertEqual(d.flags, ())
        self.assertEqual(d.kind, gke_endpoint.KIND_DNS)

    def test_a_failed_describe_yields_none(self):
        self.assertIsNone(decision(FakeRunner(describe_exit=1)))


class OwnNetworkTest(unittest.TestCase):
    def setUp(self):
        gke_endpoint.reset_cache()
        self.addCleanup(gke_endpoint.reset_cache)

    def test_the_own_cluster_is_described_once_per_process(self):
        runner = FakeRunner(PRIVATE_SAME_VPC)
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
            gke_endpoint.endpoint_decision("p", "b", "us-central1", run=runner)
        self.assertEqual(len(runner.own_network_calls), 1)
        self.assertEqual(len(runner.describe_calls), 2)

    def test_the_own_cluster_describe_names_the_identity_from_the_environment(self):
        runner = FakeRunner(PRIVATE_SAME_VPC)
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            gke_endpoint.endpoint_decision("p", "c", "us-central1", run=runner)
        argv = runner.own_network_calls[0]
        self.assertEqual(argv[:5], ["gcloud", "container", "clusters", "describe", "platform-agent-host"])
        self.assertIn("--location=us-central1", argv)
        self.assertIn("--project=mgmt-proj", argv)
        self.assertIn(OWN_NETWORK_FORMAT, argv)

    def test_a_malformed_own_row_is_not_cached(self):
        # One field where three were asked for: unknown, and retried next time.
        runner = FakeRunner(PRIVATE_SAME_VPC, own_network=OWN_NETWORK)
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            first = gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
            gke_endpoint._own_failure_at = None
            runner.own_network = OWN_ROW
            second = gke_endpoint.endpoint_decision("p", "b", "us-central1", run=runner)
        self.assertEqual(first.flags, ())
        self.assertEqual(second.flags, ("--internal-ip",))

    def test_a_failed_own_cluster_describe_is_not_cached_as_an_answer(self):
        # The failure is remembered only as a backoff, never as "no cluster":
        # once the window passes the next target asks again and gets the answer.
        runner = FakeRunner(PRIVATE_SAME_VPC, own_network=None)
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            first = gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
            runner.own_network = OWN_ROW
            gke_endpoint._own_failure_at = None
            second = gke_endpoint.endpoint_decision("p", "b", "us-central1", run=runner)
        self.assertEqual(first.flags, ())
        self.assertIsNone(first.same_network)
        self.assertEqual(second.flags, ("--internal-ip",))
        self.assertEqual(len(runner.own_network_calls), 2)

    def test_a_provisional_decision_is_cached_for_the_window_like_any_other(self):
        # A persistently failing own describe must not cost every caller a
        # target describe per call; the mark travels with the cached answer,
        # and the credential proxy refetches on the mark, not on the cache.
        runner = FakeRunner(PRIVATE_SAME_VPC, own_network=None)
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            first = gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
            second = gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
        self.assertTrue(first.provisional and second.provisional)
        self.assertEqual(len(runner.describe_calls), 1, "served from the cache the second time")

    def test_a_provisional_decision_expires_with_the_window_and_is_re_decided(self):
        runner = FakeRunner(PRIVATE_SAME_VPC, own_network=None)
        with expired_cache(), unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            first = gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
            runner.own_network = OWN_ROW
            gke_endpoint._own_failure_at = None  # the backoff window, see the next test
            second = gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
        self.assertTrue(first.provisional)
        self.assertEqual(second.flags, ("--internal-ip",))

    def test_a_failed_own_describe_is_not_retried_inside_its_backoff_window(self):
        # Distinct targets inside the window share the one failure: one gcloud
        # start for the own cluster per window, not one per target.
        runner = FakeRunner(PRIVATE_SAME_VPC, own_network=None)
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
            gke_endpoint.endpoint_decision("p", "b", "us-central1", run=runner)
            gke_endpoint.endpoint_decision("p", "c", "us-central1", run=runner)
        self.assertEqual(len(runner.own_network_calls), 1)
        self.assertEqual(len(runner.describe_calls), 3)

    def test_a_caller_that_writes_a_record_can_ask_past_the_backoff(self):
        # The scaffold asks once per cluster and records the answer for the
        # profile's life, so it must not inherit a per-request backoff set a
        # moment earlier, nor the provisional answer that backoff cached for
        # this same cluster.
        runner = FakeRunner(PRIVATE_SAME_VPC, own_network=None)
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
            runner.own_network = OWN_ROW
            d = gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner, retry_own=True)
        self.assertEqual(len(runner.own_network_calls), 2)
        self.assertEqual(d.flags, ("--internal-ip",))
        self.assertFalse(d.provisional)

    def test_the_own_describe_is_retried_once_the_backoff_window_has_passed(self):
        runner = FakeRunner(PRIVATE_SAME_VPC, own_network=None)
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
            gke_endpoint._own_failure_at -= gke_endpoint._OWN_RETRY_SECONDS + 1
            runner.own_network = OWN_ROW
            d = gke_endpoint.endpoint_decision("p", "b", "us-central1", run=runner)
        self.assertEqual(len(runner.own_network_calls), 2)
        self.assertEqual(d.flags, ("--internal-ip",))

    def test_a_failed_own_describe_marks_the_decision_provisional(self):
        runner = FakeRunner(PRIVATE_SAME_VPC, own_network=None)
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            d = gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
        self.assertTrue(d.provisional)
        with expired_cache(), unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            runner.own_network = OWN_ROW
            gke_endpoint._own_failure_at = None
            d = gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
        self.assertFalse(d.provisional)

    def test_an_absent_identity_is_a_settled_answer_and_is_cached(self):
        # A workstation or a test has no own cluster to wait for; re-describing
        # the target on every call would only cost gcloud starts.
        scrubbed = {k: v for k, v in os.environ.items() if k not in OWN_CLUSTER_ENV}
        runner = FakeRunner(PRIVATE_SAME_VPC)
        with unittest.mock.patch.dict(os.environ, scrubbed, clear=True), redirect_stderr(io.StringIO()):
            first = gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
            gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
        self.assertFalse(first.provisional)
        self.assertEqual(len(runner.describe_calls), 1, "served from the cache the second time")

    def test_a_literal_placeholder_is_no_identity(self):
        # Hermes hands an MCP server the literal "${GKE_PROJECT_ID}" when the
        # variable is unset; that is not a cluster to describe.
        placeholders = {name: "${%s}" % name for name in OWN_CLUSTER_ENV}
        runner = FakeRunner(PRIVATE_SAME_VPC)
        with unittest.mock.patch.dict(os.environ, placeholders), redirect_stderr(io.StringIO()):
            d = gke_endpoint.endpoint_decision("p", "c", "us-central1", run=runner)
        self.assertEqual(d.flags, ())
        self.assertEqual(runner.own_network_calls, [])

    def test_an_empty_own_network_answer_is_not_cached(self):
        runner = FakeRunner(PRIVATE_SAME_VPC, own_network="")
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            first = gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
            gke_endpoint._own_failure_at = None
            runner.own_network = OWN_ROW
            second = gke_endpoint.endpoint_decision("p", "b", "us-central1", run=runner)
        self.assertEqual(first.flags, ())
        self.assertEqual(second.flags, ("--internal-ip",))

    def test_a_runner_that_raises_on_the_own_describe_is_not_fatal(self):
        class Raising(FakeRunner):
            def __call__(self, argv):
                if OWN_NETWORK_FORMAT in argv:
                    raise OSError("no gcloud")
                return super().__call__(argv)

        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            d = gke_endpoint.endpoint_decision("p", "c", "us-central1", run=Raising(PRIVATE_SAME_VPC))
        self.assertEqual(d.flags, ())
        self.assertIsNone(d.same_network)

    def test_reset_cache_forgets_the_own_network(self):
        runner = FakeRunner(PRIVATE_SAME_VPC)
        with unittest.mock.patch.dict(os.environ, OWN_CLUSTER_ENV), redirect_stderr(io.StringIO()):
            gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
            gke_endpoint.reset_cache()
            gke_endpoint.endpoint_decision("p", "a", "us-central1", run=runner)
        self.assertEqual(len(runner.own_network_calls), 2)


class DescribeFormatTest(unittest.TestCase):
    def test_the_target_describe_asks_for_every_field_the_rule_reads(self):
        runner = FakeRunner(PRIVATE_SAME_VPC)
        decision(runner)
        fmt = [a for a in runner.describe_calls[0] if a.startswith("--format=")][0]
        # gcloud's json() projection keeps only the keys named, so a nested key
        # the rule reads has to be asked for by name or by its parent block.
        for field in ("controlPlaneEndpointsConfig", "privateClusterConfig",
                      "networkConfig.network", "networkConfig.subnetwork",
                      "masterAuthorizedNetworksConfig", "endpoint"):
            self.assertIn(field, fmt)


if __name__ == "__main__":
    unittest.main()
