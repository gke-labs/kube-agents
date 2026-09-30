#!/usr/bin/env python3
"""patch_readiness.py — Procedural collector for the Upgrade & Patch
Readiness Audit (`security-patch-orchestrator`).

See docs/designs/fleet-audit-collector-manifest.md for the manifest this
emits and governance/security_patch_orchestrator_sop.md for the checks.

This stream's own collector, like `fleet_drift.py`: it reads only GKE
control-plane and node-pool *metadata* through `gcloud container`, and needs
no kubeconfig at all. Its collection is also flatter than any
`kubectl`-based stream's: one
`clusters list` call per project already returns every cluster's full
resource, node pools included (the SOP's own §1, point 2), so eight of the
ten checks below read data already in memory — the one per-pool
`node-pools describe` the SOP's own §3 command lines show is never actually
issued, because `clusters list` already carries every field those describes
would return. Only the version-currency checks (`master-behind`,
`stale-image-type`) need a second call, `get-server-config`, and that one is
cached per distinct `(project, location)` pair, not re-issued per cluster —
the SOP's own §2 instruction.

Two GCP surfaces, so two manifest failure shapes:

- A project's `clusters list` failing means none of its clusters are known
  at all. The project gets one `gate-failed` `project/<id>` entry saying so,
  because a project that leaves no trace in the manifest reads exactly like
  one holding no clusters, and the document is then free to publish a
  fleet-wide verdict over clusters nobody enumerated.
- A location's `get-server-config` failing means its clusters are still
  fully collected for every check that does not need a baseline; the
  manifest's `commands` for that cluster simply has no entry for
  `master-behind`/`stale-image-type`, and the SOP tells the agent to write
  a `limitations` note naming those two rather than treating the cluster as
  unreachable. `cross_check_manifest` never objects to a `checks_run` that
  is a subset of a `"collected"` cluster's `commands` — only to one that
  claims more than the manifest backs.

A check absent from `commands` is therefore always a gap. No GKE cluster
shape rules any of the ten checks out -- Autopilot node pools carry every
field the node-pool checks read -- so this collector never writes
`checks_not_applicable`; see the comment above `collect_one_cluster`.

Discovery follows `fleet_drift.py`'s: every project the credential can list
is in scope, a project whose Kubernetes Engine API is off reads as empty, and
a scope that is short for any reason -- `projects list` failed or filtered,
or `--project` narrowed it on purpose -- is a `gate-failed`
`project/UNENUMERATED_PROJECTS` row rather than a quiet fleet of fewer
projects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple

MANIFEST_VERSION = 1

# The location a project-level target carries: it has no region of its own.
GLOBAL_LOCATION = "global"

# GKE supports node pools at most this many minors behind the control plane;
# at it the next control-plane upgrade is blocked, past it the pool is unsupported.
SKEW_CEILING_MINORS = 2
# §3.3: a fleet flags once its control planes span this many minors.
FLEET_SPREAD_MIN_MINORS = 2

# A digest of this file, published as `checks_revision`. The manifest contract
# (docs/designs/fleet-audit-collector-manifest.md §2) carries it unread today,
# reserved for the run-over-run comparison that tells a finding that stopped
# reproducing from a check that stopped looking. Long enough that two collector
# sources will not collide, short enough to read in a log line, and the same
# width in every collector: a file that truncated differently would report a
# moved collector on the run that changed it.
REVISION_DIGEST_CHARS = 12
CHECKS_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[
    :REVISION_DIGEST_CHARS
]

AUDIT_ID = "security-patch-orchestrator"
DEFAULT_TIMEOUT_S = 60
# What `timeout(1)` exits with, so a timed-out gcloud reads the same in the
# manifest whichever layer cut it off.
TIMEOUT_RC = 124
MAX_WORKERS = 8
# How much of gcloud's stderr a `gate-failed` entry keeps: enough to carry the
# API's error sentence, short enough not to dominate the manifest.
ERROR_EXCERPT_CHARS = 300
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# How many cluster names the closing summary spells out per check before it
# counts the rest.
SUMMARY_MAX_CLUSTER_NAMES = 5

# The manifest contract's outcomes and the target name for a project whose
# `clusters list` failed.
OUTCOME_COLLECTED = "collected"
OUTCOME_GATE_FAILED = "gate-failed"
OUTCOME_OUT_OF_SCOPE = "out-of-scope"
PROJECT_TARGET_PREFIX = "project/"
# The target standing for the projects discovery never named. Uppercase
# because a GCP project id cannot be, so no real project collides with it.
UNENUMERATED_PROJECTS_TARGET = PROJECT_TARGET_PREFIX + "UNENUMERATED_PROJECTS"
# `--project` skips discovery, so a scoped run names no other project. Without
# a row saying so the manifest reads as a fleet of one project, and `finish`
# resolves every ledger finding on a cluster the run never looked at.
SCOPED_RUN_NOTE = (
    "scope narrowed to project {project!r} by `--project`: discovery was skipped, so no other "
    "project in this fleet was named or read, and this run cannot speak for their clusters."
)
# gcloud's words for a project whose Kubernetes Engine API is off. Such a
# project cannot hold a GKE cluster, so its failed `clusters list` is an empty
# project rather than a lost one -- otherwise every non-GKE project the
# credential can see is a coverage gap on every run. Permission denied is not
# here on purpose: a project this credential may not list may hold clusters.
# `fleet_drift.py` matches the same three forms for the same reason.
API_DISABLED_MARKERS = ("SERVICE_DISABLED", "accessNotConfigured", "has not been used in project")
# gcloud's words for a `clusters list` some zones did not answer. It exits 0
# with the clusters that did come back, so without this the missing ones read
# as clusters that do not exist.
ZONE_TIMEOUT_MARKER = "did not respond"

# Node images no longer serviced: the pre-containerd runtimes and Windows's
# retired servicing channel. See §3.9's table for the replacement each maps to.
DEPRECATED_IMAGE_TYPES = {"COS": "COS_CONTAINERD", "UBUNTU": "UBUNTU_CONTAINERD", "WINDOWS_SAC": "WINDOWS_LTSC_CONTAINERD"}
BLOCKING_EXCLUSION_SCOPES = {"NO_UPGRADES", "NO_MINOR_OR_NODE_UPGRADES"}
# A scope-less exclusion is a full freeze: `NO_UPGRADES` is the scope enum's zero
# value, and GKE's JSON omits a field at its zero value.
DEFAULT_EXCLUSION_SCOPE = "NO_UPGRADES"
# §3.8's "ends more than 30 days from now": time left, not the freeze's length.
LONG_FREEZE = timedelta(days=30)
# §3.8's escalation: a freeze beside a critical or major version finding.
# The severities the SOP grades in, as `finish` spells them.
CRITICAL, MAJOR, MINOR = "critical", "major", "minor"
VERSION_FINDING_SEVERITIES = {CRITICAL, MAJOR}
# §3.2's staged-rollout allowance: GKE upgrades the control plane first and
# drains pools after, so a pool this many patches behind (or on the same patch
# at an older build) is a rollout in flight rather than skew.
MAX_ROLLOUT_PATCH_LAG = 1
STATUS_RECONCILING = "RECONCILING"
STATUS_PROVISIONING = "PROVISIONING"
# GKE's second spelling of "no release channel".
UNSPECIFIED_CHANNEL = "UNSPECIFIED"
UPGRADE_AVAILABLE_EVENT = "UPGRADE_AVAILABLE_EVENT"
# Opens the excerpt of a `no-notifications` hit on a cluster that already
# publishes to a topic of its own and filters the upgrade event out.
FILTER_EXCLUDES_PREFIX = "pubsub filter excludes"
# Joins `<project>/<location>/<name>`, the manifest's name for a cluster.
QUALIFIED_TARGET_SEPARATOR = "/"

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-gke\.(\d+))?$")

# §1.5's skip list, verbatim. A cluster in one of these states is not a
# cluster this audit has an opinion about: mid-flight or broken means its
# version data is meaningless, and an alpha cluster cannot be upgraded and
# expires on its own. The SOP leaves both out of either scope list.
_UNAUDITABLE_STATUSES = {
    "PROVISIONING": "cluster status is PROVISIONING; version data is not yet meaningful",
    "STOPPING": "cluster status is STOPPING; the object is mid-delete",
    "ERROR": "cluster status is ERROR; the object is broken and its reported version is not trustworthy",
}

# Default severity per check, mode-independent. A hit's own "severity" key
# overrides this for the checks whose rule forks on which condition fired
# (master-behind, pool-skew, blocking-exclusion) -- everything else here is
# one severity always, so the default is the whole rule.
SEVERITY = {
    "master-behind": CRITICAL,  # always overridden per hit
    "pool-skew": CRITICAL,  # always overridden per hit
    "fleet-spread": MINOR,
    "no-channel": MAJOR,
    "no-autoupgrade": MAJOR,
    "no-autorepair": MINOR,
    "no-maintenance-window": MINOR,
    "blocking-exclusion": MINOR,  # overridden to major per hit when it holds back a version finding
    "stale-image-type": MAJOR,
    "no-notifications": MINOR,
}

# `master-behind`'s only severity whose impact is a different claim rather than a
# stronger one: (a) says nothing patches this control plane, (b) and (c) say it is
# behind while still being patched. `IMPACT` is per-check, so the branch that
# earns this one carries it on the hit and `_emit` prefers it.
ARM_SPECIFIC_IMPACT_CHECKS = {"master-behind"}
BEHIND_MASTER_IMPACT = "Control plane is behind its release channel's default {default} and carries whatever the intervening builds fixed until it moves; GKE is still patching it."
UNSUPPORTED_MASTER_IMPACT = "Control plane runs a version no channel at this location offers; it is outside the supported window and receives no further patches."

IMPACT = {
    "master-behind": "Control plane is behind its release channel's default and carries whatever the intervening builds fixed until it moves.",
    "pool-skew": "This node pool's version skew against the control plane risks or already blocks the cluster's next control-plane upgrade.",
    "fleet-spread": "API-compatibility testing and rollout playbooks must cover every minor version this fleet spans.",
    "no-channel": "This cluster is on a static version with no release channel; it receives no automatic control-plane security patches.",
    "no-autoupgrade": "This node pool will drift out of the skew window on its own and eventually block the control plane.",
    "no-autorepair": "Unhealthy nodes stay in this pool until an operator notices.",
    "no-maintenance-window": "Automatic upgrades on this cluster can begin at any hour, including business hours.",
    "blocking-exclusion": "A maintenance exclusion is currently suppressing upgrades on this cluster.",
    "stale-image-type": "This node pool's image type is no longer offered at this location and cannot take node-image patches.",
    "no-notifications": "This cluster publishes no GKE upgrade notifications; upgrade-available signals reach no one between audits.",
}


def log(msg: str) -> None:
    print(f"[patch_readiness] {msg}", file=sys.stderr, flush=True)


class Run(NamedTuple):
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]


def _text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def default_run(argv: list[str], *, timeout: int = DEFAULT_TIMEOUT_S) -> Run:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        # `TimeoutExpired` carries whatever the child wrote as bytes, `text=True`
        # or not, and every caller matches markers against a str.
        return Run(argv, TIMEOUT_RC, _text(exc.stdout), _text(exc.stderr), time.monotonic() - t0)
    except Exception as exc:
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def run_and_gate(argv: list[str], *, run: RunFn = default_run) -> tuple[object | None, Run]:
    result = run(argv)
    if result.rc != 0 or not result.stdout.strip():
        return None, result
    try:
        return json.loads(result.stdout), result
    except json.JSONDecodeError:
        return None, result


def output_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(argv_str: str, result: Run) -> dict:
    return {
        "command": argv_str,
        "rc": result.rc,
        "duration_s": round(result.duration_s, 2),
        "output_sha256": output_digest(result.stdout),
    }


class Discovery(NamedTuple):
    projects: list[str]
    # Set when no project could be resolved at all: an empty fleet is then a
    # failure to look, and the manifest says so with a top-level `error`.
    error: str | None
    # Set when the scope is provably short -- `projects list` failed or
    # filtered out the active project, or `--project` narrowed it -- and
    # turned into a `project/UNENUMERATED_PROJECTS` row by `collect_fleet`.
    partial: str | None = None


def _stderr_excerpt(result: Run) -> str:
    return result.stderr.strip()[:ERROR_EXCERPT_CHARS] or "no stderr"


def discover_fleet(base_project: str | None, *, run: RunFn) -> Discovery:
    """§1.1's project scope: the active project plus every project
    `gcloud projects list` returns, or the one `--project` names.

    Discovery names projects and lists none of them. It used to list each
    candidate and keep the ones holding a cluster, which listed every project
    serially here and then a second time in the sweep; a project holding no
    clusters contributes no manifest entry either way. Mirrors
    `fleet_drift.discover_fleet`."""
    if base_project:
        return Discovery([base_project], None, SCOPED_RUN_NOTE.format(project=base_project))

    result = run(["gcloud", "config", "get-value", "project"])
    base = result.stdout.strip() if result.rc == 0 else ""
    projects = [base] if base else []

    _, list_result = run_and_gate(["gcloud", "projects", "list", "--format", "value(projectId)"], run=run)
    if list_result.rc != 0:
        if not projects:
            error = (
                f"project discovery failed: `gcloud config get-value project` rc={result.rc} "
                f"named no project and `gcloud projects list` rc={list_result.rc}: "
                f"{_stderr_excerpt(list_result)}"
            )
            log(f"WARNING: {error}; no project to audit")
            return Discovery([], error)
        partial = (
            f"`gcloud projects list` rc={list_result.rc}: {_stderr_excerpt(list_result)}. "
            f"The scope fell back to the active project {base!r}; how many other projects "
            "the fleet holds is unknown, so this run cannot speak for their clusters."
        )
        log(f"WARNING: {partial}")
        return Discovery(projects, None, partial)

    listed = [p.strip() for p in (list_result.stdout or "").splitlines() if p.strip()]
    for candidate in listed:
        if candidate not in projects:
            projects.append(candidate)
    if base and base not in listed:
        # rc 0 and the active project absent from its own output: the listing
        # is filtered rather than complete, so the scope is short by at least
        # one project nobody can name.
        partial = (
            f"`gcloud projects list` rc=0 did not name the active project {base!r}, so it is "
            f"filtered rather than complete: it returned {len(listed)} project(s) and this run "
            "reads clusters in one it did not return. How many other projects the fleet holds "
            "is unknown, so this run cannot speak for their clusters."
        )
        log(f"WARNING: {partial}")
        return Discovery(projects, None, partial)
    if not projects:
        error = (
            "project discovery named no project: `gcloud config get-value project` is unset "
            "and `gcloud projects list` returned nothing"
        )
        log(f"WARNING: {error}; no project to audit")
        return Discovery([], error)
    return Discovery(projects, None)


def _gate_failed_project(project: str, error: str) -> dict:
    return {
        "name": f"{PROJECT_TARGET_PREFIX}{project}",
        "project": project,
        "location": GLOBAL_LOCATION,
        "outcome": OUTCOME_GATE_FAILED,
        "error": error[:ERROR_EXCERPT_CHARS],
    }


# --------------------------------------------------------------------------- #
# Version arithmetic — §2's rule, verbatim.
# --------------------------------------------------------------------------- #


def parse_version(v: str) -> tuple[int, int, int, int] | None:
    m = VERSION_RE.match(v or "")
    if not m:
        return None
    major, minor, patch, build = m.groups()
    return (int(major), int(minor), int(patch), int(build or 0))


def minor_of(v: str) -> tuple[int, int] | None:
    parsed = parse_version(v)
    return parsed[:2] if parsed else None


def normalize_server_config(raw: dict) -> dict:
    channels = {c.get("channel"): c for c in raw.get("channels") or [] if c.get("channel")}
    return {
        "channels": channels,
        "validMasterVersions": raw.get("validMasterVersions") or [],
        "validImageTypes": {t.upper() for t in raw.get("validImageTypes") or []},
    }


# --------------------------------------------------------------------------- #
# Checks. Each takes the cluster's raw `gcloud container clusters list` item
# (`clusters describe`'s shape is identical) plus, where needed, the cached
# baseline for its location. `object` follows §2: `Cluster/<name>` for a
# cluster-scoped finding, `NodePool/<pool>` for a per-pool one.
# --------------------------------------------------------------------------- #


def _release_channel(cluster: dict) -> str:
    """The cluster's channel, with `UNSPECIFIED` reported as no channel.

    GKE spells "this cluster is on a static version" two ways: the
    `releaseChannel` object absent, and `releaseChannel.channel:
    "UNSPECIFIED"`. `check_no_channel` has always treated them alike; the
    version checks did not, and truthiness let `UNSPECIFIED` into the
    channel branch, where it missed in `baseline["channels"]` and returned
    `None`. That exempted exactly the clusters that most need the check --
    static-version ones, which take no automatic control-plane patches -- and
    it did so silently, with `master-behind` still recorded in the manifest as
    a check that ran and found nothing.
    """
    channel = (cluster.get("releaseChannel") or {}).get("channel") or ""
    return "" if channel == UNSPECIFIED_CHANNEL else channel


def _upgrade_in_progress(cluster: dict) -> bool:
    """§3's universal suppression gate, cluster half: a `RECONCILING` cluster
    is mid-upgrade and its version drift is the upgrade, not a finding."""
    return (cluster.get("status") or "") == STATUS_RECONCILING


def _still_offered(baseline: dict, version: str, channel: str) -> bool:
    """Whether GKE still serves `version` at this location, by any route open
    to this cluster.

    `validVersions` is per channel and it moves the moment a channel promotes
    a new build, so a version's absence from one channel's roster says the
    rollout has passed this cluster by -- not that the version is unsupported.
    Two other rosters in the same `get-server-config` response answer the
    support question directly: `validMasterVersions`, which is the
    channel-independent list of everything the location will still create or
    upgrade a control plane to, and any *other* channel's `validVersions`.
    A hit in either is decisive.

    Measured on 2026-09-06, against the seven `master-behind` criticals that
    day's run published. `1.35.7-gke.1027000` and `1.36.3-gke.1640000` are
    both in `validMasterVersions` at us-east4 and us-east4-c; the second is
    also in REGULAR's and EXTENDED's `validVersions` there, absent only from
    RAPID. Neither is out of support, and GKE proved it in the same window by
    auto-upgrading five of those clusters' control planes unattended.

    Another channel's roster is a route only for a cluster already on a
    channel. A static-version cluster takes versions from
    `validMasterVersions` alone -- reaching a build only a channel lists means
    enrolling in it -- so a version only EXTENDED still carries is, for that
    cluster, offered by nothing.
    """
    if version in (baseline.get("validMasterVersions") or []):
        return True
    if not channel:
        return False
    return any(
        version in (info.get("validVersions") or [])
        for info in (baseline.get("channels") or {}).values()
    )


def master_behind_judged(cluster: dict, baseline: dict | None) -> bool:
    """Whether the baseline carries the roster `check_master_behind` judges against.

    A channel `get-server-config` did not return, or a roster it returned
    empty, is a check that judged nothing, and the manifest must not record it
    as run: a check in `commands` with no candidate reads as clean. So is a
    channel without a parseable `defaultVersion`: branch (a) still runs, but
    (b) and (c) cannot, and a hit on (a) is recorded on its own. And so is a
    cluster off its channel's roster that no other channel lists, when the
    baseline has no `validMasterVersions`: whether anything still offers its
    version is the question every branch rests on, and nothing in the
    baseline answers it. A `currentMasterVersion` that does not parse judges
    nothing against any roster, so it is unjudged whatever the baseline holds.
    """
    if baseline is None or parse_version(cluster.get("currentMasterVersion") or "") is None:
        return False
    channel = _release_channel(cluster)
    if not channel:
        return bool(baseline["validMasterVersions"])
    info = baseline["channels"].get(channel) or {}
    valid = info.get("validVersions") or []
    current = cluster.get("currentMasterVersion") or ""
    if current not in valid and not baseline["validMasterVersions"] and not _still_offered(baseline, current, channel):
        return False
    return bool(valid) and parse_version(info.get("defaultVersion") or "") is not None


def check_master_behind(cluster: dict, baseline: dict | None) -> dict | None:
    if baseline is None:
        return None
    if _upgrade_in_progress(cluster):
        return None
    current = cluster.get("currentMasterVersion") or ""
    current_t = parse_version(current)
    if current_t is None:
        return None
    channel = _release_channel(cluster)
    if channel:
        info = baseline["channels"].get(channel)
        if info is None:
            return None
        valid, default = info.get("validVersions") or [], info.get("defaultVersion")
    else:
        valid, default = baseline["validMasterVersions"], None

    # An empty roster is a baseline that did not carry the field, not a fleet
    # where no version is offered. `current not in valid` is true of every
    # cluster against `[]`, so the branch below would call the whole fleet
    # critical -- "absent from validVersions" on clusters running the version
    # the channel had just promoted. `get-server-config` returning a channel
    # with no `validVersions` is the SOP's own "inspect the raw output before
    # relying on a field" case (§2), and the honest answer to a baseline that
    # says nothing is to say nothing.
    if not valid:
        return None
    # Absent from this channel's roster, and from every other route the
    # location offers: nothing will upgrade this control plane, which is what
    # `critical` claims. Absent from this channel's roster *alone* is the
    # ordinary staggered rollout, and it falls through to the same-minor and
    # behind-default branches below, which grade it honestly. Grading every
    # off-roster version `critical` publishes "receives no further patches"
    # over clusters the same API response shows GKE still patching.
    off_roster = current not in valid
    if off_roster and not _still_offered(baseline, current, channel):
        # "Offered nowhere" is only decidable against `validMasterVersions`.
        # Without it -- the same missing field as the empty roster above --
        # this would grade the staggered rollout `critical` again, and the
        # branches below would claim patches nothing established. Another
        # channel listing the version is positive evidence and still counts.
        # `master_behind_judged` leaves the slug out of `commands` here too.
        if not baseline["validMasterVersions"]:
            return None
        return {"object": f"Cluster/{cluster['name']}", "excerpt": f"currentMasterVersion={current} offered by no channel at this location", "severity": CRITICAL, "impact": UNSUPPORTED_MASTER_IMPACT}
    if not default:
        return None
    default_t = parse_version(default)
    if default_t is None:
        return None
    # Said only where it is true, and it is the difference between a cluster
    # the channel has not reached yet and one it has already moved past. Both
    # still take automatic patches -- `_still_offered` established that -- so
    # neither raises the severity; it is context for the reader deciding
    # whether to wait for the rollout or ask for the upgrade.
    roster = " and no longer on that channel's roster" if off_roster else ""
    if current_t[:2] < default_t[:2]:
        gap = default_t[1] - current_t[1] if current_t[0] == default_t[0] else 0
        behind = "a minor" if gap == 1 else f"{gap} minors" if gap else "a major"
        return {"object": f"Cluster/{cluster['name']}", "excerpt": f"currentMasterVersion={current} is {behind} behind channel default {default}{roster}", "severity": MAJOR, "impact": BEHIND_MASTER_IMPACT.format(default=default)}
    if current_t[:2] == default_t[:2] and current_t < default_t:
        return {"object": f"Cluster/{cluster['name']}", "excerpt": f"currentMasterVersion={current} is behind channel default {default} on the same minor{roster}", "severity": MINOR, "impact": BEHIND_MASTER_IMPACT.format(default=default)}
    return None


def _pool_status_excludes(pool: dict, cluster: dict) -> bool:
    return (pool.get("status") or "") in (STATUS_RECONCILING, STATUS_PROVISIONING) or _upgrade_in_progress(cluster)


def check_pool_skew(cluster: dict) -> list[dict]:
    # Autopilot is not excluded. Its node pools carry a real `version` and can
    # genuinely trail the control plane, and the one Autopilot-specific way
    # that happens innocently -- Google upgrading the control plane first and
    # rolling nodes over the following days -- is already the `minor_gap == 0`
    # rollout allowance below, which does not flag. A *minor*-level gap on
    # Autopilot is not a rollout in progress; it is the skew policy running
    # out, and it is worth a support case even though the operator has no knob.
    master_t = parse_version(cluster.get("currentMasterVersion") or "")
    if master_t is None:
        return []
    hits = []
    for pool in cluster.get("nodePools") or []:
        if _pool_status_excludes(pool, cluster):
            continue
        pool_t = parse_version(pool.get("version") or "")
        if pool_t is None:
            continue
        name = pool.get("name", "")
        if pool_t > master_t:
            hits.append({"object": f"NodePool/{name}", "excerpt": f"pool version {pool.get('version')} ahead of control plane {cluster.get('currentMasterVersion')}", "severity": MAJOR})
            continue
        if pool_t[0] != master_t[0]:
            hits.append({"object": f"NodePool/{name}", "excerpt": f"pool on a different major version ({pool.get('version')} vs {cluster.get('currentMasterVersion')})", "severity": CRITICAL})
            continue
        minor_gap = master_t[1] - pool_t[1]
        if minor_gap > SKEW_CEILING_MINORS:
            hits.append({"object": f"NodePool/{name}", "excerpt": f"pool {minor_gap} minors behind control plane", "severity": CRITICAL})
        elif minor_gap == SKEW_CEILING_MINORS:
            hits.append({"object": f"NodePool/{name}", "excerpt": f"pool {minor_gap} minors behind control plane, at GKE's skew ceiling", "severity": MAJOR})
        elif minor_gap == 1:
            auto = ((pool.get("management") or {}).get("autoUpgrade"))
            # Absent is disabled, as §3.5 reads it: the API omits a false boolean.
            hits.append({"object": f"NodePool/{name}", "excerpt": "pool 1 minor behind control plane", "severity": MINOR if auto else MAJOR})
        elif minor_gap == 0 and pool_t[2:] < master_t[2:]:
            # Same minor, older patch/build. Up to one patch behind -- the same
            # patch on an older build included, which is the smaller lag -- is
            # GKE upgrading the control plane first and draining pools after:
            # transient, and the SOP's own "Do NOT flag" case for it.
            if master_t[2] - pool_t[2] <= MAX_ROLLOUT_PATCH_LAG:
                continue
            hits.append({"object": f"NodePool/{name}", "excerpt": f"pool on an older patch than the control plane ({pool.get('version')} vs {cluster.get('currentMasterVersion')})", "severity": MINOR})
    return hits


def check_fleet_spread(clusters: list[dict]) -> list[dict]:
    """§3.3, over the whole fleet. `clusters` is every cluster the run audited,
    not one project's — the spread is a property of the fleet, and computing it
    per project both misses a fleet whose two minors live in two projects and
    emits one finding per project on a fleet where they do not.

    §3's suppression gate applies here as it does to 3.1 and 3.2, and it has to
    remove the cluster from the computation rather than only from the finding:
    a cluster halfway through its upgrade is the one most likely to be the
    outlier that makes the fleet look two minors wide.
    """
    minors = {}
    for c in clusters:
        if _upgrade_in_progress(c):
            continue
        version = parse_version(c.get("currentMasterVersion") or "")
        if version is not None:
            minors.setdefault(version[:2], []).append((version, c["name"]))
    if len(minors) < 2:
        return []
    oldest, newest = min(minors), max(minors)
    # §2: any difference in the first element is unbounded skew, so a fleet on
    # two majors is the widest spread there is, and a minor count across the
    # boundary would be meaningless.
    if newest[0] != oldest[0]:
        width = "across major versions"
    elif newest[1] - oldest[1] >= FLEET_SPREAD_MIN_MINORS:
        width = f"{newest[1] - oldest[1]} minors wide"
    else:
        return []
    # The oldest version on the oldest minor, so patch and build decide the
    # laggard; the qualified name only breaks a tie between equal versions,
    # which keeps the finding's id on one cluster from run to run.
    laggard = min(minors[oldest])[1]
    return [
        {
            "object": f"Cluster/{laggard}",
            "excerpt": f"fleet spans {oldest[0]}.{oldest[1]}–{newest[0]}.{newest[1]}, {width}",
        }
    ]


def check_no_channel(cluster: dict) -> dict | None:
    if _release_channel(cluster):
        return None
    return {"object": f"Cluster/{cluster['name']}", "excerpt": "releaseChannel.channel is empty"}


def _observed(mapping: dict, key: str, path: str) -> str:
    """`<path>=<value>` when the key is there, `<path> absent` when it is not.

    A disabled setting and a missing one are different observations with
    different fixes -- one is flipped, the other has to be created -- and an
    excerpt reading "false or absent" commits to neither. It also hides the
    move between them: a pool that grows an explicit `autoRepair: false`
    publishes an excerpt byte-identical to the one it had while the field was
    missing, so a run-over-run comparison of excerpts treats a real change as no change
    and the run-over-run diff shows nothing happened. `json.dumps` rather than
    `str` so the value reads as it does in the API response -- `false`, not
    `False`, and `null` for a key present but unset.
    """
    return f"{path}={json.dumps(mapping[key])}" if key in mapping else f"{path} absent"


def check_no_autoupgrade(cluster: dict) -> list[dict]:
    # Runs on Autopilot too: the field is there and reads `true`, so this
    # verifies the platform's guarantee instead of taking it on trust.
    return [
        {
            "object": f"NodePool/{p.get('name', '')}",
            "excerpt": _observed(p.get("management") or {}, "autoUpgrade", "management.autoUpgrade"),
        }
        for p in cluster.get("nodePools") or []
        if not (p.get("management") or {}).get("autoUpgrade")
    ]


def check_no_autorepair(cluster: dict) -> list[dict]:
    # Autopilot included, for the reason `check_no_autoupgrade` gives.
    return [
        {
            "object": f"NodePool/{p.get('name', '')}",
            "excerpt": _observed(p.get("management") or {}, "autoRepair", "management.autoRepair"),
        }
        for p in cluster.get("nodePools") or []
        if not (p.get("management") or {}).get("autoRepair")
    ]


def check_no_maintenance_window(cluster: dict) -> dict | None:
    window = ((cluster.get("maintenancePolicy") or {}).get("window") or {})
    # `in`, not truthiness: an API response can carry an empty-but-present
    # `recurringWindow: {}` while still populating it a moment later, and a
    # falsy-empty-dict-means-absent test would flag a cluster mid-populate.
    if "dailyMaintenanceWindow" in window or "recurringWindow" in window:
        return None
    return {"object": f"Cluster/{cluster['name']}", "excerpt": "no maintenancePolicy.window configured"}


def _utc(timestamp: str | None) -> datetime:
    """An RFC 3339 timestamp as an aware datetime. GKE writes `Z`; one written
    without an offset is read as UTC rather than left naive, because comparing
    a naive datetime with `now` raises `TypeError` and would cost the whole
    project its collection over one exclusion."""
    parsed = datetime.fromisoformat((timestamp or "").replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def blocking_exclusions_readable(cluster: dict) -> bool:
    """Whether every blocking-scope exclusion carries timestamps `_utc` reads.

    `check_blocking_exclusion` skips one it cannot read, and that skip is a
    freeze the check did not judge: in `commands` it would read as clean."""
    window = ((cluster.get("maintenancePolicy") or {}).get("window") or {})
    for exclusion in (window.get("maintenanceExclusions") or {}).values():
        scope = ((exclusion.get("maintenanceExclusionOptions") or {}).get("scope")) or DEFAULT_EXCLUSION_SCOPE
        if scope not in BLOCKING_EXCLUSION_SCOPES:
            continue
        try:
            _utc(exclusion.get("startTime"))
            _utc(exclusion.get("endTime"))
        except ValueError:
            return False
    return True


def check_blocking_exclusion(cluster: dict, *, now: datetime, has_version_finding: bool) -> dict | None:
    # `maintenanceExclusions` is a map keyed by exclusion name
    # (`{name: {startTime, endTime, maintenanceExclusionOptions}}`), not a
    # list -- iterating it as a list would walk the names, not the windows.
    window = ((cluster.get("maintenancePolicy") or {}).get("window") or {})
    exclusions = window.get("maintenanceExclusions") or {}
    best, best_key = None, None
    for name, exclusion in exclusions.items():
        scope = ((exclusion.get("maintenanceExclusionOptions") or {}).get("scope")) or DEFAULT_EXCLUSION_SCOPE
        if scope not in BLOCKING_EXCLUSION_SCOPES:
            continue
        try:
            start = _utc(exclusion.get("startTime"))
            end = _utc(exclusion.get("endTime"))
        except ValueError:
            continue
        if not (start <= now <= end):
            continue
        # `.days` truncates, so a 30-day-23-hour freeze read as 30 and fell
        # through: the SOP's threshold is "longer than 30 days", and comparing
        # the timedelta itself is the only reading of that which does not lose
        # the last day. The SOP measures the time left, from now to the end.
        long_freeze = (end - now) > LONG_FREEZE
        if not (long_freeze or has_version_finding):
            continue
        # Several can qualify at once. The one that ends last is the freeze
        # actually holding the cluster, and choosing it keeps the excerpt
        # stable whatever order the API lists the map in.
        if best is not None and (end, name) <= best_key:
            continue
        best_key = (end, name)
        severity = MAJOR if has_version_finding else MINOR
        best = {"object": f"Cluster/{cluster['name']}", "excerpt": f"exclusion {name} (scope {scope}) until {exclusion.get('endTime')}", "severity": severity}
    return best


def check_stale_image_type(cluster: dict, baseline: dict | None) -> list[dict]:
    # Autopilot included: its pools carry `config.imageType` like any other
    # (`COS_CONTAINERD` on the fleet measured 2026-09-05), so the check reads a
    # real value rather than declining on the belief that there is none.
    if baseline is None:
        return []
    valid = baseline["validImageTypes"]
    # `check_master_behind`'s empty-roster rule, for the same reason: against
    # an empty set every pool reads "no longer offered", which is a baseline
    # that did not carry the field rather than a location offering no image.
    if not valid:
        return []
    hits = []
    for pool in cluster.get("nodePools") or []:
        image_type = ((pool.get("config") or {}).get("imageType") or "").upper()
        if not image_type:
            continue
        if image_type not in valid or image_type in DEPRECATED_IMAGE_TYPES:
            hits.append({"object": f"NodePool/{pool.get('name', '')}", "excerpt": f"config.imageType={image_type}"})
    return hits


def notification_topic(cluster: dict) -> str:
    """The topic this cluster already publishes upgrade notifications to.

    Enabled is not enough: a filter that leaves out upgrade events publishes
    to the topic without publishing upgrade notifications, and
    `check_no_notifications` flags that cluster for it."""
    pubsub = ((cluster.get("notificationConfig") or {}).get("pubsub") or {})
    event_types = (pubsub.get("filter") or {}).get("eventType") or []
    if not pubsub.get("enabled") or (event_types and UPGRADE_AVAILABLE_EVENT not in event_types):
        return ""
    return str(pubsub.get("topic") or "")


def _upgrade_scheduled(cluster: dict) -> bool:
    """§3.10's exemption: GKE has already scheduled an upgrade on a pool."""
    return any(
        ((pool.get("management") or {}).get("upgradeOptions") or {}).get("autoUpgradeStartTime")
        for pool in cluster.get("nodePools") or []
    )


