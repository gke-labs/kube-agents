"""Decide whether a cluster should be reached over its DNS-based control plane.

`gcloud container clusters get-credentials` writes the IP endpoint into the
kubeconfig unless `--dns-endpoint` is passed. For a cluster whose IP endpoint we
cannot route to — no public endpoint, and the agent is outside the VPC — that
kubeconfig is useless, and the DNS endpoint (`*.gke.goog`) is the way in.

The flag is not safe to pass unconditionally. gcloud rejects it on a cluster with
no DNS endpoint configured (`MissingDnsEndpointConfigError`) and on one whose
`allowExternalTraffic` is off (`AllowExternalTrafficIsDisabledError`), so an
always-on flag would break clusters that work today.

**It is equally unsafe to pass the flag and fall back when gcloud complains.**
For a caller Google recognises as internal, gcloud downgrades that second error
to a warning and writes a kubeconfig pointing at the DNS endpoint anyway
(`googlecloudsdk/api_lib/container/util.py`, the `_IsGoogleInternalUser` branch).
The command exits 0; the kubeconfig it produced then answers every request with
HTTP 403 from Google's frontend. Probing by attempting the flag therefore reports
success precisely where it is most wrong, so this module reads the cluster's
configuration up front instead.

One case needs no help from us: when the IP endpoint is disabled outright, recent
gcloud already selects the DNS endpoint on its own. What this module adds is the
cluster that has both endpoints, where gcloud would pick the IP one.

A third answer joined the two above: `--internal-ip`, for a cluster on the same
VPC as the agent's own cluster that publishes a private endpoint and whose DNS
endpoint is closed. docs/designs/private-endpoint-selection.md argues the rule;
`_decide` below is the rule.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import subprocess
import sys
import time
from typing import Callable

import sandbox_exec

# (exit_code, stdout) — narrow enough that the credential proxy can satisfy it by
# wrapping its own executor, which runs commands in the sidecar rather than here.
Runner = Callable[[list[str]], "tuple[int, str]"]

DNS_ENDPOINT_FLAG = "--dns-endpoint"
INTERNAL_IP_FLAG = "--internal-ip"

# What `EndpointDecision.kind` says the kubeconfig will name.
KIND_DNS = "dns"
KIND_INTERNAL_IP = "internal-ip"
KIND_IP = "ip"

# The rendering of "no authorized-networks restriction" wherever the decision
# is written out for a human (USER.md, the scaffold log).
AUTHORIZED_NETWORKS_UNRESTRICTED = "unrestricted"
AUTHORIZED_NETWORKS_EMPTY = "enabled, no ranges listed"

# The agent's own cluster, as the operator sets it on the agent container and
# the sandbox and the credential proxy forward it. All three or nothing: a
# partial identity describes nothing.
OWN_CLUSTER_ENV = ("GKE_PROJECT_ID", "GKE_LOCATION", "GKE_CLUSTER_NAME")

# Every field `_decide` reads. `endpoint` is the address gcloud writes when no
# flag is passed, carried so the decision can say what the kubeconfig names.
# gcloud's json() projection keeps only the keys named, so every nested key
# the rule reads is listed; a block named whole would carry keys it ignores.
_DESCRIBE_FORMAT = (
    "json(controlPlaneEndpointsConfig,privateClusterConfig,"
    "networkConfig.network,networkConfig.subnetwork,"
    "masterAuthorizedNetworksConfig,endpoint)"
)
# The agent's own cluster: its VPC, its subnet, and its Pod range, which is the
# source address its traffic carries to a private endpoint (GKE does not
# masquerade RFC 1918 destinations). One describe, three tab-separated fields.
_OWN_NETWORK_FORMAT = "value(networkConfig.network,networkConfig.subnetwork,clusterIpv4Cidr)"
_OWN_FIELD_COUNT = 3
_VALUE_SEPARATOR = "\t"

# A GKE region is two segments (`us-central1`); a zone adds a third, whatever
# it is (`-a`, or `-ai1a`). The first two segments are the region.
_REGION_SEGMENTS = 2
_LOCATION_SEPARATOR = "-"
# Hermes hands a stdio MCP server the literal placeholder when the variable
# it names is unset; that is not an identity.
_UNEXPANDED_PLACEHOLDER_PREFIX = "${"

# The one-sentence remedies the decision carries for a cluster that may still
# refuse the connection. Composed here, once, so the scaffold log and the
# preflight card read the same words.
_ENABLE_DNS_ACCESS = "`gcloud container clusters update --enable-dns-access`"
REMEDY_IP = (
    "Add the agent's egress address to this cluster's authorized networks, or "
    f"open its DNS endpoint with {_ENABLE_DNS_ACCESS}."
)
REMEDY_OTHER_NETWORK = (
    "The agent is not on this cluster's VPC. Open the cluster's DNS endpoint with "
    f"{_ENABLE_DNS_ACCESS}, or, if the cluster has a public endpoint, add the address "
    "the agent leaves its VPC from to the cluster's authorized networks."
)
# Same VPC, but the private endpoint answers only from its own region. An
# address on the list cannot change that; global access or the DNS endpoint can.
REMEDY_OTHER_REGION = (
    "This cluster's private endpoint answers only from its own region. Enable "
    "control-plane global access with `gcloud container clusters update "
    "--enable-master-global-access` and re-run the onboarding, or open its DNS "
    f"endpoint with {_ENABLE_DNS_ACCESS}."
)
# Same VPC and region, but the list on the private endpoint does not admit the
# agent's Pod range. The range is spliced in so the operator has nothing to
# look up; `{pod_cidr}` reads "the agent cluster's Pod range" when unknown.
REMEDY_ADMIT_POD_RANGE = (
    "Add {pod_cidr} to this cluster's authorized networks and re-run the "
    "onboarding so the agent reaches it over the private endpoint, or open its "
    f"DNS endpoint with {_ENABLE_DNS_ACCESS}."
)
POD_RANGE_UNKNOWN = "the agent cluster's Pod range"

# gcloud is slow to start, so both answers are memoised — but only one of them
# keeps for the life of the process. The installed gcloud cannot grow a flag
# while we run, so its answer is remembered outright.
#
# A cluster's endpoint configuration can change under us, and this repository
# ships the instruction to change it: the `gke-networking` footer in
# `scripts/sync-upstream-skills.py` tells the agent that `clusters update
# --enable-dns-access` is the remedy for a closed endpoint, and the reverse is
# `--no-enable-dns-access`. Two of the three callers are long-lived — the MCP
# server and the credential proxy — so an answer kept for the life of the
# process outlasts the setting it describes: the documented remedy would appear
# to do nothing, and its reversal would keep the flag pointed at a control plane
# that has started answering 403. The endpoint answer therefore expires. The
# window is short enough that a change made by hand takes effect on the next
# call or two, and long enough that a burst of tool calls against one cluster
# still costs a single describe.
#
# Only answers gcloud actually gave are stored. A describe that failed, or a
# help probe that could not run, is retried on the next call: the credential
# proxy is a daemon, and "we could not find out" remembered as "no" would
# outlive its cause by the lifetime of the pod. For the same reason an expired
# entry whose refresh fails is served rather than discarded — it is still the
# last thing gcloud said about that cluster, so a transient error cannot demote
# a cluster that was reachable a minute ago to an IP endpoint it may not have.
_ENDPOINT_TTL_SECONDS = 60.0
# key -> (monotonic time the answer was read, decision)
_endpoint_cache: dict[tuple[str, str, str], tuple[float, "EndpointDecision"]] = {}
_support_cache: bool | None = None
# The agent's own cluster's network facts. The install's VPC cannot change
# under a running pod, so this has no TTL; it is simply never set from a
# failure. A failure is remembered only as a backoff: for _OWN_RETRY_SECONDS
# after one, decisions that need the own cluster do without it rather than
# paying a doomed gcloud start per target, and ask again once the window has
# passed. Never as "no cluster".
_own_cluster_cache: "OwnCluster | None" = None
_own_failure_at: float | None = None
_OWN_RETRY_SECONDS = 60.0

_DESCRIBE_TIMEOUT_SECONDS = 30
_HELP_TIMEOUT_SECONDS = 30


@dataclasses.dataclass(frozen=True)
class EndpointDecision:
    """Which control-plane endpoint a `get-credentials` for one cluster will name.

    `flags` is what to splice into the argv. `kind` is one of KIND_DNS,
    KIND_INTERNAL_IP, KIND_IP, and `address` the host that kind resolves to.
    `same_network` is True or False when the agent's own network was known and
    compared, None when it was not. `authorized_networks` is the CIDR list when
    Master Authorized Networks is enabled on the cluster (possibly empty) and
    None when it is not. `remedy` is one sentence naming what would make the
    cluster reachable if the connection still fails, or "" when there is
    nothing to suggest.
    """

    flags: tuple[str, ...]
    kind: str
    address: str
    same_network: bool | None
    authorized_networks: tuple[str, ...] | None
    remedy: str
    # True when the decision needed the agent's own cluster, the identity was
    # there, and the describe failed: gcloud's answer about the target, not
    # about the question. Cached for the usual window with the mark attached;
    # the credential proxy treats a kubeconfig written from one as a miss
    # after its own window, and the scaffold records nothing from one.
    provisional: bool = False

    def authorized_networks_text(self) -> str:
        if self.authorized_networks is None:
            return AUTHORIZED_NETWORKS_UNRESTRICTED
        return ", ".join(self.authorized_networks) or AUTHORIZED_NETWORKS_EMPTY


# The settled empty answer: a gcloud that lacks `--dns-endpoint` decides nothing
# about any cluster, and will not learn to while the process runs.
_NO_FLAG_DECISION = EndpointDecision((), KIND_IP, "", None, None, "")


@dataclasses.dataclass(frozen=True)
class OwnCluster:
    """What rule 3 knows about the cluster this process runs on."""

    network: str
    subnetwork: str
    pod_cidr: str
    region: str


def _log(message: str) -> None:
    print(f"gke_endpoint: {message}", file=sys.stderr, flush=True)


def _default_runner(env: dict[str, str] | None, timeout: int) -> Runner:
    """Run gcloud in the shell sandbox, deliberately without a KUBECONFIG.

    Both commands this module runs — `clusters describe` and `get-credentials
    --help` — talk to the GKE API and read no kubeconfig, so dropping the
    variable costs nothing. It is dropped rather than merely unused because
    `gcloud` here is the credential-proxy shim, which forwards `$KUBECONFIG` on
    *every* gcloud call (`credential_proxy_client.py`, `KUBECONFIG_AWARE`).
    `describe` is not `get-credentials`, so the proxy takes its read path and
    resolves that path through `_target_of`, which stats the file and rejects
    the request with HTTP 400 if it is not there.

    Every caller here passes the kubeconfig that the `get-credentials` being
    assembled is about to *create*, so it is reliably absent — forwarding it
    turned the describe into a guaranteed 400 and the detection into a constant
    "no flag". Callers may keep passing their own `env`; this strips the one key
    that must not travel.

    Nothing is forwarded to the sandbox, which satisfies that requirement by
    construction: the remote command inherits only what sshd sets. `env` now
    shapes the local fallback alone, and is kept because the operator's install
    scripts and the tests reach this module outside a sandboxed pod.
    """
    base = env if env is not None else {**os.environ, "HOME": "/tmp"}
    scrubbed = {key: value for key, value in base.items() if key != "KUBECONFIG"}

    def run(argv: list[str]) -> tuple[int, str]:
        completed = sandbox_exec.run(
            argv,
            local_env=scrubbed,
            timeout=timeout,
        )
        return completed.returncode, completed.stdout
    return run


def gcloud_supports_dns_endpoint(run: Runner | None = None) -> bool:
    """Does the gcloud on PATH understand `--dns-endpoint`?

    The agent image installs an unpinned `google-cloud-cli` from apt, so this is
    always true there. It is asked because the same helpers run from
    `scripts/installer/common.sh` on an operator's workstation, where gcloud
    is whatever they happen to have; an unrecognised flag is a hard argparse
    failure, which would turn "we could have used a better endpoint" into "the
    install stopped".
    """
    global _support_cache
    if _support_cache is not None:
        return _support_cache

    probe = run or _default_runner(None, _HELP_TIMEOUT_SECONDS)
    try:
        exit_code, stdout = probe(
            ["gcloud", "container", "clusters", "get-credentials", "--help"]
        )
    except (OSError, subprocess.SubprocessError,
            sandbox_exec.SandboxUnavailable) as error:
        # Not cached: this says the probe could not run, not that the flag is
        # absent. A transient failure remembered here would disable the endpoint
        # detection for the rest of a long-lived process.
        _log(f"could not probe gcloud for {DNS_ENDPOINT_FLAG} support ({error}); assuming absent")
        return False

    if exit_code != 0:
        _log(f"probing gcloud for {DNS_ENDPOINT_FLAG} support exited {exit_code}; assuming absent")
        return False

    _support_cache = DNS_ENDPOINT_FLAG in stdout
    if not _support_cache:
        _log(f"the installed gcloud does not offer {DNS_ENDPOINT_FLAG}; using the IP endpoint")
    return _support_cache


def _describe(
    project: str, cluster: str, location: str, run: Runner
) -> dict | None:
    argv = [
        "gcloud", "container", "clusters", "describe", cluster,
        f"--location={location}",
        f"--project={project}",
        f"--format={_DESCRIBE_FORMAT}",
    ]
    try:
        exit_code, stdout = run(argv)
    except (OSError, subprocess.SubprocessError,
            sandbox_exec.SandboxUnavailable) as error:
        _log(f"describing {cluster} failed ({error})")
        return None
    if exit_code != 0:
        _log(f"describing {cluster} exited {exit_code}")
        return None
    try:
        document = json.loads(stdout or "{}")
    except json.JSONDecodeError as error:
        _log(f"describing {cluster} returned unparseable JSON ({error})")
        return None
    return document if isinstance(document, dict) else None


def _region_of(location: str) -> str:
    """A zone's region, or the location itself when it already is one."""
    parts = (location or "").split(_LOCATION_SEPARATOR)
    if len(parts) > _REGION_SEGMENTS:
        return _LOCATION_SEPARATOR.join(parts[:_REGION_SEGMENTS])
    return location or ""


