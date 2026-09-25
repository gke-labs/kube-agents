#!/usr/bin/env python3
# cluster_agent_reconcile.py - Reconcile Cluster Agent profiles with the live GKE fleet.
#
# Cluster Agents are Hermes profiles on the data PVC ($HERMES_HOME/profiles/<name>), one per
# managed GKE cluster, each stamped with a `cluster_identity` block in its config.yaml.
#
# Policy: **every cluster in every project in scope gets a Cluster Agent profile**, including
# the management cluster where kube-agents itself runs. The scope is the management project
# alone unless the PlatformAgent declares `spec.scope` (docs/designs/multi-project-scope.md),
# which the operator renders to the file KUBEAGENTS_SCOPE_FILE names: explicit projects,
# folders and organisations (resolved through Cloud Asset Inventory) to add, project IDs or
# globs to drop, and single clusters (by project, location and name) to leave unmanaged. RECONCILE_EXCLUDE, a bare-name list matched across every project, keeps
# working for one release alongside `exclude.clusters`. Per run this deterministic engine:
#   • CREATE — scaffolds a profile for every cluster in scope that doesn't have one yet;
#   • PRUNE  — deletes a profile whose cluster is *definitively* gone (a NotFound/404 from
#     `gcloud container clusters describe`), whose cluster is excluded (a triple in
#     `spec.scope.exclude.clusters`, or a bare name in RECONCILE_EXCLUDE), or whose project the
#     scope has dropped, under the three conditions `reconcile()` states. Any other error path
#     — auth, network, timeout, quota, an unreadable identity — is treated as "unknown" and
#     the profile is left untouched: we never delete on ambiguity.
#
# The management cluster used to be excluded, identified via the GKE metadata server. It is
# not any more, because an event on that cluster now needs an agent scoped to it like every
# other cluster's does: the triage session runs on the Planning Agent, whose one instruction is
# to delegate it to the profile scoped to the cluster that raised the event
# (session_kv_server.trigger_agent_troubleshooter), so a cluster without a profile is a
# cluster whose alerts have nobody to answer them. Two consequences worth knowing:
#   • the event watcher must not then watch that cluster twice, once through --in-cluster and
#     once through the new profile — buildWatchSet in cmd/k8s-event-watcher/main.go drops the
#     duplicate;
#   • the management cluster's Cluster Agent can read the harness's own namespace with the pod's
#     GSA — not the KSA, since create_profile pins a get-credentials kubeconfig — so how far that
#     reaches is the GSA's permission set: no Secrets on the default read-only roles, and Secrets
#     included on any `custom` set that names an admin role. `spec.scope.exclude.clusters` is
#     the opt-out (RECONCILE_EXCLUDE for one more release), and the security reference is the
#     canonical statement.
#
# It runs as a `no_agent` cron job on the `default`/chat profile's roster
# (agents/chat/defaults/cron/jobs.json), not the Platform Agent's: it belongs to no one profile,
# and that store is the one the gateway's own ticker thread ticks directly. Scripts and the
# profiles PVC are shared pod-wide, so it operates on every profile regardless of which profile
# ticks it — see "This roster is not inert" and "Never put an id on both rosters" in
# agents/platform/cron/README.md for the ticking model and why the id lives on one roster. It is
# resilient (always exit 0 on the cron path) and posts a summary to every configured chat
# platform only when it created or pruned. `--require-create-pass` opts out of that for a caller
# that has to know whether the roster is actually reconciled; the bootstrap scan gate is the only
# one.

import argparse
import fcntl
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path

import sandbox_exec
from chat_platforms import enabled_chat_platforms
from cluster_agent_profile import (
    HERMES_BIN,
    RESERVED_PROFILES,  # noqa: F401 - re-exported for callers/tests; used indirectly via list_profiles
    create_profile,
    delete_profile,
    profile_name,
    kubeconfig_landed,
    list_profiles,
    profile_home,
    read_cluster_identity,
)

DESCRIBE_TIMEOUT_SECONDS = 30
_MD_BASE = "http://metadata.google.internal/computeMetadata/v1/"
EXTRA_EXCLUDE = {c for c in os.environ.get("RECONCILE_EXCLUDE", "").split(",") if c}

# Where the operator renders spec.scope (platformagent_manifests.go, scopeFileEnvKey). The
# operator renders it on every install, an empty declaration when the CR has no scope, so a
# missing or empty file means the render did not reach this pod (see _load_scope).
SCOPE_FILE_ENV = "KUBEAGENTS_SCOPE_FILE"
# The rendered file says whether the CR carries a scope block at all (see _load_scope).
SCOPE_PRESENT_KEY = "present"
# The resolved membership, rewritten by every run but --dry-run beside the profiles (design §5). The
# previous run's copy is an input: a project in the resolved set last time and absent now
# is marked `retiring`, and only a project the previous copy marked `retiring` is pruned,
# so a profile the scope never produced is never deleted by it.
SNAPSHOT_FILE = "fleet_scope.json"
RESOLVER_EXPLICIT = "explicit"
# Set when a folder or organisation is declared: containers resolve through Cloud Asset
# Inventory, one call per container (design §4).
RESOLVER_ASSET_INVENTORY = "asset-inventory"
CONTAINER_KINDS = ("folders", "organizations")
ASSET_TYPE_CLUSTER = "container.googleapis.com/Cluster"
# The two fields the resolver reads, projected so a container of thousands of clusters
# stays far below the credential proxy's output cap; full-fidelity JSON is ~1 KB a row.
ASSET_SEARCH_FORMAT = "json(name,location)"
# The credential proxy's stderr note when it cut a stream at its output cap.
_PROXY_TRUNCATED_MARKER = "truncated"
# Regional clusters render as .../locations/<L>/clusters/<C>, zonal ones as
# .../zones/<Z>/clusters/<C>; a parser written to one shape drops the other (design §4).
_ASSET_NAME = re.compile(r"^//container\.googleapis\.com/projects/(?P<project>[^/]+)/(?:locations|zones)/(?P<location>[^/]+)/clusters/(?P<cluster>[^/]+)$")
# Projects whose per-cluster describe or get-credentials answered 403 this run: a container
# member's outcome starts as `ok` because Asset Inventory listed its clusters without a
# per-project call, and these two calls are what revise it to `denied` (design §4).
_denied_this_run: set[str] = set()
# Same, for a member whose GKE API answered disabled on a per-cluster call (also a 403).
_api_disabled_this_run: set[str] = set()
# The listing phase is bounded: the management project lists first and alone, then the
# containers resolve LIST_WORKERS at a time, then the explicit projects LIST_WORKERS at a
# time, and a lookup still running when LIST_BUDGET_SECONDS is spent reads unreachable. The
# bootstrap gate runs this script under its own ceiling (RECONCILE_TIMEOUT_SECONDS there, 240s)
# and kills it on expiry with nothing written; two hanging projects listed in turn at
# LIST_TIMEOUT_SECONDS each would already overrun it. Creates still run in the fixed order.
LIST_WORKERS = 8
LIST_TIMEOUT_SECONDS = 120
LIST_BUDGET_SECONDS = 150
LIST_GRACE_SECONDS = 5
# How many unlisted projects the chat notification names before it counts the rest: a
# container can resolve to thousands of projects, a chat message has a size ceiling, and
# a message the platform drops for its size takes the created/pruned summary with it.
NOTIFY_UNLISTED_LIMIT = 8
VIA_MANAGEMENT = "management"
VIA_EXPLICIT = "explicit"
# Two caps of 100 (design §3; a declared value replaces this one in a follow-up): the CRD caps each declared list, and this caps the resolved
# set, the management project included. Explicit projects fill it in sorted order after the
# management project; one past the cap reads over-cap, keeps its profiles, and gets no CREATE.
RESOLVED_SET_CAP = 100
OUTCOME_OK = "ok"
OUTCOME_DENIED = "denied"
OUTCOME_API_DISABLED = "api-disabled"
OUTCOME_UNREACHABLE = "unreachable"
OUTCOME_OVER_CAP = "over-cap"
STATE_IN_SCOPE = "in-scope"
STATE_RETIRING = "retiring"
SNAPSHOT_TMP_SUFFIX = ".tmp"
SNAPSHOT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# How long a project reached only through a container is kept after the asset index stops
# placing it under one, before the ordinary two-run retire applies. Covers the index's lag
# after a move (minutes to hours, measured) without keeping a deleted project's profiles for
# ever; a project deleted answers 403 to describe, never NotFound, so nothing else retires it.
SECONDS_PER_HOUR = 3600
INDEX_LAG_GRACE_SECONDS = 24 * SECONDS_PER_HOUR
ABSENT_SINCE_KEY = "absentSince"
# How much of an unreadable search row the log quotes.
LOG_ROW_PREVIEW_CHARS = 200
# What gcloud says when the account is not granted in a project, and when the GKE API is
# off there. Anything else is unreachable: the run learned nothing and keeps everything.
_DENIED_MARKERS = ("PERMISSION_DENIED", "403", "does not have permission", "Permission denied")
# The create path reads a narrower set: its exception text also carries the shim's own
# "[Errno 13] Permission denied" on a data-volume or sandbox-directory fault, which is
# not an IAM answer, so only gcloud's 403 wording counts there.
_CREATE_DENIED_MARKERS = ("PERMISSION_DENIED", "code=403", "does not have permission")
_API_DISABLED_MARKERS = ("SERVICE_DISABLED", "accessNotConfigured", "API has not been used",
                         "is not enabled", "has not been enabled")