def check_no_notifications(cluster: dict) -> dict | None:
    if _upgrade_scheduled(cluster):
        return None
    pubsub = ((cluster.get("notificationConfig") or {}).get("pubsub") or {})
    if not pubsub.get("enabled"):
        return {
            "object": f"Cluster/{cluster['name']}",
            "excerpt": _observed(pubsub, "enabled", "notificationConfig.pubsub.enabled"),
        }
    event_types = ((pubsub.get("filter") or {}) or {}).get("eventType") or []
    if event_types and UPGRADE_AVAILABLE_EVENT not in event_types:
        return {"object": f"Cluster/{cluster['name']}", "excerpt": f"{FILTER_EXCLUDES_PREFIX} {UPGRADE_AVAILABLE_EVENT}: {event_types}"}
    return None


def _emit(slug: str, hit: dict) -> dict:
    return {
        "check": slug,
        "namespace": "",
        "object": hit["object"],
        "severity": hit.get("severity") or SEVERITY[slug],
        "excerpt": hit["excerpt"],
        "impact": hit.get("impact") or IMPACT[slug],
        # `master-behind`'s impact says which arm fired -- unpatched versus
        # behind-but-patched -- so `finish` publishes it over the model's
        # wording (`audit_report.adopt_arm_impact`).
        "impact_authoritative": slug in ARM_SPECIFIC_IMPACT_CHECKS,
        "needs_triage": None,
    }


