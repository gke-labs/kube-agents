#!/usr/bin/env python3
"""Which Google Cloud REST reads the credential proxy relays for the sandbox.

The broker's `/v1/gcp/<host>/<path>?<query>` route forwards a `GET` from the
shell sandbox to a Google API on the broker's own credential. This module is
the whole of what decides whether it may: an allowlist of (host, method, path
shape) entries, in the style of `command_policy.GCLOUD_READ_COMMANDS`, reviewed
as a diff and widened only by pull request. `docs/designs/gcp-api-relay.md`
argues the shape; the security section there is written against the order
`evaluate` answers in below, so do not reorder its checks without reading it.

**The table is the enforcement.** The token the broker forwards carries the
`cloud-platform` scope because the GKE metadata server ignores a narrower
request (verified on a live install, recorded in the design), so nothing under
this module bounds what a mistaken entry would read. A new `ApiRoute` is held
to the bar a new `gcloud` read is held to: one line, one reason in a comment,
and tests that hold the door one word away.

Standard library only, like `command_policy`: this is imported by the process
holding the credential and by its tests, and neither may need a package to do
so. `Decision` is reused from `command_policy` so the handler's refusal body is
built the same way for both doors.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from command_policy import Decision

# The one method this version relays. The reads that are POST -- Logging
# `entries:list`, Monitoring `timeSeries:query` for MQL -- carry a body the
# policy would have to inspect, and a body cap plus a schema per entry is a
# second design (`gcp-api-relay.md`, "The decision").
API_READ_METHOD = "GET"

# Rule ids, in the order `evaluate` answers. Each names one line of the
# security argument, and the audit line and the 403 carry them verbatim.
RULE_METHOD = "gcp.api.method"
RULE_HOST_REFUSED = "gcp.api.host-refused"
RULE_HOST = "gcp.api.host"
RULE_PATH = "gcp.api.path"

# Hosts whose responses are credentials, or that turn a read into a write
# somewhere else. Checked before the table and never overridable by it: the
# validator below refuses a table that names one, so an entry cannot be added
# here by mistake and reached through an allowlist edit.
REFUSED_HOSTS = frozenset(
    {
        "iamcredentials.googleapis.com",
        "sts.googleapis.com",
        "oauth2.googleapis.com",
        "accounts.google.com",
        "iam.googleapis.com",
        "secretmanager.googleapis.com",
        "cloudkms.googleapis.com",
        "metadata.google.internal",
    }
)

# The project segment: Google's project-id grammar (6 to 30 characters,
# lower-case letters, digits and hyphens, starting with a letter and not ending
# in a hyphen) or a project number (a decimal int64, as Resource Manager types
# it), since every API here takes either and an install's `projectId` may be
# the number. Constraining the segment to these two shapes is what stops a path
# from smuggling a second segment -- or an upper-case or dotted spelling the
# API might normalise -- through the project position. The table does not
# constrain *which* project; the `gcloud` allowlist takes the same position and
# its comment says why. IAM bounds the project set, this table bounds the
# operation.
PROJECT = r"(?:[a-z][a-z0-9-]{4,28}[a-z0-9]|[1-9][0-9]{0,18})"

# A Cloud Trace v1 trace id as the API returns it: a 128-bit value as 32
# lower-case hex characters. Constraining the segment is what keeps the
# `traces/<id>` route from admitting a child (`traces/<id>/spans`), a sibling
# spelt as an id, or an id that is not one.
TRACE_ID = r"[0-9a-f]{32}"

# A GKE location as the API takes it: `-` (every location, the list's wildcard), a
# region (`us-central1`) or a zone (`us-central1-a`). A cluster name as GKE allows
# it: lower-case letters, digits and hyphens, starting with a letter, ending with a
# letter or digit, at most 40 characters. Constraining both segments is what keeps
# the cluster routes from admitting a child (`clusters/<c>/nodePools`), a custom
# method spelt into a segment, or a second segment through either position.
GKE_LOCATION = r"(?:-|[a-z]+-[a-z]+[0-9]{1,2}(?:-[a-z])?)"
GKE_CLUSTER = r"[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?"

# What a host may look like, in the table and in a request: a lower-case DNS
# name with at least one dot. No scheme, no port, no path, no user info, no
# percent-encoding. Exact-match at lookup. Public because the broker's handler
# holds a caller's host segment to the same shape before it reaches `evaluate`;
# one regex, so the two cannot drift.
HOST_SHAPE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+\Z")

# A route regex must be anchored at both ends: `timeSeries` admits nothing
# under `timeSeries/`, and a path cannot be prefixed. Checked textually on the
# pattern source, which is what a reviewer reads.
_ANCHOR_START = "^"
_ANCHOR_END = "$"


@dataclass(frozen=True)
class ApiRoute:
    """One permitted read: a host, a method and the exact path shape."""

    host: str  # exact match, lower-cased
    method: str  # API_READ_METHOD in this version
    path: re.Pattern  # anchored at both ends, matched against the whole path
    rule_id: str  # what the audit line and the 403 name
    # What of a 2xx response body the caller is handed, or None to hand back the
    # upstream's body as it came. A projection returns None for a body it cannot
    # read, which the broker answers 502 rather than relaying unprojected.
    project: Callable[[bytes], bytes | None] | None = None


# What of a GKE `Cluster` the container routes relay: whether it exists and what it
# is called, which is all the Cluster Agent reconcile reads (#3703). Everything else
# is left behind, `masterAuth` included: control-plane auth material stays out of the
# relay, the boundary the watcher route draws (watcher_policy.project_cluster).
GKE_CLUSTER_FIELDS = ("name", "location", "status", "id")


def _cluster_row(cluster) -> dict | None:
    if not isinstance(cluster, dict):
        return None
    return {k: cluster[k] for k in GKE_CLUSTER_FIELDS if k in cluster}


def project_gke_cluster(body: bytes) -> bytes | None:
    """The relayed form of a `clusters.get` answer, or None if it is not one."""
    try:
        row = _cluster_row(json.loads(body))
    except ValueError:
        return None
    return None if row is None else json.dumps(row, separators=(",", ":")).encode()


def project_gke_cluster_list(body: bytes) -> bytes | None:
    """The relayed form of a `clusters.list` answer, or None if it is not one.

    `missingZones` is kept so the answer still says when a list is partial. The
    reconcile does not need it: it reads a list only as proof that a cluster exists,
    never as proof of absence, so a partial list cannot delete anything.
    """
    try:
        answer = json.loads(body)
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None
    clusters = answer.get("clusters", [])
    if not isinstance(clusters, list):
        return None
    rows = [_cluster_row(c) for c in clusters]
    if any(row is None for row in rows):
        return None
    kept: dict = {"clusters": rows}
    missing = answer.get("missingZones")
    if isinstance(missing, list) and all(isinstance(z, str) for z in missing):
        kept["missingZones"] = missing
    return json.dumps(kept, separators=(",", ":")).encode()


API_READ_ROUTES: tuple[ApiRoute, ...] = (
    # Per-container usage history for the cost stream's collector
    # (`skills/fleet-audit/scripts/fleet_waste.py`, sections 3.1, 3.11,
    # 3.12 and 3.13 of governance/fleet_wide_cost_analysis_sop.md): a week of series,
    # one call per metric per cluster and paginated, plus the load-balancer
    # packet counters its idle-workload check reads per project.
    ApiRoute(
        "monitoring.googleapis.com",
        API_READ_METHOD,
        re.compile(rf"^v3/projects/{PROJECT}/timeSeries$"),
        "gcp.api.monitoring.timeseries-list",
    ),
    # Discovery read the model needs to name a metric it has not seen before.
    ApiRoute(
        "monitoring.googleapis.com",
        API_READ_METHOD,
        re.compile(rf"^v3/projects/{PROJECT}/metricDescriptors$"),
        "gcp.api.monitoring.metricdescriptors-list",
    ),
    # Managed Prometheus, for the PromQL the AI-workload skills already print
    # and the fleet health sweep (skills/fleet-audit/scripts/fleet_health.py).
    ApiRoute(
        "monitoring.googleapis.com",
        API_READ_METHOD,
        re.compile(
            rf"^v1/projects/{PROJECT}/location/global/prometheus/api/v1/"
            r"(query|query_range|series|labels)$"
        ),
        "gcp.api.monitoring.promql-read",
    ),
    # The observability skill's trace helpers (analyze_trace_latency.py,
    # fetch_traces.py): the traces in a time window, then one trace's spans.
    # Two anchored reads on one host, so the helpers can run in the sandbox
    # without a token of their own; the v2 write surface, `traces/<id>/...`
    # and every other Trace path stay refused.
    ApiRoute(
        "cloudtrace.googleapis.com",
        API_READ_METHOD,
        re.compile(rf"^v1/projects/{PROJECT}/traces$"),
        "gcp.api.cloudtrace.traces-list",
    ),
    ApiRoute(
        "cloudtrace.googleapis.com",
        API_READ_METHOD,
        re.compile(rf"^v1/projects/{PROJECT}/traces/{TRACE_ID}$"),
        "gcp.api.cloudtrace.traces-get",
    ),
    # The Cluster Agent reconcile's two reads (#3703, approved by @bnaylor 10-28 as
    # exactly these two read-only routes): every cluster in a project, and whether one
    # cluster still exists. Both answers are projected to name, location, status and
    # id; node pools, operations and every write stay refused.
    ApiRoute(
        "container.googleapis.com",
        API_READ_METHOD,
        re.compile(rf"^v1/projects/{PROJECT}/locations/{GKE_LOCATION}/clusters$"),
        "gcp.api.container.clusters-list",
        project_gke_cluster_list,
    ),
    ApiRoute(
        "container.googleapis.com",
        API_READ_METHOD,
        re.compile(rf"^v1/projects/{PROJECT}/locations/{GKE_LOCATION}/clusters/{GKE_CLUSTER}$"),
        "gcp.api.container.clusters-get",
        project_gke_cluster,
    ),
)


def _validate_routes(routes: tuple[ApiRoute, ...]) -> None:
    """Refuse a table that cannot enforce what it appears to say.

    Raises at import, like `credential_proxy._validate_route_roles`: a broker
    that will not start is a red test on the pull request that mis-shaped the
    table, rather than a widening that ships quietly. Each check fails toward
    admitting a request, which is why none of them is left to review alone.
    """
    seen: set[str] = set()
    for index, route in enumerate(routes):
        where = f"API_READ_ROUTES[{index}]"
        if not isinstance(route.host, str) or not HOST_SHAPE.match(route.host):
            raise ValueError(
                f"{where}: host {route.host!r} must be a lower-case DNS name with "
                f"no scheme, port or path"
            )
        if route.host in REFUSED_HOSTS:
            raise ValueError(f"{where}: {route.host} is in REFUSED_HOSTS and cannot be relayed")
        if route.method != API_READ_METHOD:
            raise ValueError(
                f"{where}: method {route.method!r} is not relayed; only {API_READ_METHOD} is"
            )
        if not isinstance(route.path, re.Pattern):
            raise TypeError(
                f"{where}: path must be a compiled regex, not {type(route.path).__name__}"
            )
        source = route.path.pattern
        if not (source.startswith(_ANCHOR_START) and source.endswith(_ANCHOR_END)):
            raise ValueError(
                f"{where}: path regex {source!r} must be anchored with ^ and $; an "
                f"unanchored route admits every path containing it"
            )
        if source.startswith(_ANCHOR_START + "/"):
            raise ValueError(
                f"{where}: path regex {source!r} must not start with /; paths are relative"
            )
        if not route.rule_id or not isinstance(route.rule_id, str):
            raise ValueError(f"{where}: rule_id must be a non-empty str")
        if route.rule_id in seen:
            raise ValueError(
                f"{where}: rule_id {route.rule_id!r} is already used; each route names its own"
            )
        seen.add(route.rule_id)
        if route.project is not None and not callable(route.project):
            raise TypeError(f"{where}: project must be callable or None")


_validate_routes(API_READ_ROUTES)

# Every host the table names, for the "known host, unknown path" answer.
_ROUTED_HOSTS = frozenset(route.host for route in API_READ_ROUTES)
_PROJECTIONS = {route.rule_id: route.project for route in API_READ_ROUTES}


def response_projection(rule_id: str) -> Callable[[bytes], bytes | None] | None:
    """The projection the allowed route `rule_id` applies to a 2xx body, if any."""
    return _PROJECTIONS.get(rule_id)


def evaluate(method: str, host: str, path: str, query: str) -> Decision:
    """Allow or refuse one relayed request.

    Answers in this order, and the order is the security argument:

    1. Method not ``API_READ_METHOD`` -> ``gcp.api.method``.
    2. Host in ``REFUSED_HOSTS`` -> ``gcp.api.host-refused``. Listed apart from
       an unknown host so the log distinguishes "asked for a token endpoint"
       from "asked for a service nobody has added".
    3. Host in no route -> ``gcp.api.host``.
    4. Host known, path matching no route -> ``gcp.api.path``, naming the rule
       ids that host does have.
    5. Otherwise allowed, carrying the matching ``rule_id`` for the audit line.

    ``path`` is the caller's text after the host segment, with no leading
    slash, matched whole against the route regex and never normalised here:
    the handler has already refused anything not in normal form, and what this
    function sees is exactly what the broker would forward. ``query`` is
    accepted for the signature a later route may need and is not evaluated in
    this version -- the filter grammar is Google's to validate, and the handler
    strips the credential-substituting keys before forwarding.

    Never raises.
    """
    del query  # not evaluated in this version; see the docstring
    host = host.lower()
    if method != API_READ_METHOD:
        return Decision(
            allowed=False,
            rule_id=RULE_METHOD,
            message=(
                f"The credential proxy relays {API_READ_METHOD} only; {method} is "
                f"a write or carries a body the policy cannot inspect."
            ),
        )
    if host in REFUSED_HOSTS:
        return Decision(
            allowed=False,
            rule_id=RULE_HOST_REFUSED,
            message=(
                f"{host} issues credentials or turns a read into a write elsewhere; "
                f"the credential proxy never relays it."
            ),
        )
    if host not in _ROUTED_HOSTS:
        return Decision(
            allowed=False,
            rule_id=RULE_HOST,
            message=(
                f"{host} is not a host the credential proxy relays. Reads are added "
                f"to api_policy.API_READ_ROUTES by pull request."
            ),
        )
    for route in API_READ_ROUTES:
        # `fullmatch` on top of the textual anchors: `$` also matches before a
        # trailing newline, so `match` would admit `timeSeries\n`. The
        # validator holds the anchors for the reader; this holds the door.
        if route.host == host and route.method == method and route.path.fullmatch(path):
            return Decision(allowed=True, rule_id=route.rule_id, message="")
    permitted = ", ".join(route.rule_id for route in API_READ_ROUTES if route.host == host)
    return Decision(
        allowed=False,
        rule_id=RULE_PATH,
        message=(
            f"{host} relays only the reads {permitted}; this path is none of them. "
            f"Reads are added to api_policy.API_READ_ROUTES by pull request."
        ),
    )
