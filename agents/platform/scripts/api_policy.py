#!/usr/bin/env python3
"""Which Google Cloud REST reads the credential proxy relays for the sandbox.

The broker's `/v1/gcp/<host>/<path>?<query>` route forwards a `GET` from the
shell sandbox to a Google API on the broker's own credential, and a `POST` for
a read whose arguments travel in a JSON body. This module is
the whole of what decides whether it may: an allowlist of (host, method, path
shape) entries, each `POST` entry with the check its body must pass, in the style of `command_policy.GCLOUD_READ_COMMANDS`, reviewed
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
import credential_proxy_client

# The method of a read with no body, which is every route but the ones below.
API_READ_METHOD = "GET"
DEFAULT_UNIVERSE_DOMAIN = credential_proxy_client.DEFAULT_UNIVERSE_DOMAIN
# The method of a read whose arguments are a JSON body (Logging `entries:list`).
# A route with this method must carry a body check, and the broker forwards its
# own re-serialisation of the body that passed it (`gcp-api-relay.md`, "Reads
# with a body").
API_BODY_METHOD = "POST"

# Rule ids, in the order `evaluate` answers. Each names one line of the
# security argument, and the audit line and the 403 carry them verbatim.
RULE_METHOD = "gcp.api.method"
RULE_HOST_REFUSED = "gcp.api.host-refused"
RULE_HOST = "gcp.api.host"
RULE_PATH = "gcp.api.path"
RULE_BODY = "gcp.api.body"

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
PROJECT = r"(?:(?:[a-z][a-z0-9-]{0,15}:)?[a-z][a-z0-9-]{4,28}[a-z0-9]|[1-9][0-9]{0,18})"

# A Cloud Trace v1 trace id as the API returns it: a 128-bit value as 32
# lower-case hex characters. Constraining the segment is what keeps the
# `traces/<id>` route from admitting a child (`traces/<id>/spans`), a sibling
# spelt as an id, or an id that is not one.
TRACE_ID = r"[0-9a-f]{32}"

# A GKE location as the API takes it: `-` (every location, the list's wildcard), a
# region (`us-central1` or sovereign `u-region-north1`) or a zone (`us-central1-a`). A cluster name as GKE allows
# it: lower-case letters, digits and hyphens, starting with a letter, ending with a
# letter or digit, at most 40 characters. Constraining both segments is what keeps
# the cluster routes from admitting a child (`clusters/<c>/nodePools`), a custom
# method spelt into a segment, or a second segment through either position.
GKE_LOCATION = r"(?:-|(?:u-)?[a-z]+-[a-z]+[0-9]{1,2}(?:-[a-z])?)"
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


# Logging `entries:list`: the keys its body may carry, the two orders the API
# takes, and the API's own page-size ceiling.
LOGGING_ENTRIES_LIST_RULE = "gcp.api.logging.entries-list"
LOGGING_RESOURCE_NAMES = "resourceNames"
LOGGING_FILTER = "filter"
LOGGING_ORDER_BY = "orderBy"
LOGGING_PAGE_SIZE = "pageSize"
LOGGING_PAGE_TOKEN = "pageToken"
LOGGING_BODY_KEYS = frozenset(
    {LOGGING_RESOURCE_NAMES, LOGGING_FILTER, LOGGING_ORDER_BY, LOGGING_PAGE_SIZE, LOGGING_PAGE_TOKEN}
)
LOGGING_ORDERS = frozenset({"timestamp asc", "timestamp desc"})
LOGGING_MAX_PAGE_SIZE = 1000
_LOGGING_RESOURCE = re.compile(rf"projects/{PROJECT}")


@dataclass(frozen=True)
class ApiRoute:
    """One permitted read: a host, a method and the exact path shape."""

    host: str  # exact match, lower-cased
    method: str  # API_READ_METHOD, or API_BODY_METHOD with `body` set
    path: re.Pattern  # anchored at both ends, matched against the whole path
    rule_id: str  # what the audit line and the 403 name
    # What of a 2xx response body the caller is handed, or None to hand back the
    # upstream's body as it came. A projection returns None for a body it cannot
    # read, which the broker answers 502 rather than relaying unprojected.
    project: Callable[[bytes], bytes | None] | None = None
    # For an API_BODY_METHOD route: what is wrong with the request's parsed JSON
    # body, or None when the broker may forward it. Never echoes caller text.
    body: Callable[[object], str | None] | None = None


def check_logging_entries_list(body: object) -> str | None:
    """What is wrong with an `entries:list` body, or None when it may be forwarded.

    One project's log entries and nothing wider: `resourceNames` is exactly one
    `projects/<project>`, so no folder, organisation, billing account, log bucket
    or view, and the deprecated `projectIds`, a second way to name a project, is
    not a permitted key. The filter is Google's grammar and is not parsed, as
    `gcloud logging read`'s filter argument is not; it cannot widen the read
    past the resource the body names.
    """
    if not isinstance(body, dict):
        return "the body must be a JSON object"
    if not set(body) <= LOGGING_BODY_KEYS:
        return f"the body may carry only {', '.join(sorted(LOGGING_BODY_KEYS))}"
    names = body.get(LOGGING_RESOURCE_NAMES)
    if not (
        isinstance(names, list)
        and len(names) == 1
        and isinstance(names[0], str)
        and _LOGGING_RESOURCE.fullmatch(names[0])
    ):
        return f"{LOGGING_RESOURCE_NAMES} must be exactly one projects/<project>"
    if not isinstance(body.get(LOGGING_FILTER, ""), str):
        return f"{LOGGING_FILTER} must be a string"
    order = body.get(LOGGING_ORDER_BY)
    if LOGGING_ORDER_BY in body and not (isinstance(order, str) and order in LOGGING_ORDERS):
        return f"{LOGGING_ORDER_BY} must be one of: {', '.join(sorted(LOGGING_ORDERS))}"
    if LOGGING_PAGE_SIZE in body:
        size = body[LOGGING_PAGE_SIZE]
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= LOGGING_MAX_PAGE_SIZE:
            return f"{LOGGING_PAGE_SIZE} must be an integer from 1 to {LOGGING_MAX_PAGE_SIZE}"
    if not isinstance(body.get(LOGGING_PAGE_TOKEN, ""), str):
        return f"{LOGGING_PAGE_TOKEN} must be a string"
    return None


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

    `missingZones` is kept so the answer still says when a list is partial.
    stall_watch reads it as its "incomplete listing" signal, so it must stay. The
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


# The cluster read the shim's get-credentials check uses; the broker refuses it
# for an excluded cluster when the check's header is set
# (credential_proxy.GET_CREDENTIALS_CHECK_HEADER).
GKE_CLUSTER_GET_RULE = "gcp.api.container.clusters-get"

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
    # id; node pools, operations and every write stay refused. The get is also the
    # shim's check before it answers a get-credentials itself (#5447), and the list
    # is also stall_watch's cluster listing (#5598).
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
        GKE_CLUSTER_GET_RULE,
        project_gke_cluster,
    ),
    # One project's log entries, for the code-owned `gcloud logging read` callers
    # (fleet_stockout.py, the readiness report's audit_log.py): the same read
    # without a `gcloud` start in the broker. The one POST the relay forwards;
    # `entries:write`, `entries:tail`, `entries:copy`, sinks, exclusions and every
    # other Logging path stay refused, and the body check holds the read to one
    # `projects/<project>`.
    ApiRoute(
        "logging.googleapis.com",
        API_BODY_METHOD,
        re.compile(r"^v2/entries:list$"),
        LOGGING_ENTRIES_LIST_RULE,
        body=check_logging_entries_list,
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
        if route.method not in (API_READ_METHOD, API_BODY_METHOD):
            raise ValueError(
                f"{where}: method {route.method!r} is not relayed; only {API_READ_METHOD} "
                f"and {API_BODY_METHOD} are"
            )
        if (route.method == API_BODY_METHOD) != (route.body is not None):
            raise ValueError(
                f"{where}: a {API_BODY_METHOD} route must carry a body check, and a "
                f"{API_READ_METHOD} route must not; an unchecked body is a request the "
                f"policy never read"
            )
        if route.body is not None and not callable(route.body):
            raise TypeError(f"{where}: body must be callable or None")
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
_BODY_CHECKS = {route.rule_id: route.body for route in API_READ_ROUTES if route.body is not None}


def response_projection(rule_id: str) -> Callable[[bytes], bytes | None] | None:
    """The projection the allowed route `rule_id` applies to a 2xx body, if any."""
    return _PROJECTIONS.get(rule_id)


def carries_body(rule_id: str) -> bool:
    """Whether the allowed route `rule_id` is sent with a JSON body the broker must check."""
    return rule_id in _BODY_CHECKS


def evaluate_body(rule_id: str, body: object) -> Decision:
    """Allow or refuse the parsed JSON body of a request `evaluate` allowed.

    Refuses with ``gcp.api.body`` and the check's own words, which never repeat
    the caller's text. A rule with no body check refuses every body. Never raises.
    """
    check = _BODY_CHECKS.get(rule_id)
    problem = "this route takes no body" if check is None else check(body)
    if problem is None:
        return Decision(allowed=True, rule_id=rule_id, message="")
    return Decision(
        allowed=False,
        rule_id=RULE_BODY,
        message=f"The credential proxy does not relay this request body: {problem}.",
    )


def evaluate(
    method: str,
    host: str,
    path: str,
    query: str,
    *,
    universe_domain: str = DEFAULT_UNIVERSE_DOMAIN,
) -> Decision:
    """Allow or refuse one relayed request.

    Answers in this order, and the order is the security argument:

    1. A ``POST`` that is not, exactly, the host and path of a route with a body
       check (the route's host in ``universe_domain``), or any method other than
       ``GET`` and ``POST`` -> ``gcp.api.method``. A ``POST`` that is such a route
       is allowed here when it carries no query (``gcp.api.body`` otherwise), and
       its body is still to pass ``evaluate_body``.
    2. Host in ``REFUSED_HOSTS`` (in either ``googleapis.com`` or ``universe_domain``) ->
       ``gcp.api.host-refused``. Listed apart from an unknown host so the log
       distinguishes "asked for a token endpoint" from "asked for a service
       nobody has added".
    3. Host in no route for ``universe_domain`` -> ``gcp.api.host``. When
       ``universe_domain`` is not ``googleapis.com``, commercial ``*.googleapis.com``
       hosts are absent from this set and refused so custom-universe tokens
       never cross a universe boundary.
    4. Host known, path matching no route -> ``gcp.api.path``, naming the rule
       ids that host does have.
    5. Otherwise allowed, carrying the matching ``rule_id`` for the audit line.

    ``path`` is the caller's text after the host segment, with no leading
    slash, matched whole against the route regex and never normalised here:
    the handler has already refused anything not in normal form, and what this
    function sees is exactly what the broker would forward. ``query`` is not
    evaluated on a ``GET`` route -- the filter grammar is Google's to validate,
    and the handler strips the credential-substituting keys before forwarding
    -- and must be empty on a ``POST`` route, whose arguments are its body.

    Never raises.
    """
    host = host.lower()
    try:
        universe = credential_proxy_client.normalize_universe_domain(universe_domain)
    except ValueError:
        # No route has a host in a universe domain that is not valid: a POST is refused on its
        # method below, and a GET by the answer after it.
        universe = None
    if method == API_BODY_METHOD and universe is not None:
        for route in API_READ_ROUTES:
            route_host = credential_proxy_client.rewrite_google_api_host(route.host, universe)
            if route.method == method and route_host == host and route.path.fullmatch(path):
                if query:
                    return Decision(
                        allowed=False,
                        rule_id=RULE_BODY,
                        message=(
                            "This read takes its arguments in the JSON body; the credential "
                            "proxy relays it with no query string."
                        ),
                    )
                return Decision(allowed=True, rule_id=route.rule_id, message="")
    if method != API_READ_METHOD:
        return Decision(
            allowed=False,
            rule_id=RULE_METHOD,
            message=(
                f"The credential proxy relays {API_READ_METHOD}, and {API_BODY_METHOD} only "
                f"for the reads its table names with a body check; this {method} is a write "
                f"or carries a body the policy cannot inspect."
            ),
        )
    if universe is None:
        return Decision(
            allowed=False,
            rule_id=RULE_HOST,
            message="The configured universe domain is not valid; the credential proxy refuses every API relay.",
        )
    refused_hosts = (
        REFUSED_HOSTS
        if universe == DEFAULT_UNIVERSE_DOMAIN
        else REFUSED_HOSTS | frozenset(credential_proxy_client.rewrite_google_api_host(h, universe) for h in REFUSED_HOSTS)
    )
    if host in refused_hosts:
        return Decision(
            allowed=False,
            rule_id=RULE_HOST_REFUSED,
            message=(
                f"{host} issues credentials or turns a read into a write elsewhere; "
                f"the credential proxy never relays it."
            ),
        )
    routed_hosts = (
        _ROUTED_HOSTS
        if universe == DEFAULT_UNIVERSE_DOMAIN
        else frozenset(credential_proxy_client.rewrite_google_api_host(h, universe) for h in _ROUTED_HOSTS)
    )
    if host not in routed_hosts:
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
        route_host = credential_proxy_client.rewrite_google_api_host(route.host, universe)
        if route_host == host and route.method == method and route.path.fullmatch(path):
            return Decision(allowed=True, rule_id=route.rule_id, message="")
    permitted = ", ".join(
        f"{route.rule_id} ({route.method})"
        for route in API_READ_ROUTES
        if credential_proxy_client.rewrite_google_api_host(route.host, universe) == host
    )
    return Decision(
        allowed=False,
        rule_id=RULE_PATH,
        message=(
            f"{host} relays only the reads {permitted}; this {method} path is none of them. "
            f"Reads are added to api_policy.API_READ_ROUTES by pull request."
        ),
    )