# The four node-pool checks used to be declared `checks_not_applicable` on
# Autopilot, each with a reason saying the field was not there to read:
# "exposes no user node pool whose version could skew", "there is no node pool
# management setting to inspect", "exposes no user node pool carrying a
# config.imageType". Every one of those is false. `clusters list` returns
# Autopilot node pools like any other -- five per cluster on the fleet measured
# 2026-09-05, each carrying `version`, `config.imageType`, and a `management`
# block with `autoUpgrade` and `autoRepair` both explicitly `true`.
#
# Which makes the disposition wrong, not just its wording. A check is not
# applicable when the cluster's shape means the question cannot arise; here the
# question arises and the API answers it. What Autopilot removes is the *knob*,
# not the *reading* -- and an audit that declines to read a field because it
# trusts the platform's guarantee about that field is asserting the answer
# rather than checking it. If GKE ever returns `autoUpgrade: false` on an
# Autopilot pool, that is a finding worth a support case, and the version of
# this collector that declared the check inapplicable is the one that would
# never see it.
#
# So all four run everywhere. On Autopilot they pass, which costs one dict
# lookup and converts four declined checks per cluster into four verified ones.
# The SOP carries the other half: a hit on an Autopilot cluster is
# `kind: manual`, because detection and remediation are separate axes and only
# the second one Google owns.