def log(msg: str) -> None:
    print(f"[CLUSTER-RECONCILE] {msg}", file=sys.stderr)


def _run_env() -> dict[str, str]:
    """HOME -> /tmp so a subprocess can write on the writable scratch disk.

    For `hermes` only. Every gcloud call in this file goes through
    `sandbox_exec.run`, which runs it in the shell sandbox and builds its own
    environment there — this one carries the agent pod's, including
    `API_SERVER_KEY`, and must not travel over the connection.
    """
    return {**os.environ, "HOME": "/tmp"}


def _metadata(path: str):
    """Read a GKE/GCE metadata value, or None if unavailable."""
    try:
        req = urllib.request.Request(_MD_BASE + path, headers={"Metadata-Flavor": "Google"})
        # Context-managed: this runs on a cron tick, so a socket left to the garbage
        # collector is a socket leaked once per tick, forever.
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode().strip()
    except Exception:  # noqa: BLE001
        return None


def _project_source() -> tuple[str | None, bool]:
    """The management project and whether the answer is authoritative.

    The metadata server is authoritative, and so is RECONCILE_PROJECT where it
    still reaches this script: the operator pins it empty in the managed .env, so
    that a line in the PVC .env cannot re-point the management project and have
    the scope prune retire the real one, and an empty value reads as unset. The
    gcloud config fallback is not authoritative: it answers whatever the broker
    was bootstrapped with, so a metadata timeout can make it name a project the
    pod does not run in, and the reconcile must not read that as the management
    project having changed.
    """
    p = os.environ.get("RECONCILE_PROJECT") or _metadata("project/project-id")
    if p:
        return p, True
    try:
        r = sandbox_exec.run(["gcloud", "config", "get-value", "project"], timeout=30)
        return r.stdout.strip() or None, False
    except Exception:  # noqa: BLE001
        return None, False


def _hermes_home() -> Path:
    """The data volume: profiles, the reconcile lock, and the scope snapshot live here."""
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _classify_list_failure(stderr: str) -> str:
    """Name why a `clusters list` failed, in the design's vocabulary (§4).

    `denied` and `api-disabled` are read off gcloud's stderr; everything else is
    `unreachable`. All three keep existing profiles; the difference is what the
    snapshot tells the operator to fix.
    """
    if any(m in stderr for m in _API_DISABLED_MARKERS):
        return OUTCOME_API_DISABLED
    if any(m in stderr for m in _DENIED_MARKERS):
        return OUTCOME_DENIED
    return OUTCOME_UNREACHABLE


def _bounded_map(lookup, keys: list[str], deadline: float, what: str) -> dict:
    """Run `lookup(key, timeout=...)` for every key, LIST_WORKERS at a time, within the deadline.

    Each worker's own timeout is cut to the budget left when it starts, so no thread
    outlives the run by more than LIST_GRACE_SECONDS: the interpreter joins the pool's
    threads at exit, and a worker still blocked on gcloud would hold the exit code past
    the bootstrap gate's ceiling. A lookup still pending at the deadline reads
    `(None, unreachable)`: no CREATE under it, scope prune off, the run goes on to write
    its snapshot.
    """
    if not keys:
        return {}

    def within_budget(key: str):
        return lookup(key, timeout=max(1.0, min(LIST_TIMEOUT_SECONDS, deadline - time.monotonic())))

    pool = ThreadPoolExecutor(max_workers=min(LIST_WORKERS, len(keys)))
    futures = {key: pool.submit(within_budget, key) for key in keys}
    done, _ = wait(futures.values(), timeout=max(0.0, deadline + LIST_GRACE_SECONDS - time.monotonic()))
    results: dict = {}
    for key, future in futures.items():
        if future in done:
            results[key] = future.result()
        else:
            log(f"{what} {key} did not finish within the run's {LIST_BUDGET_SECONDS}s listing budget "
                f"({OUTCOME_UNREACHABLE}; skipping create for it this run).")
            results[key] = (None, OUTCOME_UNREACHABLE)
    pool.shutdown(wait=False, cancel_futures=True)
    return results


def _search_containers(containers: list[str], deadline: float) -> dict[str, tuple[dict | None, str]]:
    """Resolve every container within the run's listing budget; a pending one freezes (design §4)."""
    return _bounded_map(_search_container, containers, deadline, "resolving")


def _list_projects(projects: list[str], deadline: float) -> dict[str, tuple[list | None, str]]:
    """List every explicit project within the run's listing budget, LIST_WORKERS at a time.

    The caller lists the management project first and on its own, at the start of the
    budget, before calling this: its listing decides `create_pass_ran`, and when it
    reaches the sandbox its ssh opens the multiplexed connection the pool then shares.
    """
    return _bounded_map(_list_project, projects, deadline, "listing clusters in")


def _list_project(project: str, timeout: float = LIST_TIMEOUT_SECONDS) -> tuple[list | None, str]:
    """Every cluster in the project as (project, name, location) tuples, and the outcome.

    `check=True` matters: without it a failed `gcloud` (expired auth, no network,
    revoked permission) returns a non-zero exit with empty stdout, which parses to
    an empty list and is indistinguishable from "this project has no clusters".

    (None, outcome) means the list could not be read; ([], "ok") means the project
    genuinely has no clusters. The caller degrades identically either way — PRUNE
    runs off `_cluster_exists`, not this list, so a bad list can never delete
    anything — but the outcome is what tells the bootstrap gate, and the snapshot,
    which projects the roster it is about to read actually covers.
    """
    try:
        r = sandbox_exec.run(
            ["gcloud", "container", "clusters", "list", "--project", project,
             "--format=value(name,location)"],
            check=True, timeout=timeout,
        )
    except subprocess.CalledProcessError as e:
        # CalledProcessError stringifies to just the exit status; gcloud puts the
        # actual reason on stderr, which is the only part worth reading.
        stderr = (e.stderr or "").strip()
        outcome = _classify_list_failure(stderr)
        log(f"listing clusters in {project} failed ({outcome}; skipping create for it this run): "
            f"{stderr or e}")
        return None, outcome
    except Exception as e:  # noqa: BLE001 - timeout, gcloud missing, OSError
        log(f"listing clusters in {project} failed (unreachable; skipping create for it this run): {e}")
        return None, OUTCOME_UNREACHABLE
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out.append((project, parts[0], parts[1]))
    return out, OUTCOME_OK


def _empty_scope() -> dict:
    return {"projects": [], "folders": [], "organizations": [], "exclude": {"projects": [], "clusters": []}}


def _load_scope() -> tuple[dict, bool, bool, bool]:
    """The declaration the operator rendered: (scope, readable, present, containers_known).

    The operator renders the file on every install, so a missing, empty,
    unparseable or non-object file means the render did not reach this pod (a
    rollback to an operator without the field): not readable, and the caller must
    not run the scope prune, because a declaration that cannot be read must not
    become a declaration that deletes. A file that reads but whose `present` is
    not true means the CR carries no scope block: readable, so a run can still be
    clean, but not a declaration, so a project an earlier block declared is
    carried forward rather than retired. A block can go missing without anyone
    dropping a project, through a write that passed an older operator's webhook;
    the operator who wants the projects gone empties `projects` and keeps the
    block. In both cases the caller creates for the management project alone,
    under the last declaration's exclusions. `containers_known` says whether the
    render carries the `folders` and `organizations` keys at all: a render from an
    operator that predates them (a rollback) declares nothing about containers, so a
    project reached through one is carried, not retired.
    """
    path = os.environ.get(SCOPE_FILE_ENV)
    if not path:
        # An agent image ahead of its operator: no variable, no render. CREATE under the last
        # declaration's exclusions, and no scope prune, because nothing declared anything.
        log(f"{SCOPE_FILE_ENV} is not set; using the management project alone and skipping the scope prune.")
        return _empty_scope(), False, False, False
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
        if not raw:
            raise ValueError("empty file")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    except Exception as e:  # noqa: BLE001 - unreadable declaration: fall back, loudly, and prune nothing by scope
        log(f"could not read the scope declaration at {path} ({e}); using the management project "
            "alone and skipping the scope prune this run.")
        return _empty_scope(), False, False, False
    containers_known = any(kind in parsed for kind in CONTAINER_KINDS)
    if parsed.get(SCOPE_PRESENT_KEY) is not True:
        # No block on the CR (or a render that predates the marker): nothing declared.
        return _empty_scope(), True, False, containers_known
    return _normalize_scope(parsed), True, True, containers_known


def _normalize_scope(parsed: dict) -> dict:
    """A declaration in the file's shape, keeping only the entries of the right type."""
    def strings(value) -> list[str]:
        # A field that is not a list (a scalar, a string) is treated as absent rather than
        # iterated: a string would come apart into characters, an int would abort the run.
        return [p for p in value if isinstance(p, str)] if isinstance(value, list) else []

    scope = _empty_scope()
    scope["projects"] = strings(parsed.get("projects"))
    for kind in CONTAINER_KINDS:
        scope[kind] = [c for c in strings(parsed.get(kind)) if c.isdigit()]
    exclude = parsed.get("exclude") if isinstance(parsed.get("exclude"), dict) else {}
    scope["exclude"]["projects"] = strings(exclude.get("projects"))
    clusters = exclude.get("clusters")
    scope["exclude"]["clusters"] = [
        c for c in (clusters if isinstance(clusters, list) else [])
        if isinstance(c, dict) and all(isinstance(c.get(k), str) for k in ("projectId", "location", "clusterName"))
    ]
    return scope