def _own_identity() -> tuple[str, str, str] | None:
    """(project, location, cluster) from OWN_CLUSTER_ENV, or None when any is
    unset or still the literal `${...}` placeholder."""
    identity = [os.environ.get(name, "") for name in OWN_CLUSTER_ENV]
    if not all(identity) or any(value.startswith(_UNEXPANDED_PLACEHOLDER_PREFIX) for value in identity):
        return None
    project, location, cluster = identity
    return project, location, cluster


def own_cluster(run: Runner, retry: bool = False) -> OwnCluster | None:
    """The network facts of the cluster this process runs on, or None.

    Read from the identity in OWN_CLUSTER_ENV with one `clusters describe`,
    remembered for the life of the process once gcloud has answered in full.
    None when the identity is absent or partial (a workstation, a test), when
    the describe fails, or when it answers fewer fields than asked. A failure
    is never cached as an answer, but it is remembered as a backoff: for
    `_OWN_RETRY_SECONDS` the describe is not repeated, which bounds what a
    cluster that cannot be described costs the per-request callers. `retry`
    ignores that backoff, for a caller that asks once and writes a record.
    """
    global _own_cluster_cache, _own_failure_at
    if _own_cluster_cache is not None:
        return _own_cluster_cache
    identity = _own_identity()
    if identity is None:
        return None
    if (not retry and _own_failure_at is not None
            and time.monotonic() - _own_failure_at < _OWN_RETRY_SECONDS):
        return None
    project, location, cluster = identity
    argv = [
        "gcloud", "container", "clusters", "describe", cluster,
        f"--location={location}",
        f"--project={project}",
        f"--format={_OWN_NETWORK_FORMAT}",
    ]
    try:
        exit_code, stdout = run(argv)
    except (OSError, subprocess.SubprocessError,
            sandbox_exec.SandboxUnavailable) as error:
        _log(f"describing this pod's own cluster {cluster} failed ({error})")
        _own_failure_at = time.monotonic()
        return None
    if exit_code != 0:
        _log(f"describing this pod's own cluster {cluster} exited {exit_code}")
        _own_failure_at = time.monotonic()
        return None
    # value() renders the fields tab-separated and an unset one empty; a row
    # with fewer separators than fields is not the answer that was asked for.
    fields = (stdout or "").strip("\r\n").split(_VALUE_SEPARATOR)
    if len(fields) != _OWN_FIELD_COUNT or not fields[0].strip():
        _log(f"this pod's own cluster {cluster} reports no network")
        _own_failure_at = time.monotonic()
        return None
    network, subnetwork, pod_cidr = (field.strip() for field in fields)
    _own_cluster_cache = OwnCluster(network, subnetwork, pod_cidr, _region_of(location))
    return _own_cluster_cache