def collect_one_cluster(cluster: dict, baseline: dict | None, *, now: datetime) -> tuple[list[str], list[dict]]:
    """The check slugs this cluster has data for, and its candidates. A slug
    missing from the first is a check that judged nothing here -- `master-behind`
    and `stale-image-type` without the baseline they read, `pool-skew` or
    `stale-image-type` over a version or image type that does not parse, and
    `blocking-exclusion` over a freeze it could not grade -- and a coverage gap
    the SOP tells the agent to name in `limitations`, not a gate failure. No cluster shape
    rules any of the ten out -- the comment above explains why Autopilot does
    not -- so the collector never writes `checks_not_applicable`."""
    slugs = ["no-channel", "no-autoupgrade", "no-autorepair", "no-maintenance-window", "blocking-exclusion", "no-notifications"]
    candidates = []
    # `pool-skew` compares every pool against the control plane, so a master
    # version that does not parse leaves it nothing to judge; `check_pool_skew`
    # returns no hits then, which in `commands` would read as clean. A pool
    # whose own version does not parse is skipped the same way, so it also
    # keeps the slug out: `commands` says the check judged the whole cluster.
    # Its candidates go with it, since a candidate's evidence is the
    # `commands` entry for its check; the SOP's manual fallback covers both.
    pool_skew_judged = parse_version(cluster.get("currentMasterVersion") or "") is not None and all(
        parse_version(pool.get("version") or "") is not None
        for pool in cluster.get("nodePools") or []
        if not _pool_status_excludes(pool, cluster)
    )
    if pool_skew_judged:
        slugs.insert(0, "pool-skew")

    master_behind_hit = check_master_behind(cluster, baseline)
    pool_skew_hits = check_pool_skew(cluster) if pool_skew_judged else []
    # §3.8's escalation is specifically "a critical/major version finding" --
    # a minor one (3.1c's same-minor patch lag, 3.2's patch-only drift)
    # does not, on its own, justify calling out a long freeze as major.
    has_version_finding = (master_behind_hit or {}).get("severity") in VERSION_FINDING_SEVERITIES or any(
        h.get("severity") in VERSION_FINDING_SEVERITIES for h in pool_skew_hits
    )
    # Whether a freeze is reported, and at what severity, rests on those two
    # checks. When either judged nothing and neither found a critical/major,
    # an in-effect blocking exclusion cannot be graded, so `blocking-exclusion`
    # leaves `commands` with its candidate as `pool-skew` does. With no such
    # exclusion there is nothing to grade and the check judged the cluster.
    escalation_judged = has_version_finding or (pool_skew_judged and master_behind_judged(cluster, baseline))
    # A blocking exclusion whose window does not parse is skipped by the
    # check, so it keeps the slug out for the same reason.
    frozen = check_blocking_exclusion(cluster, now=now, has_version_finding=True) is not None
    if not blocking_exclusions_readable(cluster) or (not escalation_judged and frozen):
        slugs.remove("blocking-exclusion")

    single_hits = (
        ("no-channel", check_no_channel(cluster)),
        ("no-maintenance-window", check_no_maintenance_window(cluster)),
        ("blocking-exclusion", check_blocking_exclusion(cluster, now=now, has_version_finding=has_version_finding)),
        ("no-notifications", check_no_notifications(cluster)),
    )
    candidates += [_emit(slug, hit) for slug, hit in single_hits if hit and slug in slugs]
    candidates += [_emit("pool-skew", hit) for hit in pool_skew_hits]
    candidates += [_emit("no-autoupgrade", hit) for hit in check_no_autoupgrade(cluster)]
    candidates += [_emit("no-autorepair", hit) for hit in check_no_autorepair(cluster)]

    if master_behind_hit is not None or master_behind_judged(cluster, baseline):
        slugs.append("master-behind")
        if master_behind_hit is not None:
            candidates.append(_emit("master-behind", master_behind_hit))
    # A pool with no `config.imageType` is skipped by the check, so it keeps
    # the slug out for the reason `pool-skew`'s comment gives.
    image_types_readable = all(
        ((pool.get("config") or {}).get("imageType") or "") for pool in cluster.get("nodePools") or []
    )
    if baseline is not None and baseline["validImageTypes"] and image_types_readable:
        slugs.append("stale-image-type")
        candidates += [_emit("stale-image-type", hit) for hit in check_stale_image_type(cluster, baseline)]

    return slugs, candidates


