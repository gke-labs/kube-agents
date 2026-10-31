#!/usr/bin/env python3
"""What the credential proxy runs for the scope publisher.

The scope publisher is a next-mode Job that resolves which projects sit in an
install's folders, organizations, Shared VPC hosts and Metrics Scopes, and
writes them to a ConfigMap the A2A gateway reads. It reaches GCP through the
broker's `/v1/exec` under the `scope` role, and this module is the whole of
what decides which `gcloud` commands the broker runs for it.

Two checks, both before the command runs:

- **The argv** is exactly one of five shapes, token for token, with only the
  argument free. These are the commands `cluster_agent_reconcile.py` already
  runs for the same lookups, fixed `--format` included. No other flag, no
  repeated flag, no reordering. The flags that change the identity, the
  configuration, or the logging (`REFUSED_FLAGS`) are refused by name, so the
  refusal says which one.
- **The argument** is one `scope.json` declares (a folder, an organization, a
  Shared VPC host, a Metrics Scope), or one an earlier lookup returned to a
  `scope` caller: the member window. `clusters list --project` takes a project
  that an asset search, a Shared VPC lookup or a `projects describe` returned;
  `projects describe` takes a number that a Metrics Scope lookup returned.

The member window is broker memory: `MemberWindow`. It records what the
broker's own successful lookups printed, for `MEMBER_WINDOW_SECONDS` from the
call that printed it. It empties when `scope.json` changes and on a restart,
and it is capped at `MEMBER_WINDOW_CAP` entries per kind with no eviction. The
cap is a memory bound, not `maxProjects`: lookups return members the
reconcile's cap never counts (excluded projects, whole over-cap containers),
so a `maxProjects` cap could refuse a probe for an in-cap member. It is state
on one broker process, so the scope role must stay on the single stateful
broker, never on an exec tier of several replicas.

Standard library only. No refusal message repeats caller text.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple

SCOPE_FILE_ENV = "CREDENTIAL_PROXY_SCOPE_FILE"
# How long a member a lookup returned may be probed. The publisher runs every
# lookup before its first probe, and a run at maxProjects 5000 can take about
# 4.2 hours, so the window outlasts the longest run.
SECONDS_PER_HOUR = 60 * 60
MEMBER_WINDOW_SECONDS = 5 * SECONDS_PER_HOUR
# How many project IDs, and separately how many numbers, the window holds: the
# CRD's maximum for spec.scope.maxProjects, as a fixed memory bound.
MEMBER_WINDOW_CAP = 5000

RULE_SCOPE_ARGV = "caller.scope-argv"
RULE_SCOPE_ARGUMENT = "caller.scope-argument"
RULE_SCOPE_REQUEST = "caller.scope-request"

# Each changes whose identity, which configuration, or how much gcloud logs.
# The exact match below refuses them anyway; naming them makes the refusal and
# the audit line say which one, and pins them in the tests.
REFUSED_FLAGS = (
    "--impersonate-service-account",
    "--flags-file",
    "--access-token-file",
    "--configuration",
    "--log-http",
    "--verbosity",
    "--account",
    "--billing-project",
    "--quiet",
)
PROJECT_FLAG = "--project"

# The argument patterns. None of them can start with `-`, so an
# argument is never read as a flag.
_PROJECT = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")
_NUMBER = re.compile(r"\A[0-9]{1,20}\Z")
CONTAINER_KIND_FOLDERS = "folders"
# The scope.json keys this module reads.
DECLARATION_FOLDERS = CONTAINER_KIND_FOLDERS
DECLARATION_ORGANIZATIONS = "organizations"
DECLARATION_SHARED_VPC_HOSTS = "sharedVpcHosts"
DECLARATION_METRICS_SCOPES = "metricsScopes"
_CONTAINER = re.compile(r"\A(?P<kind>folders|organizations)/(?P<id>[0-9]{1,20})\Z")

SHAPE_CLUSTERS_LIST = "clusters-list"
SHAPE_ASSET_SEARCH = "asset-search"
SHAPE_SHARED_VPC = "shared-vpc"
SHAPE_METRICS_SCOPE = "metrics-scope"
SHAPE_PROJECT_NUMBER = "project-describe"

ASSET_TYPE_CLUSTER = "container.googleapis.com/Cluster"
METRICS_SCOPE_NAME_PREFIX = "locations/global/metricsScopes/"
SCOPE_FLAG_PREFIX = "--scope="


class _Slot(NamedTuple):
    """The one free token of a shape: `prefix` followed by the argument."""

    prefix: str


# The five shapes, copied token for token from cluster_agent_reconcile.py
# (`_list_project`, `_search_container`, `_resolve_selector` and
# `_project_id_of`; test_scope_policy holds the two in step). The publisher
# sends exactly these.
SHAPES: dict[str, tuple[str | _Slot, ...]] = {
    SHAPE_CLUSTERS_LIST: (
        "gcloud", "container", "clusters", "list", PROJECT_FLAG, _Slot(""),
        "--format=value(name,location)",
    ),
    SHAPE_ASSET_SEARCH: (
        "gcloud", "asset", "search-all-resources", _Slot(SCOPE_FLAG_PREFIX),
        f"--asset-types={ASSET_TYPE_CLUSTER}", "--format=json(name,location)",
    ),
    SHAPE_SHARED_VPC: (
        "gcloud", "compute", "shared-vpc", "list-associated-resources", _Slot(""),
        "--format=json(id,type)",
    ),
    SHAPE_METRICS_SCOPE: (
        "gcloud", "beta", "monitoring", "metrics-scopes", "describe", _Slot(METRICS_SCOPE_NAME_PREFIX),
        "--format=json(name,monitoredProjects)",
    ),
    SHAPE_PROJECT_NUMBER: (
        "gcloud", "projects", "describe", _Slot(""), "--format=value(projectId)",
    ),
}

ARGUMENT_PATTERNS: dict[str, re.Pattern[str]] = {
    SHAPE_CLUSTERS_LIST: _PROJECT,
    SHAPE_ASSET_SEARCH: _CONTAINER,
    SHAPE_SHARED_VPC: _PROJECT,
    SHAPE_METRICS_SCOPE: _PROJECT,
    SHAPE_PROJECT_NUMBER: _NUMBER,
}

# What a successful lookup's stdout names, for the member window.
_ASSET_NAME = re.compile(
    r"\A//container\.googleapis\.com/projects/(?P<project>[^/]+)/(?:locations|zones)/[^/]+/clusters/[^/]+\Z"
)
_MONITORED_PROJECT_NAME = re.compile(r"\Alocations/global/metricsScopes/[^/]+/projects/(?P<project>[^/]+)\Z")
XPN_RESOURCE_TYPE_PROJECT = "PROJECT"
# The output fields the lookups' --format projections keep.
FIELD_NAME = "name"
FIELD_ID = "id"
FIELD_TYPE = "type"
FIELD_MONITORED_PROJECTS = "monitoredProjects"
EMPTY_JSON = "null"

KIND_PROJECTS = "projects"
KIND_NUMBERS = "numbers"


class Refusal(NamedTuple):
    rule: str
    message: str


class Declaration(NamedTuple):
    """The parts of the rendered `scope.json` this module reads."""

    digest: str
    folders: frozenset[str]
    organizations: frozenset[str]
    shared_vpc_hosts: frozenset[str]
    metrics_scopes: frozenset[str]


def _string_set(document: dict[str, Any], key: str) -> frozenset[str]:
    values = document.get(key) or []
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"scope.json {key} is not a list of strings")
    return frozenset(values)


def load_declaration(path: str) -> Declaration:
    """Read `scope.json`; raises on a missing, unreadable or malformed file.

    Read on every scope call, with no cache: the file is small, and the
    directory mount updates it in place when the CR changes.
    """
    raw = Path(path).read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("scope.json is not an object")
    return Declaration(
        digest=hashlib.sha256(raw).hexdigest(),
        folders=_string_set(document, DECLARATION_FOLDERS),
        organizations=_string_set(document, DECLARATION_ORGANIZATIONS),
        shared_vpc_hosts=_string_set(document, DECLARATION_SHARED_VPC_HOSTS),
        metrics_scopes=_string_set(document, DECLARATION_METRICS_SCOPES),
    )


def _token_names(token: str, flag: str) -> bool:
    return token == flag or token.startswith(flag + "=")


def match_shape(argv: list[str]) -> tuple[str, str] | None:
    """(shape, argument) when `argv` is one of `SHAPES` exactly, else None."""
    for shape, tokens in SHAPES.items():
        if len(argv) != len(tokens):
            continue
        argument = None
        for token, expected in zip(argv, tokens):
            if isinstance(expected, _Slot):
                if not token.startswith(expected.prefix):
                    break
                argument = token[len(expected.prefix):]
            elif token != expected:
                break
        else:
            if argument is not None and ARGUMENT_PATTERNS[shape].match(argument):
                return shape, argument
    return None


def argv_refusal(argv: list[str]) -> tuple[Refusal | None, tuple[str, str] | None]:
    """The refusal for an argv that is not one of the five shapes, or the match."""
    for token in argv[1:]:
        for flag in REFUSED_FLAGS:
            if _token_names(token, flag):
                return Refusal(RULE_SCOPE_ARGV, f"The scope caller may not pass {flag}."), None
    matched = match_shape(argv)
    if matched is not None:
        return None, matched
    if any(_token_names(token, PROJECT_FLAG) for token in argv[1:]):
        return Refusal(
            RULE_SCOPE_ARGV,
            f"The scope caller may pass {PROJECT_FLAG} only in the exact `container clusters list` lookup.",
        ), None
    return Refusal(
        RULE_SCOPE_ARGV,
        "The scope caller may run only the five scope lookups, token for token "
        "(clusters list, asset search, Shared VPC, Metrics Scope, projects describe).",
    ), None


def argument_admitted(shape: str, argument: str, declaration: Declaration, window: "MemberWindow") -> bool:
    """Whether `scope.json` declares the argument, or the member window holds it."""
    if shape == SHAPE_ASSET_SEARCH:
        container = _CONTAINER.match(argument)
        declared = declaration.folders if container["kind"] == CONTAINER_KIND_FOLDERS else declaration.organizations
        return container["id"] in declared
    if shape == SHAPE_SHARED_VPC:
        return argument in declaration.shared_vpc_hosts
    if shape == SHAPE_METRICS_SCOPE:
        return argument in declaration.metrics_scopes
    if shape == SHAPE_CLUSTERS_LIST:
        return window.holds(KIND_PROJECTS, argument, declaration.digest)
    return window.holds(KIND_NUMBERS, argument, declaration.digest)


def members_named(shape: str, stdout: str) -> tuple[str, set[str]]:
    """(kind, values) a successful lookup's stdout names, for the member window.

    A row this cannot read is skipped rather than guessed at: recording less
    only refuses more. The publisher reads the same output and treats a row it
    cannot read as a failed lookup.
    """
    if shape == SHAPE_PROJECT_NUMBER:
        project = stdout.strip()
        return KIND_PROJECTS, {project} if _PROJECT.match(project) else set()
    try:
        parsed = json.loads(stdout or EMPTY_JSON)
    except ValueError:
        return KIND_PROJECTS, set()
    if shape == SHAPE_ASSET_SEARCH:
        rows = parsed if isinstance(parsed, list) else []
        names = (
            _ASSET_NAME.match(row[FIELD_NAME]) for row in rows
            if isinstance(row, dict) and isinstance(row.get(FIELD_NAME), str)
        )
        return KIND_PROJECTS, {m["project"] for m in names if m and _PROJECT.match(m["project"])}
    if shape == SHAPE_SHARED_VPC:
        rows = parsed if isinstance(parsed, list) else []
        return KIND_PROJECTS, {
            row[FIELD_ID] for row in rows
            if isinstance(row, dict) and row.get(FIELD_TYPE) == XPN_RESOURCE_TYPE_PROJECT
            and isinstance(row.get(FIELD_ID), str) and _PROJECT.match(row[FIELD_ID])
        }
    if shape == SHAPE_METRICS_SCOPE:
        rows = parsed.get(FIELD_MONITORED_PROJECTS) if isinstance(parsed, dict) else None
        names = (
            _MONITORED_PROJECT_NAME.match(row[FIELD_NAME]) for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and isinstance(row.get(FIELD_NAME), str)
        )
        # Numbers only: a Metrics Scope member opens `projects describe`,
        # never the probe, which takes the ID that call returns.
        return KIND_NUMBERS, {m["project"] for m in names if m and _NUMBER.match(m["project"])}
    return KIND_PROJECTS, set()


class MemberWindow:
    """The members the scope role's own lookups returned, for the probe.

    One dict per kind, value -> the monotonic time it was last returned. Tied
    to one `scope.json` digest: a different digest empties it. Capped per kind
    at `cap` with no eviction: once full, a new value is not recorded and
    nothing is dropped; a value already held has its time refreshed, which adds
    no entry.
    """

    def __init__(
        self,
        ttl_seconds: float = MEMBER_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        cap: int = MEMBER_WINDOW_CAP,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.cap = cap
        self.clock = clock
        self._lock = threading.Lock()
        self._digest: str | None = None
        self._members: dict[str, dict[str, float]] = {KIND_PROJECTS: {}, KIND_NUMBERS: {}}

    def _sync(self, digest: str) -> None:
        if digest != self._digest:
            self._digest = digest
            self._members = {KIND_PROJECTS: {}, KIND_NUMBERS: {}}

    def holds(self, kind: str, value: str, digest: str) -> bool:
        with self._lock:
            self._sync(digest)
            recorded = self._members[kind].get(value)
            return recorded is not None and self.clock() - recorded < self.ttl_seconds

    def record(self, kind: str, values: set[str], declaration: Declaration) -> None:
        with self._lock:
            self._sync(declaration.digest)
            now = self.clock()
            members = self._members[kind]
            for value in [v for v, recorded in members.items() if now - recorded >= self.ttl_seconds]:
                del members[value]
            for value in sorted(values):
                if value in members or len(members) < self.cap:
                    members[value] = now


class Admitted(NamedTuple):
    """A scope request that passed: its shape and the declaration it passed against."""

    shape: str
    declaration: Declaration


def check(argv: list[str], *, extras: bool, scope_file: str, window: MemberWindow) -> tuple[Refusal | None, Admitted | None]:
    """Decide one scope-role `/v1/exec` request before it runs.

    `extras` is whether the request carried stdin, a working directory or a
    kubeconfig field; the five lookups need none of them. A missing, unreadable
    or malformed `scope.json` refuses every request.
    """
    if extras:
        return Refusal(RULE_SCOPE_REQUEST, "The scope caller may send only argv: no stdin, cwd or kubeconfig."), None
    refusal, matched = argv_refusal(argv)
    if refusal is not None:
        return refusal, None
    shape, argument = matched
    try:
        declaration = load_declaration(scope_file)
    except (OSError, ValueError) as exc:
        return Refusal(
            RULE_SCOPE_ARGUMENT, f"The broker cannot read the scope declaration ({type(exc).__name__}); nothing is admitted."
        ), None
    if not argument_admitted(shape, argument, declaration, window):
        return Refusal(
            RULE_SCOPE_ARGUMENT,
            "The scope caller may name only what scope.json declares, or a member an earlier lookup returned "
            f"within the last {MEMBER_WINDOW_SECONDS // SECONDS_PER_HOUR} hours.",
        ), None
    return None, Admitted(shape, declaration)


def record(admitted: Admitted, stdout: str, *, scope_file: str, window: MemberWindow) -> None:
    """Record what a successful lookup returned, if `scope.json` did not change while it ran."""
    if admitted.shape == SHAPE_CLUSTERS_LIST:
        return
    try:
        current = load_declaration(scope_file)
    except (OSError, ValueError):
        return
    if current.digest != admitted.declaration.digest:
        return
    kind, values = members_named(admitted.shape, stdout)
    window.record(kind, values, current)
