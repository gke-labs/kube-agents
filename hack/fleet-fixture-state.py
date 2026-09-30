#!/usr/bin/env python3
# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Assert that the seeded fleet's planted fixtures are in their DESIGNED state.

hack/fleet-kubeconfigs.sh confirms that every object a fixture role names is
PRESENT before it publishes the role's kubeconfig. Presence is not state: on
2026-09-07 a GKE auto-upgrade rebuilt seeded-a's single node, the system pods
took all of its CPU, `payments-api` and `checkout-gateway` sat Pending on all
30 pool projects, every presence probe kept passing, and the gate went red on
every pull request for a day (#1278). This script is the other half (#1544): for
each role the runner published it reads the `state` assertions the catalog
(bench/tf/fleet/fixtures.json) declares -- the crashloop has recorded an
OOMKilled termination, the healthy workload is Ready, the idle pool's node is
Ready and tainted, slot b's control plane is still one minor behind its
channel -- and reports the roles whose fixture is there but not in the shape
the cases depend on.

It runs in two places. `scripts/verify_ci_pool_project.py` runs it after the
presence pass and fails a project whose fixtures have drifted. The CI health
bot's hourly scan (`scripts/eval_dashboard/fixture_state.py`, #1550) runs it
against every pool project with `--report`, which writes each role's verdict
as JSON beside the summary so the scan need not parse the warnings. Detection
is this script's whole job -- nothing here, and nothing in the presubmit, acts
on a drift.
`--wait` is for a fixture that has just been rescheduled -- the crashloop needs
its first restart before OOMKilled evidence exists -- and keeps re-reading a
role that is positively out of shape until it converges or the deadline
passes. A role whose reads fail does not hold the wait: nothing about it can
converge, so it is re-read only while some other role is worth waiting for. A
role still drifted at the deadline gets a `<role>.drift` file beside its
kubeconfig, one failed assertion per line with what was observed, which is the
output contract a consumer reads.

What it never does: mutate anything (every read is `kubectl get -o json` or
`gcloud container clusters describe`), address a cluster it discovered itself
(the runner recorded each slot's cluster and location in `.fleet-context`), or
kill the job. Weather -- an unreachable API server, a refused describe -- leaves
the role "not checked" with a warning and no drift file; only positive
evidence of a fixture out of shape counts as drift. A malformed catalog is a
repository bug and exits 1.

Usage:
    hack/fleet-fixture-state.py [--dir DIR] [--catalog PATH] [--project ID]
                                [--wait SECONDS] [--interval SECONDS]
                                [--report PATH]

Inputs (flags win over the environment):
    BENCH_FLEET_KUBECONFIG_DIR         the directory hack/fleet-kubeconfigs.sh wrote
    FLEET_CATALOG                      path to fixtures.json
    FLEET_PROJECT_ID / PROJECT_ID      named in messages; `.fleet-context` wins
    FLEET_FIXTURE_STATE_WAIT_SECONDS   how long to poll for convergence (default 0)

Output: everything goes to stderr, ending in one summary line:

    Seeded-fleet fixture state: N role(s) in their designed state, D drifted,
    U not checked (project P)

With `--report PATH` the same verdicts are written as JSON, one entry per
catalog role: `converged`, `drifted` or `unchecked` with the lines above as
`detail`, `unpublished` for a role the runner wrote no kubeconfig for, and
`no_state` for one that declares no assertions.

Exit 0 whenever the fleet was looked at, whatever it found; 1 on a repository
bug (unreadable catalog, malformed `state` entry, missing directory).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Files the runner (hack/fleet-kubeconfigs.sh) writes and this script reads or
# writes beside them. `.drift` is this script's own output, one file per
# drifted role; the other three names are the runner's.
KUBECONFIG_SUFFIX = ".kubeconfig"
DRIFT_SUFFIX = ".drift"
CONTEXT_FILE = ".fleet-context"
MARKER_FILE = ".kube-agents-fleet-kubeconfigs"

# The subject that means "the GKE cluster carrying this role itself", read
# with `gcloud container clusters describe` rather than kubectl. Slots b and c
# carry control-plane defects that no in-cluster object shows.
CLUSTER_SUBJECT = "cluster"

# Keys in `.fleet-context`, as the runner writes them: `project=<id>` and, per
# discovered slot, `cluster.<slot>=<name>` and `location.<slot>=<location>`.
CONTEXT_PROJECT_KEY = "project"
CONTEXT_CLUSTER_PREFIX = "cluster."
CONTEXT_LOCATION_PREFIX = "location."

# The same shapes hack/fleet-kubeconfigs.sh accepts for a probe, anchored so
# nothing in the catalog can smuggle a flag or a shell metacharacter into a
# command line. A selector subject may leave the selector empty (`kind?`),
# which reads every object of that kind in the role's namespace -- the form an
# `absent` assertion over a whole namespace needs.
NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
SUBJECT_RE = re.compile(
    r"[a-z][a-z0-9.]*(/[a-z0-9][a-z0-9.-]*|\?[A-Za-z0-9][A-Za-z0-9._/=,-]*|\?)"
)
# One path step: a key, optionally followed by `[*]`, `[N]` or a
# `[?(@.key=='value')]` filter. Keys allow the hyphens GKE puts in exclusion
# names (`maintenanceExclusions.hold-the-minor-lag`).
PATH_STEP_RE = re.compile(
    r"(?P<key>[A-Za-z0-9_-]+)"
    r"(?:\[(?:(?P<all>\*)|(?P<index>\d+)|\?\(@\.(?P<fkey>[A-Za-z0-9_-]+)=='(?P<fval>[^']*)'\))\])?"
)

# Comparison operators, applied to the LIST of values a path matched. `eq`,
# `ge`, `after_now` and `minor_behind_channel_default` want exactly one value;
# `any_*` and `none_eq` quantify over however many the path found; `absent`
# wants none (or, with no path, no objects at all).
OP_EQ = "eq"
OP_GE = "ge"
OP_ANY_EQ = "any_eq"
OP_ANY_GE = "any_ge"
OP_NONE_EQ = "none_eq"
OP_ABSENT = "absent"
OP_AFTER_NOW = "after_now"
OP_MINOR_BEHIND = "minor_behind_channel_default"
OPS_WITH_VALUE = frozenset({OP_EQ, OP_GE, OP_ANY_EQ, OP_ANY_GE, OP_NONE_EQ, OP_MINOR_BEHIND})
OPS_WITHOUT_VALUE = frozenset({OP_ABSENT, OP_AFTER_NOW})
OPS = OPS_WITH_VALUE | OPS_WITHOUT_VALUE
# Only the cluster subject can be compared against its channel's default.
CLUSTER_ONLY_OPS = frozenset({OP_MINOR_BEHIND})

# GKE version strings: `1.33.4-gke.1134000`. Only the minor matters here.
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.")
# Fields of a `clusters describe` document the minor-behind operator reads
# beside the path it was given.
RELEASE_CHANNEL_PATH = "releaseChannel.channel"
SERVER_CONFIG_CHANNELS = "channels"
SERVER_CONFIG_CHANNEL = "channel"
SERVER_CONFIG_DEFAULT = "defaultVersion"

# Read ceilings. A fixture read is one API call; anything slower is weather,
# and the poll loop asks again rather than hanging on it.
KUBECTL_TIMEOUT_SECONDS = 60
GCLOUD_TIMEOUT_SECONDS = 120
# kubectl's own request ceiling, below the subprocess one so the message on a
# slow API server is kubectl's rather than a kill.
KUBECTL_REQUEST_TIMEOUT = "30s"
# Polling defaults. Zero wait is one pass; a caller that has just re-applied
# the stack passes the wait it can afford.
DEFAULT_WAIT_SECONDS = 0.0
DEFAULT_INTERVAL_SECONDS = 15.0

# kubectl exits 1 for "not found" and for a dead API server alike, so the
# wording is what separates a fixture that is GONE (positive evidence: drift)
# from one this run could not look at (weather: not checked).
NOT_FOUND_RE = re.compile(r"\bNotFound\b|\bnot found\b", re.IGNORECASE)

# The one line other programs parse. scripts/verify_ci_pool_project.py reads
# the three counts off it and test_verify_ci_pool_project.py renders this
# format string to prove its regex still matches.
SUMMARY_FORMAT = (
    "Seeded-fleet fixture state: {converged} role(s) in their designed state, "
    "{drifted} drifted, {unchecked} not checked (project {project})"
)

# `--report` verdicts, one per catalog role. The first three are the summary
# line's three counts; the last two are roles the summary does not count.
REPORT_SCHEMA_VERSION = 1
VERDICT_CONVERGED = "converged"
VERDICT_DRIFTED = "drifted"
VERDICT_UNCHECKED = "unchecked"
VERDICT_UNPUBLISHED = "unpublished"
VERDICT_NO_STATE = "no_state"

EXIT_OK = 0
EXIT_REPOSITORY_BUG = 1


class CatalogError(ValueError):
    """A `state` entry this script cannot evaluate: a repository bug."""


class Unreadable(Exception):
    """The subject could not be read this pass; nothing is known about it."""


def _warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


# --- Catalog ------------------------------------------------------------------


def parse_path(path: str) -> list[tuple]:
    """`status.conditions[?(@.type=='Ready')].status` -> a list of steps.

    Each step is ("key", name) followed optionally by ("all",), ("index", n)
    or ("filter", key, value). Raises CatalogError on anything else, so a typo
    in the catalog fails the run rather than matching nothing forever.
    """
    steps: list[tuple] = []
    pos = 0
    while pos < len(path):
        m = PATH_STEP_RE.match(path, pos)
        if not m:
            raise CatalogError(f"path {path!r}: cannot parse at offset {pos}")
        steps.append(("key", m.group("key")))
        if m.group("all"):
            steps.append(("all",))
        elif m.group("index") is not None:
            steps.append(("index", int(m.group("index"))))
        elif m.group("fkey"):
            steps.append(("filter", m.group("fkey"), m.group("fval")))
        pos = m.end()
        if pos < len(path):
            if path[pos] != ".":
                raise CatalogError(f"path {path!r}: expected '.' at offset {pos}")
            pos += 1
            if pos == len(path):
                raise CatalogError(f"path {path!r}: trailing '.'")
    if not steps:
        raise CatalogError("empty path")
    return steps


def load_assertions(catalog_path: Path) -> dict[str, dict]:
    """The catalog's roles, each with its `state` list validated.

    Returns {role: {"cluster_slot", "namespace", "state": [entry, ...]}} where
    every entry has been checked for shape: a legal subject, an operator this
    script implements, a value exactly when the operator takes one, and a path
    that parses (optional only for `absent`, which may count objects instead).
    """
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"could not read {catalog_path}: {exc}") from exc
    roles = catalog.get("roles") or {}
    if not isinstance(roles, dict) or not roles:
        raise CatalogError(f"{catalog_path} declares no fixture roles")
    out: dict[str, dict] = {}
    for role, spec in sorted(roles.items()):
        if not NAME_RE.fullmatch(str(role)):
            raise CatalogError(f"role {role!r} is not a lowercase-hyphen name")
        slot = spec.get("cluster_slot")
        if not isinstance(slot, str) or not NAME_RE.fullmatch(slot):
            raise CatalogError(f"role {role!r} names a malformed cluster slot {slot!r}")
        namespace = spec.get("namespace")
        if namespace is not None and not NAME_RE.fullmatch(str(namespace)):
            raise CatalogError(f"role {role!r} names a malformed namespace {namespace!r}")
        state = spec.get("state")
        if not isinstance(state, list):
            raise CatalogError(f"role {role!r} declares no `state` list")
        entries = []
        for i, entry in enumerate(state):
            where = f"role {role!r} state[{i}]"
            if not isinstance(entry, dict):
                raise CatalogError(f"{where} is not a mapping")
            subject = entry.get("subject")
            if subject != CLUSTER_SUBJECT and (
                not isinstance(subject, str) or not SUBJECT_RE.fullmatch(subject)
            ):
                raise CatalogError(f"{where} declares a malformed subject {subject!r}")
            op = entry.get("op")
            if op not in OPS:
                raise CatalogError(f"{where} names unknown op {op!r} (known: {sorted(OPS)})")
            if op in CLUSTER_ONLY_OPS and subject != CLUSTER_SUBJECT:
                raise CatalogError(f"{where}: op {op!r} applies to the cluster subject only")
            if (op in OPS_WITH_VALUE) != ("value" in entry):
                raise CatalogError(
                    f"{where}: op {op!r} {'takes' if op in OPS_WITH_VALUE else 'takes no'} value"
                )
            path = entry.get("path")
            if path is None:
                if op != OP_ABSENT:
                    raise CatalogError(f"{where}: op {op!r} needs a path")
                steps = None
            elif isinstance(path, str):
                steps = parse_path(path)
            else:
                raise CatalogError(f"{where}: path must be a string or null")
            entries.append(
                {
                    "subject": subject,
                    "path": path,
                    "steps": steps,
                    "op": op,
                    "value": entry.get("value"),
                    "why": str(entry.get("why") or ""),
                }
            )
        out[role] = {"cluster_slot": slot, "namespace": namespace, "state": entries}
    return out


def read_context(directory: Path) -> dict[str, str]:
    """`.fleet-context` as key -> value. Empty when the runner wrote none."""
    try:
        text = (directory / CONTEXT_FILE).read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


# --- Reading subjects ----------------------------------------------------------


def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:g}s: {' '.join(cmd)}"
    return proc.returncode, proc.stdout, proc.stderr