def crashed_entries(project: str, exc: BaseException) -> list[dict]:
    """The `clusters[]` entries for a worker that raised something unmodelled.

    `future.result()` re-raises, so one unhandled exception on one project
    aborts `collect_fleet` — and the SOP invokes this collector as
    `patch_readiness.py … > manifest_security-patch-orchestrator.json`, so by
    then the shell has already truncated the file. The run loses every project
    to one bad object instead of one. The shape is the one the gate-failed
    branch above already uses, for the same reason it gives: a project missing
    from the manifest reads as a project holding no clusters.
    """
    log(f"{project}: collector raised {type(exc).__name__}: {exc}")
    return [_gate_failed_project(project, f"collector raised {type(exc).__name__}: {exc}")]


def out_of_scope_reason(cluster: dict) -> str | None:
    """Why §1.5 leaves this cluster out of scope, or `None` to audit it.

    The collector has to answer this, not the model. Marking such a cluster
    `collected` makes `cross_check_manifest` demand it in `scope.clusters`
    while §1.5 keeps it out, so on a fleet holding one PROVISIONING or alpha
    cluster every document was rejected whatever the model did with it. `RECONCILING` is
    deliberately not here: §3's gate suppresses that cluster's *version*
    findings and its policy checks still run, which is a different disposition
    from not auditing it at all.
    """
    status = (cluster.get("status") or "").upper()
    if status in _UNAUDITABLE_STATUSES:
        return _UNAUDITABLE_STATUSES[status]
    if cluster.get("enableKubernetesAlpha"):
        return "enableKubernetesAlpha=true; alpha clusters cannot be upgraded and auto-expire by design"
    return None


