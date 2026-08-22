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
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Callable

# (exit_code, stdout) — narrow enough that the credential proxy can satisfy it by
# wrapping its own executor, which runs commands in the sidecar rather than here.
Runner = Callable[[list[str]], "tuple[int, str]"]

DNS_ENDPOINT_FLAG = "--dns-endpoint"

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
_endpoint_cache: dict[tuple[str, str, str], tuple[float, list[str]]] = {}
_support_cache: bool | None = None

_DESCRIBE_TIMEOUT_SECONDS = 30
_HELP_TIMEOUT_SECONDS = 30


def _log(message: str) -> None:
    print(f"gke_endpoint: {message}", file=sys.stderr, flush=True)


def _default_runner(env: dict[str, str] | None, timeout: int) -> Runner:
    """Run gcloud, deliberately without a KUBECONFIG.

    Both commands this module runs — `clusters describe` and `get-credentials
    --help` — talk to the GKE API and read no kubeconfig, so dropping the
    variable costs nothing. It is dropped rather than merely unused because in
    the agent container `gcloud` is the credential-proxy shim, which forwards
    `$KUBECONFIG` on *every* gcloud call (`credential_proxy_client.py`,
    `KUBECONFIG_AWARE`). `describe` is not `get-credentials`, so the proxy takes
    its read path and resolves that path through `_target_of`, which stats the
    file and rejects the request with HTTP 400 if it is not there.

    Every caller here passes the kubeconfig that the `get-credentials` being
    assembled is about to *create*, so it is reliably absent — forwarding it
    turned the describe into a guaranteed 400 and the detection into a constant
    "no flag". Callers may keep passing their own `env`; this strips the one key
    that must not travel.
    """
    base = env if env is not None else {**os.environ, "HOME": "/tmp"}
    scrubbed = {key: value for key, value in base.items() if key != "KUBECONFIG"}

    def run(argv: list[str]) -> tuple[int, str]:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=scrubbed,
        )
        return completed.returncode, completed.stdout
    return run


def gcloud_supports_dns_endpoint(run: Runner | None = None) -> bool:
    """Does the gcloud on PATH understand `--dns-endpoint`?

    The agent image installs an unpinned `google-cloud-cli` from apt, so this is
    always true there. It is asked because the same helpers run from
    `k8s-operator/scripts/common.sh` on an operator's workstation, where gcloud
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
    except (OSError, subprocess.SubprocessError) as error:
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
        "--format=json(controlPlaneEndpointsConfig)",
    ]
    try:
        exit_code, stdout = run(argv)
    except (OSError, subprocess.SubprocessError) as error:
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


def dns_endpoint_args(
    project: str,
    cluster: str,
    location: str,
    *,
    env: dict[str, str] | None = None,
    run: Runner | None = None,
) -> list[str]:
    """Return the `get-credentials` flags to append for this cluster.

    `[DNS_ENDPOINT_FLAG]` when the cluster publishes a DNS endpoint that accepts
    external traffic, `[]` otherwise. Splice it into the argv rather than
    branching at each call site.

    Never raises. A cluster we cannot describe — no permission, no network, an
    older gcloud — falls back to the empty list, which is exactly the command
    every caller ran before this module existed. Reaching a perfectly ordinary
    public cluster must not become contingent on an extra API call succeeding.

    The answer is remembered per cluster for `_ENDPOINT_TTL_SECONDS` and then
    re-read, so enabling or disabling the DNS endpoint on a live cluster takes
    effect in a process that never restarts.

    `env` is used for the gcloud subprocess, minus `KUBECONFIG`, which is
    dropped for the reason `_default_runner` explains. Pass `run` instead to
    execute gcloud somewhere else entirely, as the credential proxy does.
    """
    if not (project and cluster and location):
        return []

    key = (project, cluster, location)
    cached = _endpoint_cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < _ENDPOINT_TTL_SECONDS:
        return list(cached[1])

    runner = run or _default_runner(env, _DESCRIBE_TIMEOUT_SECONDS)

    if not gcloud_supports_dns_endpoint(runner):
        return []

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
        return list(cached[1]) if cached is not None else []

    dns = (
        described.get("controlPlaneEndpointsConfig", {})
        .get("dnsEndpointConfig", {})
    )
    # `allowExternalTraffic` absent is a no, not a maybe: clusters predating the
    # DNS endpoint omit the whole block, and the flag fails against them.
    decision = (
        [DNS_ENDPOINT_FLAG]
        if dns.get("endpoint") and dns.get("allowExternalTraffic") is True
        else []
    )
    _endpoint_cache[key] = (time.monotonic(), decision)
    return list(decision)


def reset_cache() -> None:
    """Forget both memoised answers. For tests."""
    global _support_cache
    _endpoint_cache.clear()
    _support_cache = None