def read_kubectl(kubeconfig: Path, namespace: str | None, subject: str) -> list[dict]:
    """The objects a `<kind>/<name>` or `<kind>?<selector>` subject names.

    A named object that is not there returns an empty list -- that is an
    observation ("gone"), which an assertion may fail on. Any other failure is
    Unreadable: nothing was learned this pass.
    """
    cmd = ["kubectl", f"--kubeconfig={kubeconfig}", f"--request-timeout={KUBECTL_REQUEST_TIMEOUT}", "get"]
    # Selector first, as hack/fleet-kubeconfigs.sh's _fleet_probe_present
    # does: a label key may itself contain a slash
    # (`node?cloud.google.com/gke-nodepool=idle-batch-pool`), so testing for
    # `/` first would split that subject as kind/name and ask kubectl for a
    # resource type that does not exist.
    named = "?" not in subject
    if named:
        kind, name = subject.split("/", 1)
        cmd += [kind, name]
    else:
        kind, selector = subject.split("?", 1)
        cmd += [kind]
        if selector:
            cmd += ["-l", selector]
    if namespace:
        cmd += ["-n", namespace]
    cmd += ["-o", "json"]
    rc, out, err = _run(cmd, KUBECTL_TIMEOUT_SECONDS)
    if rc != 0:
        if named and NOT_FOUND_RE.search(err):
            return []
        raise Unreadable(f"kubectl get {kind} failed ({rc}): {err.strip().splitlines()[-1] if err.strip() else 'no output'}")
    try:
        doc = json.loads(out)
    except ValueError as exc:
        raise Unreadable(f"kubectl get {kind} printed something other than JSON: {exc}") from exc
    if isinstance(doc, dict) and isinstance(doc.get("items"), list):
        return [item for item in doc["items"] if isinstance(item, dict)]
    if isinstance(doc, dict):
        return [doc]
    raise Unreadable(f"kubectl get {kind} printed an unexpected document")