def _previous_declaration(previous: dict | None) -> dict | None:
    """The declaration the last run read, from the snapshot's `declared`, or None.

    A run that cannot read the declaration keeps this one's exclusions: a rollback to an
    operator without the field must not re-onboard a cluster the operator excluded, which
    is the one profile the security page tells a `custom`-role install to keep away.
    """
    if not previous or not isinstance(previous.get("declared"), dict):
        return None
    return _normalize_scope(previous["declared"])


def _excluded_by(project: str, patterns: list[str]) -> str | None:
    """The first `exclude.projects` entry that matches, an ID or a shell-style glob."""
    for pattern in patterns:
        if fnmatch.fnmatchcase(project, pattern):
            return pattern
    return None


def _container_ids(scope: dict) -> list[str]:
    """The declared containers as `folders/<id>` and `organizations/<id>`, sorted by ID."""
    return sorted(f"{kind}/{cid}" for kind in CONTAINER_KINDS for cid in set(scope.get(kind) or []))


def _parse_asset(asset: dict) -> tuple[str, str, str] | None:
    """(project, cluster, location) from one search-all-resources result, or None.

    The project ID is the segment after `projects/` in the asset name: the `project`
    field carries the project number, which nothing downstream keys on. The location is
    the asset's `location` field when present, else the path segment (design §4).
    """
    if not isinstance(asset, dict):
        return None
    m = _ASSET_NAME.match(str(asset.get("name") or ""))
    if not m:
        return None
    location = asset.get("location") if isinstance(asset.get("location"), str) and asset.get("location") else m.group("location")
    return m.group("project"), m.group("cluster"), location


def _search_container(container: str, timeout: float = LIST_TIMEOUT_SECONDS) -> tuple[dict[str, list[tuple[str, str, str]]] | None, str]:
    """Every GKE cluster under a folder or organisation, grouped by project ID, and the outcome.

    One Cloud Asset Inventory call per container, through the broker (design §4). (None,
    outcome) means the container could not be read; its previous members are then carried
    forward under that outcome by the caller, and the scope prune stays off for the run.
    """
    cmd = ["gcloud", "asset", "search-all-resources", f"--scope={container}",
           f"--asset-types={ASSET_TYPE_CLUSTER}", f"--format={ASSET_SEARCH_FORMAT}"]
    try:
        result = sandbox_exec.run(cmd, check=True, timeout=timeout)
        try:
            assets = json.loads(result.stdout or "[]")
        except ValueError as e:
            if _PROXY_TRUNCATED_MARKER in (result.stderr or ""):
                raise ValueError("the credential proxy cut the search output at its cap; the container "
                                 "holds more clusters than one search can return through it") from e
            raise
        if not isinstance(assets, list):
            raise ValueError("asset search did not return a list")
    except subprocess.CalledProcessError as e:
        outcome = _classify_list_failure(e.stderr or "")
        log(f"resolving {container} through Cloud Asset Inventory failed ({outcome}; its previous "
            f"members are carried forward, no CREATE under them): {(e.stderr or '').strip()}")
        return None, outcome
    except subprocess.TimeoutExpired:
        log(f"resolving {container} timed out ({OUTCOME_UNREACHABLE}); its previous members are carried forward.")
        return None, OUTCOME_UNREACHABLE
    except Exception as e:  # noqa: BLE001 - a failed lookup is never a resolved-empty container
        log(f"resolving {container} errored ({OUTCOME_UNREACHABLE}; its previous members are carried forward): {e}")
        return None, OUTCOME_UNREACHABLE
    members: dict[str, list[tuple[str, str, str]]] = {}
    for asset in assets:
        triple = _parse_asset(asset)
        if triple is None:
            # The search is filtered to one asset type, so a row this parser cannot read is
            # not a foreign asset to skip but a shape this run does not know (a name format
            # change, a projection that dropped `name`). Reading it as "no clusters" would
            # retire every member a day later while the container reported healthy; a
            # failed lookup freezes instead (design §4).
            log(f"resolving {container}: a search row has a shape this run cannot read "
                f"({str(asset)[:LOG_ROW_PREVIEW_CHARS]!r}); {OUTCOME_UNREACHABLE}, its previous members are carried forward.")
            return None, OUTCOME_UNREACHABLE
        members.setdefault(triple[0], []).append(triple)
    return members, OUTCOME_OK


def _previous_outcome(previous: dict | None, project: str) -> str | None:
    """The outcome the last snapshot recorded for the project, if any."""
    for p in (previous or {}).get("projects", []):
        if isinstance(p, dict) and p.get("id") == project:
            return p.get("outcome") if isinstance(p.get("outcome"), str) else None
    return None


def _previous_container_members(previous: dict | None, container: str) -> list[str]:
    """Project IDs the last snapshot reached through this container, for the freeze rule."""
    if not previous:
        return []
    return sorted({
        p["id"] for p in previous.get("projects", [])
        if isinstance(p, dict) and p.get("id") and container in (p.get("via") or [])
    })