def _within_any(cidr: str, blocks: tuple[str, ...]) -> bool:
    """Is `cidr` inside one of `blocks`? Unparseable input is a no."""
    try:
        inner = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    for block in blocks:
        try:
            if inner.subnet_of(ipaddress.ip_network(block, strict=False)):
                return True
        except (ValueError, TypeError):
            continue
    return False


def _decide(described: dict, location: str,
            own: Callable[[], OwnCluster | None], own_known: bool) -> EndpointDecision:
    """The rule, over one describe document, numbered as
    docs/designs/private-endpoint-selection.md numbers it. `own` is called only
    when rule 3 could fire, so an ordinary public cluster never pays for the
    second describe.

    `own_known` says whether an own-cluster identity exists at all. A decision
    that needed the own cluster, had the identity, and did not get the describe
    is marked `provisional`: gcloud's answer about the target but not about the
    question. An absent identity is a settled answer."""
    endpoints = described.get("controlPlaneEndpointsConfig") or {}
    dns = endpoints.get("dnsEndpointConfig") or {}
    ip = endpoints.get("ipEndpointsConfig") or {}
    private = described.get("privateClusterConfig") or {}
    # GKE reports the same block in both places; the top-level one is older
    # and present on every cluster shape, the nested one on newer ones.
    authorized = (described.get("masterAuthorizedNetworksConfig")
                  or ip.get("authorizedNetworksConfig") or {})
    restricted = authorized.get("enabled") is True
    networks = (
        tuple(block.get("cidrBlock", "") for block in authorized.get("cidrBlocks") or []
              if block.get("cidrBlock"))
        if restricted else None
    )
    # Only an explicit `false` says the list leaves the private endpoint
    # alone. New clusters report `true` without being asked; what a cluster
    # that omits the field does server-side is not documented, and reading it
    # as enforced can only keep today's endpoint.
    private_enforced = restricted and authorized.get("privateEndpointEnforcementEnabled") is not False
    public_endpoint = ip.get("enablePublicEndpoint") is not False
    # Only this field: gcloud's --internal-ip reads privateClusterConfig alone
    # (MissingPrivateEndpointError otherwise), so the nested copy must not be
    # what triggers the flag.
    private_endpoint = private.get("privateEndpoint") or ""
    global_access = ((private.get("masterGlobalAccessConfig") or {}).get("enabled") is True
                     or ip.get("globalAccess") is True)
    network_config = described.get("networkConfig") or {}
    network = network_config.get("network") or ""
    subnetwork = network_config.get("subnetwork") or ""
    default_address = described.get("endpoint") or ""

    # Rule 1. `allowExternalTraffic` absent is a no, not a maybe: clusters
    # predating the DNS endpoint omit the whole block, and the flag fails
    # against them.
    if dns.get("endpoint") and dns.get("allowExternalTraffic") is True:
        return EndpointDecision((DNS_ENDPOINT_FLAG,), KIND_DNS, dns["endpoint"],
                                None, networks, "")

    # Rule 2. With IP endpoints off gcloud writes the DNS host by itself and
    # refuses --internal-ip (IPEndpointsIsDisabledError); authorized networks
    # do not gate that host, so there is no network remedy to give. gcloud's
    # test is `not ipEndpointsConfig.enabled`, so a block that omits the
    # value counts as off, as does an empty block; a cluster old enough to
    # have no block at all is on.
    if "ipEndpointsConfig" in endpoints and not ip.get("enabled"):
        return EndpointDecision((), KIND_DNS, dns.get("endpoint") or default_address,
                                None, networks, "")

    # Rule 3. gcloud's other precondition for --internal-ip is a private
    # endpoint. Beyond that, the private endpoint has to be worth moving to:
    # the public one is gated by the list or absent (a public endpoint
    # nothing restricts works today, and moving it could only break it);
    # routable -- same VPC, and same region unless control-plane global
    # access is on; and one that admits this pod's traffic: the list does not
    # gate it, or the target shares this cluster's subnet (observed to admit
    # a pod from another cluster on it), or this cluster's Pod range is on
    # the list. A private endpoint that would refuse us is no better than a
    # public one that does, and worse than a public one that does not.
    same_network: bool | None = None
    routable = False
    pod_cidr = ""
    provisional = False
    if private_endpoint and network and (restricted or not public_endpoint):
        mine = own()
        if mine is not None:
            same_network = mine.network == network
            pod_cidr = mine.pod_cidr
        else:
            provisional = own_known
        if same_network:
            routable = global_access or _region_of(location) == mine.region
            admitted = (
                not private_enforced
                or (subnetwork and subnetwork == mine.subnetwork)
                or (networks is not None and pod_cidr and _within_any(pod_cidr, networks))
            )
            if routable and admitted:
                return EndpointDecision((INTERNAL_IP_FLAG,), KIND_INTERNAL_IP, private_endpoint,
                                        True, networks, "")
            if routable:
                # Not admitted, which can only mean the list is enforced on the
                # private endpoint and misses this cluster's Pod range.
                return EndpointDecision((), KIND_IP, default_address, True, networks,
                                        REMEDY_ADMIT_POD_RANGE.format(pod_cidr=pod_cidr or POD_RANGE_UNKNOWN))

    # Rule 4. Whatever gcloud writes unflagged. The remedy names what could
    # actually open the cluster: a private endpoint on another network or in
    # another region is not helped by an address on the list.
    if same_network and not routable:
        remedy = REMEDY_OTHER_REGION
    elif private_endpoint and same_network is False:
        # Reached only through rule 3's gate: the list is on or there is no
        # public endpoint, so an address on the list is not the answer.
        remedy = REMEDY_OTHER_NETWORK
    elif restricted:
        remedy = REMEDY_IP
    else:
        remedy = ""
    return EndpointDecision((), KIND_IP, default_address, same_network, networks, remedy,
                            provisional=provisional)