def target_name(project: str, location: str, name: str) -> str:
    """What the manifest calls a cluster: `<project>/<location>/<name>`.

    A GKE name is unique only inside one project and location, and
    `audit_report._vouching_clusters` keys on the manifest's name, so two
    `seeded-b` clusters in two projects collapse into one entry. Every cluster
    is qualified, not only one that collides today: a name qualified only on
    collision moves when the rest of the fleet changes, and a finding's id
    moves with it. The same rule `fleet_drift.qualify_targets` states. A
    candidate's `object` stays the bare resource; the identity tuple already
    carries the qualified cluster beside it.
    """
    return QUALIFIED_TARGET_SEPARATOR.join([p for p in (project, location) if p] + [name])


def attach_fleet_spread(entries: list[dict]) -> None:
    """Run §3.3 once over the whole fleet and attach its finding, in place.

    Not inside `collect_project`, which sees one project: §3.3 is "across all
    audited clusters … emit exactly **one** finding". Per project, a fleet
    running 1.28 in one project and 1.31 in another reports nothing, and a
    fleet spread across three projects reports it three times, each naming a
    different laggard.
    """
    readable = [e for e in entries if e.get("outcome") == OUTCOME_COLLECTED]
    # Ranked by the qualified name, which is unique, so the laggard is one
    # cluster; the candidate's `object` then names the bare resource, as every
    # other candidate's does.
    hits = check_fleet_spread(
        [
            {"name": e["name"], "currentMasterVersion": e.get("_master_version") or "", "status": e.get("_status") or ""}
            for e in readable
        ]
    )
    by_name = {entry["name"]: entry for entry in readable}
    for hit in hits:
        target = by_name.get(hit["object"].split("/", 1)[1])
        if target is not None:
            hit["object"] = f"Cluster/{target['_bare_name']}"
            target.setdefault("candidates", []).append(_emit("fleet-spread", hit))
    for entry in entries:
        entry.pop("_bare_name", None)
        entry.pop("_master_version", None)
        entry.pop("_status", None)