def read_cluster(project: str, name: str, location: str) -> dict:
    cmd = [
        "gcloud", "container", "clusters", "describe", name,
        "--location", location, "--project", project, "--format", "json",
    ]
    rc, out, err = _run(cmd, GCLOUD_TIMEOUT_SECONDS)
    if rc != 0:
        raise Unreadable(f"clusters describe {name} failed ({rc}): {err.strip().splitlines()[-1] if err.strip() else 'no output'}")
    try:
        doc = json.loads(out)
    except ValueError as exc:
        raise Unreadable(f"clusters describe {name} printed something other than JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise Unreadable(f"clusters describe {name} printed an unexpected document")
    return doc


def read_channel_default(project: str, location: str, channel: str) -> str | None:
    """The default version of `channel` at `location`, or None if not listed."""
    cmd = [
        "gcloud", "container", "get-server-config",
        "--location", location, "--project", project, "--format", "json",
    ]
    rc, out, err = _run(cmd, GCLOUD_TIMEOUT_SECONDS)
    if rc != 0:
        raise Unreadable(f"get-server-config failed ({rc}): {err.strip().splitlines()[-1] if err.strip() else 'no output'}")
    try:
        doc = json.loads(out)
    except ValueError as exc:
        raise Unreadable(f"get-server-config printed something other than JSON: {exc}") from exc
    for entry in (doc.get(SERVER_CONFIG_CHANNELS) or []) if isinstance(doc, dict) else []:
        if isinstance(entry, dict) and entry.get(SERVER_CONFIG_CHANNEL) == channel:
            default = entry.get(SERVER_CONFIG_DEFAULT)
            return str(default) if default else None
    return None


# --- Evaluating assertions -----------------------------------------------------


def walk(steps: list[tuple], roots: list) -> list:
    """Every value the path reaches from the given roots, flattened."""
    current = list(roots)
    for step in steps:
        nxt = []
        for value in current:
            if step[0] == "key":
                if isinstance(value, dict) and step[1] in value:
                    nxt.append(value[step[1]])
            elif step[0] == "all":
                if isinstance(value, list):
                    nxt.extend(value)
            elif step[0] == "index":
                if isinstance(value, list) and step[1] < len(value):
                    nxt.append(value[step[1]])
            elif step[0] == "filter" and isinstance(value, list):
                nxt.extend(
                    item for item in value
                    if isinstance(item, dict) and _same(item.get(step[1]), step[2])
                )
        current = nxt
    return current


def _same(observed, expected) -> bool:
    """Equality that lets the catalog write `2` for a JSON 2 and `"True"` for
    a string status without caring which type the API chose."""
    if isinstance(observed, bool) or isinstance(expected, bool):
        return str(observed).lower() == str(expected).lower()
    if isinstance(observed, (int, float)) and isinstance(expected, (int, float)):
        return float(observed) == float(expected)
    return str(observed) == str(expected)


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _minor(version) -> int | None:
    m = VERSION_RE.match(str(version or ""))
    return int(m.group(2)) if m else None


def _describe(values: list) -> str:
    if not values:
        return "observed nothing"
    shown = json.dumps(values if len(values) > 1 else values[0], default=str)
    return f"observed {shown}"


def evaluate(entry: dict, objects: list, *, channel_default=None) -> str | None:
    """None when the assertion holds; otherwise one line saying what was seen.

    `channel_default` is a callable returning the channel default version for
    the cluster document, used by the minor-behind operator only; it is
    resolved lazily so a role that never needs server-config never pays for it.
    """
    op, expected = entry["op"], entry["value"]
    label = " ".join(part for part in (entry["subject"], entry["path"], op) if part)
    if op in OPS_WITH_VALUE:
        label += f" {json.dumps(expected)}"
    values = walk(entry["steps"], objects) if entry["steps"] else objects

    if op == OP_ABSENT:
        ok = not values
    elif op == OP_EQ:
        ok = len(values) == 1 and _same(values[0], expected)
    elif op == OP_GE:
        n, want = (_number(values[0]) if len(values) == 1 else None), _number(expected)
        ok = n is not None and want is not None and n >= want
    elif op == OP_ANY_EQ:
        ok = any(_same(v, expected) for v in values)
    elif op == OP_ANY_GE:
        want = _number(expected)
        ok = want is not None and any(
            (_number(v) is not None and _number(v) >= want) for v in values
        )
    elif op == OP_NONE_EQ:
        ok = not any(_same(v, expected) for v in values)
    elif op == OP_AFTER_NOW:
        ok = False
        if len(values) == 1:
            try:
                when = datetime.fromisoformat(str(values[0]).replace("Z", "+00:00"))
                ok = when > datetime.now(timezone.utc)
            except ValueError:
                ok = False
    elif op == OP_MINOR_BEHIND:
        ok = False
        if len(values) == 1:
            current = _minor(values[0])
            channel = walk(parse_path(RELEASE_CHANNEL_PATH), objects)
            default = channel_default(str(channel[0])) if len(channel) == 1 and channel_default else None
            want = _number(expected)
            if current is None or default is None:
                return (
                    f"{label}: {_describe(values)}; channel "
                    f"{channel[0] if channel else 'unset'} default "
                    f"{default or 'unknown'}"
                )
            ok = want is not None and current == _minor(default) - int(want)
            if not ok:
                return f"{label}: {_describe(values)} against channel default {default}"
    else:  # pragma: no cover - load_assertions rejects unknown ops
        raise CatalogError(f"unknown op {op!r}")
    return None if ok else f"{label}: {_describe(values)}"


# --- The pass ------------------------------------------------------------------


class Reader:
    """Reads for one pass, with the server-config lookups memoised.

    A pass over eight roles must not pay one get-server-config per assertion;
    a location's channel defaults do not change between two reads seconds
    apart.
    """

    def __init__(self, directory: Path, project: str, context: dict[str, str]):
        self.directory = directory
        self.project = project
        self.context = context
        self._defaults: dict[tuple[str, str], str | None] = {}

    def objects_for(self, role: str, spec: dict, subject: str) -> list:
        kubeconfig = self.directory / f"{role}{KUBECONFIG_SUFFIX}"
        if subject == CLUSTER_SUBJECT:
            slot = spec["cluster_slot"]
            name = self.context.get(f"{CONTEXT_CLUSTER_PREFIX}{slot}")
            location = self.context.get(f"{CONTEXT_LOCATION_PREFIX}{slot}")
            if not name or not location:
                raise Unreadable(
                    f"{CONTEXT_FILE} records no cluster for slot {slot!r}; the runner "
                    "that wrote this directory predates the cluster lines"
                )
            return [read_cluster(self.project, name, location)]
        return read_kubectl(kubeconfig, spec["namespace"], subject)

    def channel_default_for(self, spec: dict):
        slot = spec["cluster_slot"]
        location = self.context.get(f"{CONTEXT_LOCATION_PREFIX}{slot}")

        def lookup(channel: str) -> str | None:
            if not location:
                raise Unreadable(f"{CONTEXT_FILE} records no location for slot {slot!r}")
            key = (location, channel)
            if key not in self._defaults:
                self._defaults[key] = read_channel_default(self.project, location, channel)
            return self._defaults[key]

        return lookup


def check_role(reader: Reader, role: str, spec: dict) -> tuple[list[str], list[str]]:
    """(drift lines, unreadable lines) for one role in one pass.

    Objects are read once per distinct subject, so two assertions on the same
    pod list cost one kubectl. An assertion whose subject could not be read
    lands in the second list; the caller decides what that means once it
    knows whether anything else in the role was positively out of shape.
    """
    drift: list[str] = []
    unreadable: list[str] = []
    cache: dict[str, list] = {}
    for entry in spec["state"]:
        subject = entry["subject"]
        if subject not in cache:
            try:
                cache[subject] = reader.objects_for(role, spec, subject)
            except Unreadable as exc:
                cache[subject] = exc
        objects = cache[subject]
        if isinstance(objects, Unreadable):
            unreadable.append(f"{subject}: {objects}")
            continue
        try:
            problem = evaluate(entry, objects, channel_default=reader.channel_default_for(spec))
        except Unreadable as exc:
            unreadable.append(f"{subject}: {exc}")
            continue
        if problem:
            drift.append(problem + (f" (why: {entry['why']})" if entry["why"] else ""))
    return drift, unreadable


def run(directory: Path, catalog: Path, project_override: str | None, wait: float, interval: float, report: Path | None = None) -> int:
    if not directory.is_dir() or not (directory / MARKER_FILE).exists():
        print(
            f"ERROR: {directory} is not a directory hack/fleet-kubeconfigs.sh wrote; "
            "run it first (or point --dir at its output)",
            file=sys.stderr,
        )
        return EXIT_REPOSITORY_BUG
    try:
        roles = load_assertions(catalog)
    except CatalogError as exc:
        print(f"ERROR: fleet fixture catalog {catalog}: {exc}", file=sys.stderr)
        return EXIT_REPOSITORY_BUG

    context = read_context(directory)
    project = context.get(CONTEXT_PROJECT_KEY) or project_override or "unknown"

    # Only roles the runner PUBLISHED are asserted: a role with no kubeconfig
    # was already reported as unresolved or unplanted, and repeating that here
    # as "drift" would turn one finding into two. A role that declares no
    # state asserts nothing and is not counted either way.
    pending = {
        role: spec
        for role, spec in roles.items()
        if spec["state"] and (directory / f"{role}{KUBECONFIG_SUFFIX}").is_file()
    }
    converged: list[str] = []
    last: dict[str, tuple[list[str], list[str]]] = {}
    deadline = time.monotonic() + max(wait, 0.0)
    while True:
        reader = Reader(directory, project, context)
        for role in sorted(pending):
            drift, unreadable = check_role(reader, role, pending[role])
            last[role] = (drift, unreadable)
            if not drift and not unreadable:
                converged.append(role)
                # Idempotent across re-runs: a role that has converged since a
                # previous pass must not keep an old verdict on disk.
                try:
                    (directory / f"{role}{DRIFT_SUFFIX}").unlink()
                except FileNotFoundError:
                    pass
        for role in converged:
            pending.pop(role, None)
        remaining = deadline - time.monotonic()
        # Only a role that was READ and found out of shape is worth another
        # pass: a fixture rescheduling onto a healed node converges; a refused
        # or timed-out read does not, and holding the caller's whole wait for
        # it would make every scan with one unreachable API server pay the
        # full deadline for nothing. Such a role is still re-read while another
        # role keeps the loop going, so a transient failure gets its retries.
        waiting = [role for role in pending if last[role][0]]
        if not waiting or remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    drifted = 0
    unchecked = 0
    verdicts: dict[str, tuple[str, list[str]]] = {role: (VERDICT_CONVERGED, []) for role in converged}
    for role in sorted(pending):
        drift, unreadable = last[role]
        # Positive evidence wins: an assertion that was READ and failed is drift
        # whatever else in the role went unread. Only a role whose every failure
        # was a failed read is "not checked", and it gets no drift file -- a
        # fixture nobody saw must not be reported as out of shape.
        if drift:
            drifted += 1
            lines = drift + [f"unread: {u}" for u in unreadable]
            path = directory / f"{role}{DRIFT_SUFFIX}"
            path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
            os.chmod(path, 0o600)
            verdicts[role] = (VERDICT_DRIFTED, lines)
            _warn(
                f"fixture role '{role}' is present but not in its designed state in "
                f"{project}; the cases that depend on it cannot be graded against it: "
                + "; ".join(lines)
            )
        else:
            unchecked += 1
            verdicts[role] = (VERDICT_UNCHECKED, list(unreadable))
            try:
                (directory / f"{role}{DRIFT_SUFFIX}").unlink()
            except FileNotFoundError:
                pass
            _warn(
                f"fixture role '{role}' could not be checked in {project}; nothing is "
                f"known about its state: " + "; ".join(unreadable)
            )
    print(
        SUMMARY_FORMAT.format(
            converged=len(converged), drifted=drifted, unchecked=unchecked, project=project
        ),
        file=sys.stderr,
    )
    if report is not None:
        write_report(report, project, roles, verdicts, converged=len(converged), drifted=drifted, unchecked=unchecked)
    return EXIT_OK


def write_report(path: Path, project: str, roles: dict[str, dict], verdicts: dict[str, tuple[str, list[str]]], **counts: int) -> None:
    """Every catalog role's verdict as JSON, for a caller that runs this
    script per project and must not parse its warnings. A role the runner
    published no kubeconfig for is `unpublished` (the runner already said
    why on its own stderr); one with no `state` list is `no_state`."""
    entries = {}
    for role, spec in sorted(roles.items()):
        if role in verdicts:
            verdict, detail = verdicts[role]
        elif not spec["state"]:
            verdict, detail = VERDICT_NO_STATE, []
        else:
            verdict, detail = VERDICT_UNPUBLISHED, []
        entries[role] = {"cluster_slot": spec["cluster_slot"], "state": verdict, "detail": detail}
    document = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "project": project,
        "roles": entries,
        "summary": dict(counts),
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dir",
        default=os.environ.get("BENCH_FLEET_KUBECONFIG_DIR"),
        help="the directory hack/fleet-kubeconfigs.sh wrote (default: $BENCH_FLEET_KUBECONFIG_DIR)",
    )
    parser.add_argument(
        "--catalog",
        default=os.environ.get("FLEET_CATALOG") or str(here.parent / "bench" / "tf" / "fleet" / "fixtures.json"),
        help="path to fixtures.json (default: $FLEET_CATALOG, else the repository's)",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("FLEET_PROJECT_ID") or os.environ.get("PROJECT_ID"),
        help="project named in messages when .fleet-context does not record one",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=float(os.environ.get("FLEET_FIXTURE_STATE_WAIT_SECONDS") or DEFAULT_WAIT_SECONDS),
        help="seconds to keep re-reading drifted roles before recording drift (default: 0, one pass)",
    )
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, help="seconds between passes")
    parser.add_argument("--report", help="also write every role's verdict as JSON to this path")
    args = parser.parse_args(argv)
    if not args.dir:
        parser.error("no directory: pass --dir or set BENCH_FLEET_KUBECONFIG_DIR")
    return run(Path(args.dir), Path(args.catalog), args.project, args.wait, args.interval, Path(args.report) if args.report else None)


if __name__ == "__main__":
    sys.exit(main())