def endpoint_decision(
    project: str,
    cluster: str,
    location: str,
    *,
    env: dict[str, str] | None = None,
    run: Runner | None = None,
    retry_own: bool = False,
) -> EndpointDecision | None:
    """Decide which endpoint `get-credentials` should name for this cluster.

    None when nothing could be decided — an incomplete identity, a gcloud
    without `--dns-endpoint`, a describe that failed with nothing cached —
    which every caller treats as "run the command gcloud ran before this
    module existed". Reaching a perfectly ordinary public cluster must not
    become contingent on an extra API call succeeding.

    Otherwise an EndpointDecision, remembered per cluster for
    `_ENDPOINT_TTL_SECONDS` and then re-read, so enabling or disabling an
    endpoint on a live cluster takes effect in a process that never restarts.

    `env` is used for the gcloud subprocess, minus `KUBECONFIG`, which is
    dropped for the reason `_default_runner` explains. Pass `run` instead to
    execute gcloud somewhere else entirely, as the credential proxy does.

    `retry_own` is for a caller that asks once and writes a permanent record,
    the profile scaffold: it asks for the agent's own cluster past any backoff
    a failed describe left, and does not accept a cached provisional answer,
    so one transient failure does not blank the record of every cluster
    scaffolded in the minute after it.
    """
    if not (project and cluster and location):
        return None

    key = (project, cluster, location)
    cached = _endpoint_cache.get(key)
    if (cached is not None and time.monotonic() - cached[0] < _ENDPOINT_TTL_SECONDS
            and not (retry_own and cached[1].provisional)):
        return cached[1]

    runner = run or _default_runner(env, _DESCRIBE_TIMEOUT_SECONDS)

    if not gcloud_supports_dns_endpoint(runner):
        if _support_cache is False:
            # Settled, not undecided: the installed gcloud answered and lacks
            # the flag, and cannot grow it while we run, so this is the empty
            # answer for the life of the process. An empty decision names no
            # address, which is how callers that record the decision know
            # there is nothing to record, and the credential proxy does not
            # treat it as provisional.
            return _NO_FLAG_DECISION
        # The probe did not run or did not answer: nothing is settled, and
        # gcloud_supports_dns_endpoint asks again on the next call. The same
        # answer as a failed describe below -- the last thing gcloud said, or
        # None, which the credential proxy marks provisional.
        return cached[1] if cached is not None else None

    described = _describe(project, cluster, location, runner)
    if described is None:
        # Only a definite answer is worth remembering. "We could not find out"
        # cached as "no" outlives whatever caused it: the credential proxy is a
        # daemon, so one failed describe would pin a cluster to its IP endpoint
        # until the pod restarts, long after the describe would have succeeded.
        #
        # A stale entry survives a failed refresh, timestamp untouched, so the
        # next call retries rather than waiting out another window. Serving it
        # beats falling back to "no": it is gcloud's own last answer, where "no"
        # would be this failure mistaken for a configuration.
        return cached[1] if cached is not None else None

    # A provisional decision is cached like any other, mark included: the
    # window bounds what a persistently failing own describe costs the agent's
    # callers, and the credential proxy acts on the mark, not on this cache.
    decision = _decide(described, location, lambda: own_cluster(runner, retry=retry_own),
                       own_known=_own_identity() is not None)
    _endpoint_cache[key] = (time.monotonic(), decision)
    return decision


def dns_endpoint_args(
    project: str,
    cluster: str,
    location: str,
    *,
    env: dict[str, str] | None = None,
    run: Runner | None = None,
) -> list[str]:
    """Return the `get-credentials` flags to append for this cluster.

    The flag-only view of `endpoint_decision`: `["--dns-endpoint"]`,
    `["--internal-ip"]` or `[]`. Splice it into the argv rather than branching
    at each call site. Never raises; a cluster we cannot decide for falls back
    to the empty list, which is exactly the command every caller ran before
    this module existed.
    """
    decision = endpoint_decision(project, cluster, location, env=env, run=run)
    return list(decision.flags) if decision is not None else []


def reset_cache() -> None:
    """Forget every memoised answer. For tests."""
    global _support_cache, _own_cluster_cache, _own_failure_at
    _endpoint_cache.clear()
    _support_cache = None
    _own_cluster_cache = None
    _own_failure_at = None