def _resolve_projects(management: str | None, scope: dict,
                      searches: dict[str, tuple[dict | None, str]] | None = None,
                      previous: dict | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Turn the declaration into the ordered resolved set (design §3).

    Returns (entries, ignored_excludes, containers). Each entry is {id, via, outcome},
    where outcome is None for a project still to be listed, `ok` for a container member
    whose clusters Asset Inventory already named (kept under `clusters`), a container's
    own outcome for a member carried forward under the freeze rule, and `over-cap` for a
    project past RESOLVED_SET_CAP. The order is fixed so the cap binds the same way every
    run: the management project, then explicit projects sorted by ID, then containers
    sorted by ID (design §3). A glob that matches the management project is recorded and
    not applied. `searches` holds each container's Asset Inventory result.
    """
    patterns = scope["exclude"]["projects"]
    entries: list[dict] = []

    def listed_count() -> int:
        # What the cap counts: the projects this run lists, and the ones a frozen container
        # last listed. A member carried over-cap is in the set but not listed, so it does
        # not close the cap on the containers after its own (design §3: a container that
        # does not fit is skipped and the next one is still tried); a frozen member that was
        # over-cap last run was not listed then either (`uncounted`), so one failed search
        # of a large folder does not shut its small siblings out for the run.
        return sum(1 for e in entries if e["outcome"] != OUTCOME_OVER_CAP and not e.get("uncounted"))

    ignored: list[dict] = []
    seen: set[str] = set()
    if management:
        pattern = _excluded_by(management, patterns)
        if pattern:
            log(f"exclude.projects entry {pattern!r} matches the management project {management}; "
                "ignored, the management project is always in scope.")
            ignored.append({"project": management, "pattern": pattern})
        entries.append({"id": management, "via": [VIA_MANAGEMENT], "outcome": None})
        seen.add(management)
    def add_via(project: str, source: str) -> None:
        for entry in entries:
            if entry["id"] == project and source not in entry["via"]:
                entry["via"] = sorted(entry["via"] + [source])

    for project in sorted(set(scope["projects"])):
        if project in seen:
            # Declared explicitly as well as being the management project: both vias.
            add_via(project, VIA_EXPLICIT)
            continue
        seen.add(project)
        if _excluded_by(project, patterns):
            continue
        outcome = OUTCOME_OVER_CAP if listed_count() >= RESOLVED_SET_CAP else None
        if outcome:
            log(f"{project} is past the resolved-set cap of {RESOLVED_SET_CAP}; over-cap, no CREATE.")
        entries.append({"id": project, "via": [VIA_EXPLICIT], "outcome": outcome})

    def frozen_entry(project: str) -> dict | None:
        for entry in entries:
            if entry["id"] == project and entry.get("frozen"):
                return entry
        return None

    def mark_indexed(project: str) -> None:
        # A successful lookup placed the project under this container; whatever else the
        # entry is (explicit, management, carried frozen by an earlier container), the index
        # saw it this run, which is what the snapshot's index-lag stamp asks.
        for entry in entries:
            if entry["id"] == project:
                entry["indexed"] = True

    containers: list[dict] = []
    for container in _container_ids(scope):
        members, outcome = (searches or {}).get(container, (None, OUTCOME_UNREACHABLE))
        if members is not None:
            fresh = [p for p in sorted(members) if p not in seen and not _excluded_by(p, patterns)]
            # A member an earlier container carried over-cap is in the set but not listed;
            # this container would list it (the live listing wins below), so it counts here
            # like a fresh one, or a second, overlapping container would lift a whole
            # over-cap folder past the cap without a check.
            lifted = [p for p in members if p in seen and (frozen_entry(p) or {}).get("outcome") == OUTCOME_OVER_CAP
                      or (frozen_entry(p) or {}).get("uncounted")]
            if listed_count() + len(fresh) + len(lifted) > RESOLVED_SET_CAP:
                # The lookup succeeded and the run holds the full member list, but listing
                # them would cross the cap: the members the run just resolved are carried
                # reading over-cap and get no CREATE, and because the run knows they are
                # under the container, over-cap does not hold back the prune (design §3/§4 as revised).
                log(f"{container} resolved {len(members)} project(s), which would cross the resolved-set "
                    f"cap of {RESOLVED_SET_CAP}; over-cap, its members are carried without CREATE.")
                outcome, carried, members = OUTCOME_OVER_CAP, sorted(members), None
            else:
                carried = []
        else:
            # Frozen (design §4): the members the last snapshot reached through this
            # container are carried forward under the container's outcome, so a failed
            # lookup never reads as "no projects", and nothing is created under them.
            carried = _previous_container_members(previous, container)
        if members is None:
            for project in carried:
                if project in seen:
                    add_via(project, container)
                    if outcome == OUTCOME_OVER_CAP:
                        mark_indexed(project)
                    continue
                if _excluded_by(project, patterns):
                    continue
                seen.add(project)
                # `indexed`: an over-cap member was placed by the index this run (the lookup
                # succeeded); a frozen one was not. The snapshot's index-lag stamp reads it.
                entries.append({"id": project, "via": [container], "outcome": outcome, "frozen": True,
                                "indexed": outcome == OUTCOME_OVER_CAP,
                                "uncounted": outcome != OUTCOME_OVER_CAP
                                and _previous_outcome(previous, project) == OUTCOME_OVER_CAP})
            containers.append({"id": container, "outcome": outcome, "projects": len(carried)})
            continue
        for project in sorted(members):
            if project in seen:
                add_via(project, container)
                mark_indexed(project)
                # Listed by this container but carried frozen by an earlier one: the live
                # listing wins, whichever container sorted first (an over-cap one was counted
                # against the cap above before this container read ok).
                frozen = frozen_entry(project)
                if frozen:
                    frozen.pop("frozen")
                    frozen.pop("uncounted", None)
                    frozen["outcome"] = OUTCOME_OK
                    frozen["clusters"] = sorted(members[project])
                continue
            if _excluded_by(project, patterns):
                continue
            seen.add(project)
            entries.append({"id": project, "via": [container], "outcome": OUTCOME_OK, "clusters": sorted(members[project])})
        containers.append({"id": container, "outcome": OUTCOME_OK, "projects": len(members)})
    for entry in entries:
        entry.pop("frozen", None)
        entry.pop("uncounted", None)
    return entries, ignored, containers


def _snapshot_path() -> Path:
    return _hermes_home() / SNAPSHOT_FILE


def _load_previous_snapshot() -> dict | None:
    path = _snapshot_path()
    try:
        if not path.exists():
            return None
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("projects"), list):
            raise ValueError("not a snapshot object with a projects list")
        return parsed
    except Exception as e:  # noqa: BLE001 - a corrupt snapshot means "no previous run", which prunes nothing
        log(f"could not read the previous scope snapshot at {path} ({e}); treating this as the first run.")
        return None


def _write_snapshot(snapshot: dict) -> None:
    """Atomic, key-sorted write so an unchanged fleet leaves an unchanged file apart from resolvedAt."""
    path = _snapshot_path()
    try:
        tmp = path.with_suffix(SNAPSHOT_TMP_SUFFIX)
        tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 - the snapshot is a report; failing to write it never fails the run
        log(f"could not write the scope snapshot at {path}: {e}")


def _previous_via(previous: dict | None, project: str) -> list[str]:
    """The `via` the last snapshot recorded for a project, or [] when it had none."""
    for p in (previous or {}).get("projects", []):
        if isinstance(p, dict) and p.get("id") == project:
            return [v for v in (p.get("via") or []) if isinstance(v, str)]
    return []


def _previous_container_ids(previous: dict | None) -> set[str]:
    """The containers the last run knew as declared, by id, or an empty set.

    Read from the snapshot's `declared` first: a tick that could not read the declaration
    resolves no container and writes `containers: []`, but carries `declared` forward on
    purpose, and the next readable tick must not read every folder as newly declared (which
    would hold a dropped explicit project for a day under the index's reason). The
    `containers` array is added for a snapshot written before `declared` existed.
    """
    declaration = _previous_declaration(previous)
    known = set(_container_ids(declaration)) if declaration else set()
    known.update(c["id"] for c in (previous or {}).get("containers", []) if isinstance(c, dict) and isinstance(c.get("id"), str))
    return known


def _previous_absent_since(previous: dict | None, project: str) -> str | None:
    """When the last snapshot first found a container member absent from the index, if it did."""
    for p in (previous or {}).get("projects", []):
        if isinstance(p, dict) and p.get("id") == project and isinstance(p.get(ABSENT_SINCE_KEY), str):
            return p[ABSENT_SINCE_KEY]
    return None


def _previous_management(previous: dict | None) -> str | None:
    """The management project the last run resolved, from its `via`, or None."""
    if not previous:
        return None
    for p in previous.get("projects", []):
        if isinstance(p, dict) and VIA_MANAGEMENT in (p.get("via") or []) and p.get("id"):
            return p["id"]
    return None


def remaining_profiles(project: str, identities: dict, pruned: list[str]) -> int:
    """Profiles of a project still on the volume after this run's deletes.

    A pruned name still counts while its home is on disk: `delete_profile` swallows
    its own errors, and a project whose delete failed must stay `retiring` so the
    next run tries again instead of reading the survivor as never in scope.
    """
    pruned_set = set(pruned)
    return sum(
        1 for n, i in identities.items()
        if i and i["project"] == project and (n not in pruned_set or profile_home(n).exists())
    )


def _previous_attribution(previous: dict | None) -> dict[str, str]:
    """Profile name -> project ID as the last run read them (the snapshot's `profiles`).

    The one use is a profile whose identity cannot be read this run: it keeps only the
    project it was last attributed to retiring, never every retiring project, and the
    write carries its attribution forward so a second unreadable run reads the same.
    """
    if not previous or not isinstance(previous.get("profiles"), dict):
        return {}
    return {n: p for n, p in previous["profiles"].items() if isinstance(n, str) and isinstance(p, str)}


def _previously_retiring(previous: dict | None) -> set[str]:
    """Project IDs the last run marked retiring: the ones this run may prune."""
    if not previous:
        return set()
    return {
        p.get("id") for p in previous.get("projects", [])
        if isinstance(p, dict) and p.get("state") == STATE_RETIRING and p.get("id")
    }


def _previously_resolved(previous: dict | None) -> set[str]:
    """Project IDs the last run listed as in scope or retiring (design §7, third condition)."""
    if not previous:
        return set()
    return {
        p.get("id") for p in previous.get("projects", [])
        if isinstance(p, dict) and p.get("state") in (STATE_IN_SCOPE, STATE_RETIRING) and p.get("id")
    }


def _cluster_exists(project: str, cluster: str, location: str) -> bool | None:
    """Return True if the GKE cluster exists, False if it definitively does not, None if unknown.

    Mirrors platform_mcp_server.verify_gke_cluster's classification: a NotFound/404 is the *only*
    signal that authorizes deletion. Any other failure (auth, network, timeout, quota) returns
    None so the caller leaves the profile in place.
    """
    cmd = [
        "gcloud", "container", "clusters", "describe", cluster,
        f"--location={location}", f"--project={project}", "--format=json(status, id)",
    ]
    try:
        sandbox_exec.run(cmd, check=True, timeout=DESCRIBE_TIMEOUT_SECONDS)
        return True
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if "NotFound" in stderr or "not found" in stderr.lower() or "404" in stderr:
            return False
        classified = _classify_list_failure(stderr)
        if classified == OUTCOME_DENIED:
            _denied_this_run.add(project)
        elif classified == OUTCOME_API_DISABLED:
            _api_disabled_this_run.add(project)
        log(f"describe {cluster} ({project}/{location}) failed (treating as unknown): {stderr.strip()}")
        return None
    except subprocess.TimeoutExpired:
        log(f"describe {cluster} ({project}/{location}) timed out (treating as unknown).")
        return None
    except Exception as e:  # noqa: BLE001 - any unexpected failure is 'unknown', never 'absent'
        log(f"describe {cluster} ({project}/{location}) errored (treating as unknown): {e}")
        return None


# Distinct from 1 so a caller can tell "the roster is not reconciled" from a crash.
EXIT_CREATE_PASS_SKIPPED = 3
# Another reconcile holds the lock. Also distinct from 1: the caller has learned
# nothing about the roster and should retry rather than count this as a failure.
EXIT_ALREADY_RUNNING = 4

RECONCILE_LOCK = ".cluster_agent_reconcile.lock"


@contextmanager
def _exclusive_run():
    """Hold the reconcile lock, or yield False if another run already has it.

    Two schedules drive this script — the hourly `cluster-agent-reconcile` job and
    the bootstrap scan gate, which runs it every minute until the roster is usable —
    and the gateway's cron lock is per job id, so nothing upstream keeps the two
    apart. Overlapping runs would call `create_profile` and `delete_profile` against
    the same profile home: interleaved read-modify-writes of `config.yaml` and
    `.env`, or an rmtree under a scaffold in progress. The lock lives here rather
    than in either caller because it has to cover both.
    """
    path = _hermes_home() / RECONCILE_LOCK
    try:
        handle = open(path, "w")  # noqa: SIM115 - closed by this contextmanager
    except Exception as e:  # noqa: BLE001 - an unlockable path must not block the roster
        log(f"could not open {path} ({e}); running without the lock.")
        yield True
        return
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True

# Written by create_profile after the identity stamp, so their absence means the
# scaffold was interrupted between the two. The kubeconfig is checked separately:
# it is not on this pod's filesystem.
SCAFFOLD_ARTIFACTS = ("USER.md",)

# What create_profile fetches in step 3, relative to the profile home. Named here
# because this pod cannot stat it -- the path is resolved on whichever side
# kubectl runs, which kubeconfig_landed decides.
KUBECONFIG_ARTIFACT = "kubeconfig.yaml"


def _scaffold_gaps(home: Path) -> list[str]:
    """Artifacts create_profile writes after the identity stamp that this home lacks.

    ``create_profile`` stamps ``cluster_identity`` into ``config.yaml`` (step 2b)
    before it fetches the kubeconfig (step 3) and writes ``USER.md`` (step 4). A
    process killed in that window -- the bootstrap gate runs this script under a
    240s timeout, and Python SIGKILLs on expiry -- leaves a home that reads as fully
    managed: CREATE finds its identity tuple and skips the cluster, PRUNE keeps it
    because the cluster still exists, and the half-scaffolded profile survives with
    no credentials for the life of the volume. Treating it as absent re-runs the
    scaffold, which is idempotent.

    The kubeconfig is asked for over the sandbox rather than stat'ed here. With a
    sandbox, ``gcloud container clusters get-credentials`` runs in the shell pod
    and writes to the shell pod's volume, so this pod never sees the file:
    stat'ing it locally reports every profile incomplete on every tick, which
    re-scaffolds the whole fleet hourly and re-fetches a credential for each.
    ``kubeconfig_landed`` asks the side that has it -- the same way create_profile
    confirmed the fetch -- and answers "not landed" when the sandbox cannot be
    reached, which is the case a recreated sandbox volume actually needs.
    """
    gaps = [f for f in SCAFFOLD_ARTIFACTS if not (home / f).exists()]
    if not kubeconfig_landed(home / KUBECONFIG_ARTIFACT):
        gaps.insert(0, KUBECONFIG_ARTIFACT)
    return gaps


def reconcile(dry_run: bool = False) -> dict:
    """Reconcile Cluster Agent profiles with the clusters in scope (create + prune).

    Returns a structured report dict with the profile names/clusters in each outcome bucket.
    Isolated per-item: one bad profile/cluster never aborts the sweep.
    """
    report: dict[str, list] = {
        "created": [],           # profile scaffolded for a cluster that lacked one
        "pruned": [],            # profile removed (cluster gone, excluded, or its project left the scope)
        "kept": [],              # cluster still exists and should be managed
        "skipped_no_identity": [],  # config.yaml lacked a usable cluster_identity
        "skipped_error": [],     # liveness check was inconclusive (auth/network/etc.)
        "incomplete": [],        # identity stamped but the scaffold never finished
        "create_failed": [],     # cluster that should have a profile and could not get one
        "unmanaged": [],  # kept and listed: project never produced by the scope, retiring, or carried forward
        "retiring": [],          # project the scope dropped whose profiles are being removed
    }
    # Per-project outcome (design §4), keyed by project ID. Not a bucket of names: the
    # same outcomes go into the snapshot, which the bootstrap gate reads to name the
    # projects a roster is missing.
    report["projects"] = {}
    # Not a bucket: whether the CREATE direction ran for at least one project this run.
    # Every failure below is caught and logged so a cron producer can always exit 0,
    # which leaves a caller no way to tell "this scope has no clusters to add" from
    # "every list call failed". `--require-create-pass` turns this into an exit code
    # for the one caller that needs the difference.
    report["create_pass_ran"] = False

    profiles = list_profiles()
    # The homes that predate this run, by name: a create-path rollback below removes only a
    # home this run made, never one that was already here (an incomplete re-run, a profile
    # whose identity cannot be read), which PRUNE deliberately keeps.
    preexisting_homes = set(profiles)
    identities = {name: read_cluster_identity(profile_home(name)) for name in profiles}
    existing_keys = set()
    for name, identity in identities.items():
        if not identity:
            continue
        missing = _scaffold_gaps(profile_home(name))
        if missing:
            log(f"{name}: incomplete scaffold ({', '.join(missing)} missing) — recreating.")
            report["incomplete"].append(name)
            continue
        existing_keys.add((identity["project"], identity["cluster"], identity["location"]))

    # --- RESOLVE: the management project plus whatever spec.scope declares (design §3).
    management, management_authoritative = _project_source()
    _denied_this_run.clear()
    _api_disabled_this_run.clear()
    scope, scope_readable, scope_present, containers_known = _load_scope()
    previous = _load_previous_snapshot()
    # An unreadable or absent declaration keeps the exclusions of the last one read. The
    # projects do not carry: nothing is listed or created outside the management project
    # on such a tick, but a cluster the operator excluded stays excluded, so a rollback
    # cannot re-onboard it. The snapshot keeps naming that last declaration, so a second
    # such tick reads the same exclusions, and a project it named stays carried in scope
    # rather than retiring: removing the whole block retires nothing.
    declared = scope
    if not scope_present:
        last = _previous_declaration(previous)
        if last:
            scope["exclude"] = last["exclude"]
            declared = last
            carried = len(last["exclude"]["projects"]) + len(last["exclude"]["clusters"])
            state = "not readable" if not scope_readable else "absent from the PlatformAgent"
            if carried or last["projects"]:
                log(f"scope declaration {state} this run; keeping the {carried} exclusion(s) of the last one "
                    f"read and carrying its {len(last['projects'])} project(s) without retiring them.")
    excluded_triples = {
        (c["projectId"], c["clusterName"], c["location"]) for c in scope["exclude"]["clusters"]
    }
    previously_resolved = _previously_resolved(previous)
    # A fallback answer that disagrees with the previous run is not a changed management
    # project, it is a metadata timeout answered by the broker's gcloud config: treated as
    # unresolved, so the previous identity is carried forward below and nothing is judged
    # or retired on its account. Only RECONCILE_PROJECT or the metadata server can move it.
    fallback_disagrees = _previous_management(previous)
    if (management and not management_authoritative and fallback_disagrees
            and management != fallback_disagrees):
        log(f"management project {management} came from the gcloud config fallback and differs "
            f"from the previous run's {fallback_disagrees}; treated as unresolved this run.")
        management = None
    # A tick that cannot resolve the management project puts the previous one in the
    # management slot instead, unreachable: the fill order and the cap are then the same as
    # on any other tick, the snapshot keeps naming it so the next tick can tell a management
    # project that merely changed from one the scope dropped, and nothing is created under
    # a project this tick could not confirm is still the pod's own.
    carried_management = _previous_management(previous) if not management else None
    # One budget for the whole listing phase (LIST_BUDGET_SECONDS): the management project
    # lists first, at the start of it, so its listing, the one that decides whether the
    # roster is reconciled, is never cut short by a slow container; then the containers,
    # one Asset Inventory call each; then the explicit projects. Container members arrive
    # with their clusters, so no per-project listing follows for them (design §4).
    listing_deadline = time.monotonic() + LIST_BUDGET_SECONDS
    listings: dict[str, tuple[list | None, str]] = {}
    if management:
        listings[management] = _list_project(management)
    searches = _search_containers(_container_ids(scope), listing_deadline)
    entries, ignored_excludes, containers = _resolve_projects(management or carried_management, scope, searches, previous)
    report["containers"] = [dict(c) for c in containers]
    if carried_management:
        for entry in entries:
            # Declared explicitly too: the declaration vouches for it, so it lists as any
            # explicit project does. Only the bare management slot is left unlisted.
            if entry["id"] == carried_management and entry["via"] == [VIA_MANAGEMENT]:
                entry["outcome"] = OUTCOME_UNREACHABLE
    resolved_ids = {e["id"] for e in entries}
    if not management:
        log("could not resolve the management project — its clusters are not reconciled this run.")
    elif os.environ.get("RECONCILE_PROJECT"):
        log("RECONCILE_PROJECT overrides the management project; the operator pins it empty in the "
            "managed .env, so this install is running without that pin. Name the project in "
            "spec.scope.projects instead; the variable retires with RECONCILE_EXCLUDE.")

    # --- CREATE: ensure every cluster in every listable project (except exclusions) has a
    #     profile. Requires only a resolvable project now that the management cluster is
    #     managed like any other — the metadata-server self-identification this used to
    #     gate on existed solely to recognise the cluster being skipped.
    cluster_counts: dict[str, int | None] = {}
    to_list = [e["id"] for e in entries if e["outcome"] is None and e["id"] not in listings]
    listings.update(_list_projects(to_list, listing_deadline))
    for entry in entries:
        # A container member: Asset Inventory named its clusters already.
        if "clusters" in entry and entry["id"] not in listings:
            listings[entry["id"]] = (entry["clusters"], OUTCOME_OK)
    for entry in entries:
        project = entry["id"]
        if project not in listings:
            # Not listed this run: over-cap is decided, a frozen container's member carries
            # its container's outcome, and unreachable is the carried-forward management
            # project; nothing is created under any of them this tick.
            cluster_counts[project] = None
            continue
        listed, outcome = listings[project]
        entry["outcome"] = outcome
        if listed is None:
            cluster_counts[project] = None
            continue
        cluster_counts[project] = len(listed)
        # The roster is reconciled only once the management project itself has listed:
        # a run that could not name it, or could not read it, hands the gate a roster
        # missing the one cluster every install has, which is what exit 3 exists to
        # prevent. Explicit projects listing on their own do not count.
        if project == management:
            report["create_pass_ran"] = True
        # A container member arrives from the asset index without any per-project
        # permission check, so a project the account holds no GKE role in would otherwise
        # be scaffolded (registered with Hermes, stamped, pushed to the sandbox) and fail
        # only at get-credentials, every run. One describe per cluster before its create
        # answers cheaply: a NotFound skips the cluster the index still names, a 403 skips
        # the project's remaining creates and reads it `denied` (design §4).
        member_only = not ({VIA_MANAGEMENT, VIA_EXPLICIT} & set(entry["via"]))
        for (proj, cluster, location) in sorted(listed):
            if (proj, cluster, location) in existing_keys:
                continue
            if (proj, cluster, location) in excluded_triples:
                continue
            if cluster in EXTRA_EXCLUDE:
                log(f"{cluster} ({proj}/{location}) is skipped by RECONCILE_EXCLUDE, a bare name; "
                    "move it to spec.scope.exclude.clusters, the variable retires next release.")
                continue
            if member_only:
                # Per cluster: the index lags real state by minutes, so a member's cluster the
                # index still names may be gone, and the first cluster answering says nothing
                # about the second. A 403 seen earlier in the project, or on this describe,
                # skips the create. On a dry run too: the preview has to name the creates and
                # outcomes the real run will produce, and PRUNE describes on a dry run already.
                revised = proj in _denied_this_run or proj in _api_disabled_this_run
                exists = None if revised else _cluster_exists(proj, cluster, location)
                if exists is False:
                    log(f"{cluster} ({proj}/{location}) is in the asset index but describe says it is gone "
                        "(deleted, or the index is behind); no profile made for it this run.")
                    continue
                if proj in _denied_this_run:
                    log(f"{cluster} ({proj}/{location}) has no profile and the project answered 403; "
                        f"no CREATE under it this run ({OUTCOME_DENIED}).")
                    continue
                if proj in _api_disabled_this_run:
                    log(f"{cluster} ({proj}/{location}) has no profile and the project's GKE API is disabled; "
                        f"no CREATE under it this run ({OUTCOME_API_DISABLED}).")
                    continue
            if dry_run:
                log(f"{cluster} ({proj}/{location}) has no profile — WOULD create (dry-run).")
                report["created"].append(f"{cluster}/{location}")
                continue
            try:
                name = create_profile(proj, cluster, location)
                log(f"created profile {name} for {cluster} ({proj}/{location}).")
                report["created"].append(name)
            except (SystemExit, Exception) as e:  # noqa: BLE001 - one failure never aborts the sweep
                # A container-only member's get-credentials 403 revises the project (design §4);
                # an explicit or management project's own listing already decided its outcome,
                # and a local "Permission denied" is not an IAM answer, so neither counts here.
                # The API-disabled answer is itself a 403, so it is classified first, as
                # `_classify_list_failure` does, and reads `api-disabled` rather than `denied`.
                revision = None
                if member_only and any(m in str(e) for m in _API_DISABLED_MARKERS):
                    revision, bucket = OUTCOME_API_DISABLED, _api_disabled_this_run
                elif member_only and any(m in str(e) for m in _CREATE_DENIED_MARKERS):
                    revision, bucket = OUTCOME_DENIED, _denied_this_run
                if revision:
                    bucket.add(proj)
                    # The failure came from get-credentials, after the profile was registered
                    # and its layout pushed: roll that back, or the next run finds a home it
                    # reads as incomplete and scaffolds it again, every hour, for as long as
                    # the grant is missing. Only a home this run made: one that was here
                    # before (an incomplete re-run, an unreadable identity) is PRUNE's to
                    # keep, whatever it holds, and stays.
                    half = profile_name(proj, cluster, location)
                    if half in preexisting_homes:
                        log(f"{half} predates this run and is left in place after its get-credentials failed ({revision}).")
                    else:
                        try:
                            delete_profile(half)
                            log(f"rolled back the half-built profile {half}: the project answered {revision} on get-credentials.")
                        except (SystemExit, Exception) as rollback_error:  # noqa: BLE001 - best effort; the sweep goes on
                            log(f"could not roll back the half-built profile {half}: {rollback_error}")
                log(f"create for {cluster} ({proj}/{location}) failed (left unmanaged): {e}")
                report["create_failed"].append(f"{cluster}/{location}")
    for entry in entries:
        report["projects"][entry["id"]] = entry["outcome"]

    # A project the scope has dropped is pruned only under three conditions (design §7):
    # no selector produced it this run, every listed project resolved without an error
    # that could hide a project (no explicit project unreachable, every container `ok` or
    # `over-cap`), and the previous snapshot had it in scope. The
    # third protects every profile the scope never produced, above all the ones
    # onboarded by hand before a scope existed. On top of that the prune takes two clean
    # runs: the first clean run a project is absent marks it retiring, the next clean run
    # that still finds it absent deletes; an unclean run in between carries it forward
    # without marking or counting. That second run is what stands between a
    # declaration edit and its profiles: one reverted before it costs nothing. (A vanished
    # declaration is the unreadable-file case below, not this rule's.)
    #
    # Three more things switch the scope prune off for the run, because each makes
    # "absent from the resolved set" a lookup failure rather than the declaration
    # speaking: the management project could not be resolved (its previous identity is
    # carried forward above, and nothing under it may be judged this tick), it resolved but
    # could not list its own clusters, and the declaration file could not be read (an empty
    # fallback scope is not a declared one).
    # A fourth: the management project's identity changed since the last run (RECONCILE_PROJECT
    # removed or re-pointed, or the metadata server naming another project; a fallback answer
    # that disagrees was already discarded above).
    # The old one then reads as dropped on a run that cannot vouch for the change, so it is
    # retired rather than pruned, and the next clean run decides.
    previous_management = _previous_management(previous)
    management_changed = bool(previous_management and management and previous_management != management)
    # The new identity counts only once it has listed its own clusters: a RECONCILE_PROJECT
    # typo answers `denied` every tick, and two such ticks must not read as the old project
    # confirmed gone. Until then the old project is carried forward, not retired.
    management_listed = report["create_pass_ran"]
    if management_changed and management_listed:
        log(f"management project changed from {previous_management} to {management}; "
            "the scope prune is skipped this run and the old project's profiles are kept, retiring: "
            "the next clean run prunes them unless the old project is named in spec.scope.projects.")
    elif management_changed:
        log(f"management project changed from {previous_management} to {management}, which did not "
            "list its clusters; the old project is carried forward and nothing is judged this run.")
    lookups_clean = (management is not None and management_listed and scope_readable
                     and not management_changed
                     and all(e["outcome"] != OUTCOME_UNREACHABLE for e in entries)
                     and all(c["outcome"] in (OUTCOME_OK, OUTCOME_OVER_CAP) for c in containers))
    if not lookups_clean and not management_changed:
        why = ("the management project did not list its own clusters" if management and not management_listed
               else "a lookup or the declaration could not be trusted")
        log(f"scope prune skipped this run: {why}.")
    declared_containers = set(_container_ids(scope))
    # A container this run declares that the previous snapshot did not: the one-edit
    # migration from `projects` to a folder, whose members the index may not place yet.
    previous_known_containers = _previous_container_ids(previous)
    newly_declared_containers = bool(declared_containers - previous_known_containers)
    exclude_patterns = scope["exclude"]["projects"]

    now = datetime.now(timezone.utc)
    absent_since: dict[str, str] = {}

    def index_dropped(project: str) -> bool:
        """A project reached only through containers that the index no longer places under one.

        The asset index lags a move by minutes to hours, and a project moved between two
        declared folders vanishes from both meanwhile; the declaration did not change, so the
        scope rule must not retire it yet. It is kept for INDEX_LAG_GRACE_SECONDS from the
        first run that found it absent (`absentSince` on its row), then the ordinary two-run
        retire applies, which is what retires a project that was deleted or moved under a
        parent the CR does not declare. It retires sooner when the declaration speaks: the
        container removed from the CR, or an `exclude.projects` entry naming it. A render
        that predates containers (a rollback to the previous release) declares nothing about
        them, and keeps their members without a clock.

        A project that was explicit alone last run, or whose previous containers have all
        left the CR, is the declaration's to decide, with one exception: dropped from
        `projects`, or from a folder the same edit removes, in the edit that declares the
        folder it moved into, the index may not place it under that folder yet, and its
        previous `via` names no declared container to keep it by. A container the previous
        snapshot did not carry is what marks that edit, and the same day's clock applies; a
        stamp already on the row keeps counting on the runs after, when the container is no
        longer new. A container removed by this edit with nothing new declared is the
        declaration speaking, stamp or no stamp: the row retires.
        """
        # A project already retiring was dropped by the declaration; the index rule is for
        # members the declaration still reaches, and an unclean run carries a retiring
        # project as retiring (below), never back into scope.
        if project in previously_retiring:
            return False
        via = _previous_via(previous, project)
        container_vias = [v for v in via if v.split("/")[0] in CONTAINER_KINDS]
        if VIA_MANAGEMENT in via:
            return False
        if _excluded_by(project, exclude_patterns):
            return False
        if not container_vias:
            if not (containers_known and (newly_declared_containers or _previous_absent_since(previous, project))):
                return False
        elif not containers_known:
            return True
        elif not any(v in declared_containers for v in container_vias):
            if newly_declared_containers:
                pass  # the one-edit move: held from this run, whatever the row carried
            elif any(v in previous_known_containers for v in container_vias):
                return False  # removed by this edit, nothing declared in its place: retires
            elif not _previous_absent_since(previous, project):
                return False
            # else: a hold that began on an earlier run, still counting
        since = _previous_absent_since(previous, project) or now.strftime(SNAPSHOT_TIME_FORMAT)
        try:
            first_absent = datetime.strptime(since, SNAPSHOT_TIME_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError:
            # A stamp this run cannot read restarts the clock rather than keeping for ever.
            first_absent, since = now, now.strftime(SNAPSHOT_TIME_FORMAT)
        absent_since[project] = since
        return (now - first_absent).total_seconds() < INDEX_LAG_GRACE_SECONDS

    retiring: dict[str, list[str]] = {}
    deferred_retiring: set[str] = set()
    carried_in_scope: set[str] = set()
    previously_retiring = _previously_retiring(previous)
    unmanaged: list[dict] = []

    # --- PRUNE: remove profiles whose cluster is gone, whose cluster is excluded, or whose
    #     project the scope has dropped.
    log(f"Reconciling {len(profiles)} managed profile(s){' (dry-run)' if dry_run else ''}.")
    previous_attribution = _previous_attribution(previous)
    unattributed_counts: dict[str, int] = {}
    for name in profiles:
        identity = identities[name]
        if identity is None:
            log(f"{name}: no readable cluster_identity — skipping (never delete unverifiable profiles).")
            report["skipped_no_identity"].append(name)
            if name in previous_attribution:
                pid = previous_attribution[name]
                unattributed_counts[pid] = unattributed_counts.get(pid, 0) + 1
            continue
        triple = (identity["project"], identity["cluster"], identity["location"])

        # Policy prune: an excluded cluster must not carry a profile, so adding a name to
        # RECONCILE_EXCLUDE, or a triple to exclude.clusters, removes the profile it already
        # has rather than merely stopping a new one being made.
        why = None
        if triple in excluded_triples:
            why = "spec.scope.exclude.clusters"
        elif identity["cluster"] in EXTRA_EXCLUDE:
            why = "RECONCILE_EXCLUDE"
            log(f"{name}: excluded by RECONCILE_EXCLUDE, a bare name across every project; "
                "move it to spec.scope.exclude.clusters, the variable retires next release.")
        if why:
            if dry_run:
                log(f"{name}: {identity['cluster']} is in {why} — WOULD prune (dry-run).")
            else:
                log(f"{name}: {identity['cluster']} is in {why} — pruning.")
                delete_profile(name)
            report["pruned"].append(name)
            continue

        # Scope prune: the project left the scope (three conditions above, two runs).
        project = identity["project"]
        if project not in resolved_ids:
            if lookups_clean and project in previously_retiring:
                retiring.setdefault(project, []).append(name)
                if dry_run:
                    log(f"{name}: project {project} left the scope — WOULD prune (dry-run).")
                else:
                    log(f"{name}: project {project} left the scope — pruning.")
                    delete_profile(name)
                report["pruned"].append(name)
                continue
            if management_changed and management_listed and project == previous_management:
                # Retiring like any dropped project, so the ordinary rule takes over next run
                # rather than the old project vanishing from the snapshot as never in scope.
                deferred_retiring.add(project)
                reason = "was the management project until this run; retiring, pruned on the next clean run"
            elif project in previously_resolved and index_dropped(project):
                # Reached only through a container that is still declared, and absent from the
                # asset index this run: the index dropped it, not the declaration. Kept, listed,
                # and back in scope the run the index places it again.
                carried_in_scope.add(project)
                reason = (f"not under any declared container in the asset index this run (a move, or the "
                          f"index behind); kept until {INDEX_LAG_GRACE_SECONDS // SECONDS_PER_HOUR}h after {absent_since.get(project, '')}"
                          if containers_known
                          else "reached through a container the running render does not know; kept" if scope_readable
                          else "not judged this run: the declaration could not be read; carried forward")
            elif lookups_clean and scope_present and project in previously_resolved:
                # First clean run the declaration omits it: retiring now, pruned next run.
                deferred_retiring.add(project)
                reason = "left the scope this run; retiring, pruned on the next clean run"
            elif project in previously_retiring:
                # Already retiring: stays so. An unclean or unreadable tick neither prunes
                # nor restarts the two-run count.
                deferred_retiring.add(project)
                reason = "retiring; waiting for a clean run"
            elif project in previously_resolved:
                # Absent on a run that could not trust its lookups, could not read the
                # declaration, or found no scope block on the CR: not judged, carried forward
                # in scope so the first clean run under a present block is the one that marks
                # it retiring (design §7). Nothing is forgotten, nothing counted.
                carried_in_scope.add(project)
                reason = ("no scope block declared this run; carried forward" if lookups_clean and not scope_present
                          else "not judged this run: a lookup or the declaration could not be trusted; carried forward")
            else:
                reason = "never in scope"
            # Listed as unmanaged below, once its own cluster is known to exist or the lookup
            # was inconclusive: a profile whose cluster is gone is pruned, and a pruned profile
            # is not on the volume for the snapshot to list.
            unmanaged_reason = reason
        else:
            unmanaged_reason = None

        exists = _cluster_exists(**identity)
        if exists is not False and unmanaged_reason:
            unmanaged.append({"profile": name, "project": project, "reason": unmanaged_reason})
            report["unmanaged"].append(name)
        if exists is True:
            report["kept"].append(name)
            continue
        if exists is None:
            report["skipped_error"].append(name)
            continue

        # exists is False -> definitive NotFound -> orphan.
        if dry_run:
            log(f"{name}: cluster {identity['cluster']} ({identity['project']}/{identity['location']}) "
                f"is gone — WOULD prune (dry-run).")
        else:
            log(f"{name}: cluster {identity['cluster']} ({identity['project']}/{identity['location']}) "
                f"is gone — pruning.")
            delete_profile(name)
        report["pruned"].append(name)

    # A project absent from the resolved set whose only profiles were unreadable this run
    # was judged by nothing above; it is judged here by attribution, the same way a
    # readable one would have been, so a drop that coincides with an unreadable identity
    # still starts (or carries) the two-run count rather than falling out of the snapshot.
    for pid in set(unattributed_counts) & previously_resolved - resolved_ids - previously_retiring:
        if pid in deferred_retiring or pid in carried_in_scope:
            continue
        old_management = management_changed and management_listed and pid == previous_management
        (deferred_retiring if ((lookups_clean and scope_present and not index_dropped(pid)) or old_management)
         else carried_in_scope).add(pid)

    # A retiring project stays in the snapshot, eligible for the prune, until every one of
    # its profiles is gone; otherwise a delete that failed on the one tick the third
    # condition held would leave the profile unmanaged for good (design §7).
    # Retiring until every profile is gone, and not a tick longer: a project whose last
    # profile went this run leaves the snapshot with it. A profile whose identity could not
    # be read this run counts for the project the last snapshot attributed it to, and for
    # no other, so it keeps its own project retiring and pins nothing else.
    pruned_now = report["pruned"] if not dry_run else []

    def remaining(pid: str) -> int:
        return remaining_profiles(pid, identities, pruned_now) + unattributed_counts.get(pid, 0)

    still_retiring = {
        pid for pid in (set(retiring) | deferred_retiring | (previously_retiring - resolved_ids))
        if remaining(pid)
    }
    report["retiring"] = sorted(still_retiring)

    # A container member whose describe or get-credentials answered 403 reads `denied`:
    # an IAM deny on a member project blocks the inherited grant without hiding the
    # cluster from the asset index, and these calls are the only ones that see it.
    for entry in entries:
        if entry["outcome"] != OUTCOME_OK or {VIA_MANAGEMENT, VIA_EXPLICIT} & set(entry["via"]):
            continue
        for bucket, revised_to, why in ((_api_disabled_this_run, OUTCOME_API_DISABLED, "answered API disabled"),
                                        (_denied_this_run, OUTCOME_DENIED, "answered 403")):
            if entry["id"] in bucket:
                entry["outcome"] = revised_to
                report["projects"][entry["id"]] = revised_to
                log(f"{entry['id']} (via {', '.join(entry['via'])}) {why} on a per-cluster call; "
                    f"its outcome is {revised_to} this run.")
                break

    # Fill order decided the cap above; the written order is sorted by ID so an
    # unchanged fleet writes an unchanged file (design §3, "Resolution is deterministic").
    snapshot_projects = sorted([
        # Only a member a frozen container carried, and nothing else placed this run, keeps its
        # index-lag stamp (`indexed` is False on exactly those rows); a project the index
        # placed, or that lists on its own as explicit or management, clears it.
        {"id": e["id"], "via": e["via"], "outcome": e["outcome"], "state": STATE_IN_SCOPE,
         "clusters": cluster_counts.get(e["id"]),
         **({ABSENT_SINCE_KEY: _previous_absent_since(previous, e["id"])}
            if e.get("indexed") is False and _previous_absent_since(previous, e["id"]) else {})}
        for e in entries
    ] + [
        # Carried with the via it had, so a container frozen on a later run still finds the
        # members an unjudged run carried, and the gate still sees what produced them; and
        # with the index-lag stamp it had, so a frozen or unjudged run in between does not
        # restart the day.
        # Without the `management` marker: that marker is the management slot's alone, and a
        # carried old management project must not answer _previous_management next run.
        {"id": pid, "via": [v for v in _previous_via(previous, pid) if v != VIA_MANAGEMENT], "outcome": OUTCOME_UNREACHABLE,
         "state": STATE_IN_SCOPE, "clusters": remaining(pid),
         **({ABSENT_SINCE_KEY: absent_since.get(pid) or _previous_absent_since(previous, pid)}
            if (pid in absent_since or _previous_absent_since(previous, pid)) else {})}
        for pid in sorted(carried_in_scope - resolved_ids)
    ] + [
        {"id": pid, "via": [], "outcome": OUTCOME_OK, "state": STATE_RETIRING,
         "clusters": remaining(pid)}
        for pid in sorted(still_retiring - carried_in_scope)
    ], key=lambda p: p["id"])
    if not dry_run:
        _write_snapshot({
            "resolvedAt": datetime.now(timezone.utc).strftime(SNAPSHOT_TIME_FORMAT),
            "declared": declared,
            "resolver": RESOLVER_ASSET_INVENTORY if _container_ids(scope) else RESOLVER_EXPLICIT,
            "containers": sorted(containers, key=lambda c: c["id"]),
            # Every profile by project: as read this run, or as last read for one whose
            # identity could not be read this run and whose home is still on the volume, so
            # the attribution survives any number of unreadable runs. A pruned name is dropped
            # once its home is gone, and kept while a failed delete leaves it on the volume.
            "profiles": dict(sorted((
                {n: previous_attribution[n] for n in profiles
                 if identities.get(n) is None and n in previous_attribution and profile_home(n).exists()}
                | {n: i["project"] for n, i in identities.items()
                   if i and (n not in set(report["pruned"]) or profile_home(n).exists())}
            ).items())),
            "projects": snapshot_projects,
            "unmanaged": sorted(unmanaged, key=lambda u: u["profile"]),
            "ignoredExcludes": ignored_excludes,
        })

    return report


def _format_notification(report: dict) -> str:
    created = report.get("created", [])
    pruned = report.get("pruned", [])
    lines = ["🔧 *Cluster Agent reconcile*"]
    if created:
        lines.append(f"  ➕ created {len(created)} profile(s): "
                     + ", ".join(f"`{n}`" for n in created))
    if pruned:
        lines.append(f"  🧹 pruned {len(pruned)} profile(s):")
        for name in pruned:
            lines.append(f"     • `{name}` (cluster gone, excluded, or its project left the scope)")
    failed = report.get("create_failed", [])
    if failed:
        lines.append(
            f"  ❌ {len(failed)} cluster(s) could not be given a profile "
            f"(retried next run): {', '.join(f'`{n}`' for n in failed)}."
        )
    if report.get("skipped_error"):
        lines.append(
            f"  ⚠️ {len(report['skipped_error'])} profile(s) could not be verified this run "
            f"(left untouched): {', '.join(f'`{n}`' for n in report['skipped_error'])}."
        )
    # Containers first, one line each: a container that failed or read over-cap stands
    # for every member it carried, which is what keeps the next line short. The two are
    # said apart: a failed lookup points at IAM or the Asset API, an over-cap one (the
    # lookup succeeded, the members would cross the cap) at the declaration.
    containers = sorted((report.get("containers") or []), key=lambda c: c["id"])
    failed_containers = [c for c in containers if c.get("outcome") not in (OUTCOME_OK, OUTCOME_OVER_CAP)]
    over_cap_containers = [c for c in containers if c.get("outcome") == OUTCOME_OVER_CAP]
    if failed_containers:
        lines.append(
            f"  ⚠️ {len(failed_containers)} folder(s)/organisation(s) could not be resolved (members carried, profiles kept): "
            + ", ".join(f"`{c['id']}` ({c['outcome']}, {c.get('projects', 0)} project(s))" for c in failed_containers) + "."
        )
    if over_cap_containers:
        lines.append(
            f"  ⚠️ {len(over_cap_containers)} folder(s)/organisation(s) resolved past the listing cap of {RESOLVED_SET_CAP} "
            "(members carried over-cap, profiles kept, nothing created): "
            + ", ".join(f"`{c['id']}` ({c.get('projects', 0)} project(s))" for c in over_cap_containers)
            + ". Narrow it with exclude.projects or declare the sub-folders that hold the clusters."
        )
    unlisted = sorted((p, o) for p, o in (report.get("projects") or {}).items() if o != OUTCOME_OK)
    if unlisted:
        named = ", ".join(f"`{p}` ({o})" for p, o in unlisted[:NOTIFY_UNLISTED_LIMIT])
        rest = len(unlisted) - NOTIFY_UNLISTED_LIMIT
        lines.append(
            f"  ⚠️ {len(unlisted)} project(s) in scope could not be listed (profiles kept): {named}"
            + (f", and {rest} more (see fleet_scope.json)." if rest > 0 else ".")
        )
    return "\n".join(lines)


def _notify(message: str) -> None:
    """Post a summary to each configured chat platform's home channel (best-effort).

    Stays in the agent pod. `hermes` is not cluster tooling: it needs the
    profiles on the data PVC and the gateway on loopback, neither of which the
    sandbox has, and the sandbox image does not carry the binary.

    The target used to be the literal `google_chat`, which meant a Slack-only
    install never heard that a Cluster Agent profile had been created or pruned:
    the send failed on the missing Google Chat home channel and the `except`
    below turned it into one line of stderr on a run that still exits 0. #989.

    Each platform is sent to independently — a Google Chat outage must not cost
    Slack the summary, and the reverse.
    """
    for platform in enabled_chat_platforms():
        try:
            subprocess.run(
                [HERMES_BIN, "send", "--to", platform, message],
                capture_output=True, text=True, check=True, timeout=30, env=_run_env(),
            )
        except Exception as e:  # noqa: BLE001 - notification is best-effort; never fail the run
            log(f"Failed to post reconcile notification to {platform}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile Cluster Agent profiles with the GKE clusters in scope "
                    "(create for every cluster not excluded; prune orphans and dropped projects)."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be created/pruned without changing anything or notifying.",
    )
    parser.add_argument(
        "--require-create-pass", action="store_true",
        help="Exit non-zero if the CREATE direction could not run (no management project, or "
             "its cluster list failed), if it ran and every create failed, if the run raised, or "
             "if another run holds the lock (4). Off by default: "
             "the cron producer must always exit 0.",
    )
    args = parser.parse_args()

    with _exclusive_run() as acquired:
        if not acquired:
            log("another reconcile is already running; leaving the roster to it.")
            # The cron producer still exits 0 — an overlap is expected, not an error.
            # Only the caller that asked to be told about the roster hears about it,
            # and what it hears is "ask again", not "reconcile failed".
            if args.require_create_pass:
                raise SystemExit(EXIT_ALREADY_RUNNING)
            return

        try:
            report = reconcile(dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001 - resilient: a cron producer must always exit 0
            log(f"Reconcile aborted unexpectedly: {e}")
            if args.require_create_pass:
                raise SystemExit(EXIT_CREATE_PASS_SKIPPED)
            return

    log(
        "Done: created={} failed={} pruned={} kept={} no_identity={} unknown={}.".format(
            len(report.get("created", [])), len(report.get("create_failed", [])),
            len(report.get("pruned", [])),
            len(report.get("kept", [])), len(report.get("skipped_no_identity", [])),
            len(report.get("skipped_error", [])),
        )
    )

    # PRUNE walks every profile including the half-built ones: their cluster exists, so
    # they land in `kept` alongside the healthy homes. Counting them as "already in
    # place" would let a run whose only create failed report a reconciled roster from
    # the second tick onward, the tick where the failure's own wreckage is on disk.
    incomplete = set(report.get("incomplete", []))
    kept_scaffolded = [n for n in report.get("kept", []) if n not in incomplete]

    unreconciled = None
    if not report.get("create_pass_ran"):
        unreconciled = "CREATE direction did not run"
    elif report.get("create_failed") and not (report.get("created") or kept_scaffolded):
        # Every create failed and nothing was already in place, so the roster is empty
        # apart from the half-built homes those failures left behind — `create_profile`
        # stamps the identity before it fetches credentials. The caller that gates a
        # one-shot fan-out on this exit code must not file against that; a retry either
        # succeeds or repairs the home on the next run.
        #
        # A partial failure is deliberately not reported here. One unscaffoldable cluster
        # among several costs the sweep one `gaps` row — the audit SOP's preflight branch
        # catches the missing kubeconfig — and holding the whole report back for it buys
        # nothing when the cause is permanent (no IAM, a private control plane).
        unreconciled = "every CREATE failed: " + ", ".join(report["create_failed"])

    if args.dry_run:
        print(json.dumps(report, indent=2))
    else:
        # Notify only when there's something actionable to report (avoid idle hourly
        # noise). A failed create rides along on a run that already has something to say
        # rather than triggering its own message: it repeats every run until the cause is
        # fixed, and the gate re-runs this script every minute during onboarding.
        if report.get("created") or report.get("pruned"):
            _notify(_format_notification(report))

    if args.require_create_pass and unreconciled:
        log(f"{unreconciled}; the roster is not reconciled.")
        raise SystemExit(EXIT_CREATE_PASS_SKIPPED)


if __name__ == "__main__":
    main()