def attach_incumbent_topic(entries: list[dict]) -> None:
    """Name the topic the rest of the fleet already publishes to, in place.

    §3.10 tells the agent to write one fleet-scoped topic across every
    `no-notifications` finding, so the harness groups them into a single pull
    request. What it cannot tell the agent is which topic that is on a fleet
    where one already exists -- and the agent sees each cluster's own
    `notificationConfig`, which on a Pub/Sub-disabled hit is empty. Left to infer
    it, the agent infers there is none.

    That is not hypothetical. On 2026-09-05 fifteen of sixteen clusters got a
    `manifest` remediation naming `gke-upgrade-notifications`; the sixteenth,
    `kube-agents-host`, is the one cluster the GitOps repo does not declare, so
    §3.10 routes it to `gcloud` -- which needs a topic path. The agent wrote
    `kind: manual` instead, with a note saying no topic for this exists "in this
    repository or the project's declared resources", while the other fifteen
    findings of the same document were declaring exactly that topic. One
    cluster in sixteen came out of the run with no fix and a reason that its own
    report contradicted, and it would have come out that way every week.

    So the collector states the fact it holds and the agent does not: some
    cluster in this fleet already publishes to `<topic>`. Only from a cluster
    that is genuinely enrolled -- an incumbent is evidence, and a topic nobody
    publishes to yet is a guess. Where no cluster is enrolled, nothing is
    appended and §3.10's "give the topic a fleet-scoped name" stands unaltered:
    the first run on a green-field fleet names the topic, and every run after it
    reads the name back off the fleet rather than inventing it again.
    """
    readable = [e for e in entries if e.get("outcome") == OUTCOME_COLLECTED]
    topics = sorted({t for e in readable if (t := e.get("_notification_topic"))})
    for entry in entries:
        entry.pop("_notification_topic", None)
    # Two topics is a fleet mid-migration, and picking one would send half the
    # findings at the wrong one. The agent gets no hint and falls back to
    # §3.10's naming rule, which is the honest answer to a fleet that does not
    # agree with itself.
    if len(topics) != 1:
        return
    for entry in readable:
        for candidate in entry.get("candidates") or []:
            if candidate.get("check") != "no-notifications":
                continue
            # A filtered cluster has a topic of its own; its fix is the filter,
            # and naming the fleet's topic would point it somewhere else.
            if candidate.get("excerpt", "").startswith(FILTER_EXCLUDES_PREFIX):
                continue
            candidate["excerpt"] = (
                f"{candidate.get('excerpt', '')}; other clusters in this fleet "
                f"publish upgrade notifications to {topics[0]}"
            )


def collect_project(project: str, *, run: RunFn, now: datetime) -> list[dict]:
    argv = ["gcloud", "container", "clusters", "list", "--project", project, "--format", "json"]
    parsed, result = run_and_gate(argv, run=run)
    if parsed is None:
        if any(marker in result.stderr for marker in API_DISABLED_MARKERS):
            log(f"{project}: Kubernetes Engine API is not enabled; no cluster can exist here")
            return []
        log(f"{project}: clusters list gate failed (rc={result.rc}); no clusters known this run")
        # One `project/<p>` entry rather than nothing at all. The manifest is
        # the only record of what the collector managed to read, and a project
        # that drops out of it entirely is indistinguishable from one that
        # holds no clusters -- so cross_check_manifest has nothing to hold the
        # document to, and the run publishes a fleet-wide verdict over clusters
        # nobody enumerated. Recorded as `gate-failed`, the loss is a target the
        # document has to account for, and §6 turns that into a coverage gap.
        return [_gate_failed_project(project, f"clusters list rc={result.rc}: {_stderr_excerpt(result)}")]
    # rc 0 and a warning that some zones did not answer: the clusters that came
    # back are collected, and the project also gets its `gate-failed` row, the
    # only thing that stops the missing ones reading as absent.
    incomplete = [line.strip() for line in result.stderr.splitlines() if ZONE_TIMEOUT_MARKER in line]
    shortfall = []
    if incomplete:
        detail = " ".join(incomplete)
        log(f"{project}: clusters list returned {len(parsed)} cluster(s) but is incomplete: {detail}")
        shortfall = [_gate_failed_project(project, f"clusters list rc=0 but incomplete: {detail}")]
    clusters_record = _record(shlex.join(argv), result)

    by_location: dict[str, list[dict]] = {}
    for c in parsed:
        by_location.setdefault(c.get("location") or c.get("zone") or "", []).append(c)

    baselines: dict[str, tuple[dict, dict] | None] = {}
    for location in by_location:
        sc_argv = ["gcloud", "container", "get-server-config", "--location", location, "--project", project, "--format", "json"]
        sc_parsed, sc_result = run_and_gate(sc_argv, run=run)
        if sc_parsed is None:
            log(f"{project}/{location}: get-server-config gate failed (rc={sc_result.rc}); master-behind/stale-image-type unavailable there")
            baselines[location] = None
        else:
            baselines[location] = (normalize_server_config(sc_parsed), _record(shlex.join(sc_argv), sc_result))

    entries = []
    for c in parsed:
        location = c.get("location") or c.get("zone") or ""
        skip_reason = out_of_scope_reason(c)
        if skip_reason:
            log(f"{project}/{c.get('name', '?')}: out of scope — {skip_reason}")
            entries.append(
                {
                    "name": target_name(project, location, c.get("name", "?")),
                    "project": project,
                    "location": location,
                    "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
                    "outcome": OUTCOME_OUT_OF_SCOPE,
                    # The key `cross_check_manifest` renders when it has to
                    # explain why a target could not just be documented, and
                    # the same one the two failure outcomes use. A reader of
                    # the manifest needs the reason wherever it came from.
                    "error": skip_reason,
                }
            )
            continue
        baseline_pair = baselines.get(location)
        baseline = baseline_pair[0] if baseline_pair else None
        slugs, candidates = collect_one_cluster(c, baseline, now=now)
        commands = {slug: clusters_record for slug in slugs}
        if baseline_pair is not None:
            # Both baseline checks read `get-server-config`, so that call is
            # their evidence rather than `clusters list`. Only the ones
            # `collect_one_cluster` judged: a roster the baseline lacked
            # leaves its slug out of `slugs`, and must stay out of `commands`.
            for slug in ("master-behind", "stale-image-type"):
                if slug in commands:
                    commands[slug] = baseline_pair[1]
        # Recorded for every cluster, not only the laggard §3.3 attaches the
        # finding to. Every cluster in `parsed` handed its minor to the spread
        # computation, so the check ran against all of them and the same
        # `clusters list` output is its evidence -- but §6 reads `commands` as
        # the roster of checks that ran, so recording it only on a hit makes a
        # tight fleet indistinguishable from one nobody measured, and every
        # clean run reports a coverage gap it does not have.
        # A master version that does not parse never reaches the computation
        # (`check_fleet_spread` skips it), so the check did not run against
        # that cluster. A cluster mid-upgrade is skipped too, by §3's
        # suppression gate: that is the check's judgement on it, so it keeps
        # the slug, as a reconciling pool keeps `pool-skew`'s.
        if parse_version(c.get("currentMasterVersion") or "") is not None:
            commands["fleet-spread"] = clusters_record
        entry = {
            "name": target_name(project, location, c["name"]),
            "project": project,
            "location": location,
            "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
            "outcome": OUTCOME_COLLECTED,
            "commands": [{"check": slug, **record} for slug, record in commands.items()],
            "candidates": candidates,
            # `_bare_name`, `_master_version` and `_status` are consumed and
            # removed by `attach_fleet_spread`, which needs every
            # audited cluster's minor and cannot get it from one project's
            # worker. Underscored so a leak shows up as an unknown manifest key
            # rather than as plausible data.
            "_bare_name": c["name"],
            "_master_version": c.get("currentMasterVersion") or "",
            "_status": c.get("status") or "",
            # Consumed and removed by `attach_incumbent_topic`, for the same
            # reason `_master_version` is: the fact is fleet-wide and this
            # worker only ever sees one project.
            "_notification_topic": notification_topic(c),
        }
        entries.append(entry)
    return entries + shortfall


def collect_fleet(project: str | None = None, *, run: RunFn = default_run, max_workers: int = MAX_WORKERS, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    started_at = time.strftime(TIMESTAMP_FORMAT, time.gmtime())
    discovery = discover_fleet(project, run=run)
    projects = discovery.projects

    results: list[list[dict]] = [[] for _ in projects]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(collect_project, p, run=run, now=now): i for i, p in enumerate(projects)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 — see crashed_entries
                results[index] = crashed_entries(projects[index], exc)

    entries = [entry for group in results for entry in group]
    attach_fleet_spread(entries)
    attach_incumbent_topic(entries)

    # A `projects list` that failed took the project names with it, so this
    # row stands for every project nobody enumerated -- and for the ones a
    # `--project` run chose not to read. §3.3's spread is then measured across
    # part of the fleet, which is one more reason the run must read as partial.
    if discovery.partial:
        entries.append(
            {
                "name": UNENUMERATED_PROJECTS_TARGET,
                "project": "",
                "location": GLOBAL_LOCATION,
                "outcome": OUTCOME_GATE_FAILED,
                "error": discovery.partial[:ERROR_EXCERPT_CHARS],
            }
        )

    manifest = {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
        "audit": AUDIT_ID,
        "started_at": started_at,
        "finished_at": time.strftime(TIMESTAMP_FORMAT, time.gmtime()),
        "clusters": entries,
    }
    # §1.3: an empty scope is a failure of discovery, not a clean run. The SOP
    # redirects stdout into the manifest without checking the exit status, so
    # this key, not the exit code, is what tells the worker not to publish.
    collected = [e for e in entries if e.get("outcome") == OUTCOME_COLLECTED]
    failed = [e for e in entries if e.get("outcome") == OUTCOME_GATE_FAILED and e["name"] != UNENUMERATED_PROJECTS_TARGET]
    out_of_scope = [e for e in entries if e.get("outcome") == OUTCOME_OUT_OF_SCOPE]
    if discovery.error:
        manifest["error"] = discovery.error
    elif failed and not collected:
        # The answering projects may still have listed clusters §1.5 rules
        # out; those entries stay in `clusters`, so the error says so rather
        # than calling the projects empty.
        rest = (
            f"the rest held no auditable cluster ({len(out_of_scope)} out of scope)"
            if out_of_scope
            else "the rest held none"
        )
        manifest["error"] = (
            f"no cluster could be read: {len(failed)} of {len(projects)} project(s) in scope "
            f"failed `clusters list` and {rest}; {failed[0]['name']}: {failed[0]['error']}"
        )[:ERROR_EXCERPT_CHARS]
    return manifest


def candidate_summary(manifest: dict) -> list[str]:
    """The closing stderr lines: how many candidates this run produced, and where.

    `candidates` is nested under each cluster in a manifest too large to scan,
    and nothing else in the run states the number a worker has to account
    for. stderr, because stdout is redirected into the manifest file."""
    clusters = [c for c in manifest.get("clusters") or [] if isinstance(c, dict)]
    collected = [c for c in clusters if c.get("outcome") == OUTCOME_COLLECTED]
    # The unenumerated-projects row stands for projects nobody named, not one
    # project, so it is reported apart rather than counted as one.
    unread = [c for c in clusters if c.get("outcome") == OUTCOME_GATE_FAILED and c.get("name") != UNENUMERATED_PROJECTS_TARGET]
    unenumerated = any(c.get("name") == UNENUMERATED_PROJECTS_TARGET for c in clusters)
    by_check: dict[str, list[str]] = {}
    for c in collected:
        for candidate in c.get("candidates") or []:
            by_check.setdefault(str(candidate.get("check", "")), []).append(str(c.get("name", "")))
    total = sum(len(names) for names in by_check.values())
    head = f"{len(collected)} cluster(s) collected"
    if unread:
        head += f", {len(unread)} project(s) unread"
    if unenumerated:
        head += ", project list incomplete"
    head += f"; {total} candidate(s) to report"
    if not total:
        return [head]
    parts = []
    for check in sorted(by_check):
        names = sorted(by_check[check])
        shown = ", ".join(names[:SUMMARY_MAX_CLUSTER_NAMES])
        if len(names) > SUMMARY_MAX_CLUSTER_NAMES:
            shown += f", and {len(names) - SUMMARY_MAX_CLUSTER_NAMES} more"
        parts.append(f"{check}: {len(names)} ({shown})")
    return [
        head + " -- " + "; ".join(parts),
        "every candidate above is a verified finding to report; a resolved_because for one "
        "contradicts this manifest",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="single project to audit; omit to run §1's project discovery")
    args = parser.parse_args(argv)
    manifest = collect_fleet(args.project)
    print(json.dumps(manifest, indent=2))
    if manifest.get("error"):
        log(f"WARNING: {manifest['error']}")
        return 1
    for line in candidate_summary(manifest):
        log(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
