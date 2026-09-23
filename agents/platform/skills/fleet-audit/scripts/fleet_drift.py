#!/usr/bin/env python3
"""fleet_drift.py — Procedural collector for the Fleet Consistency Drift
Audit (`fleet-consistency-drift`).

See docs/designs/fleet-audit-collector-manifest.md for the manifest this
emits and governance/fleet_consistency_drift_sop.md for the checks.

This stream's own collector: every one of its twenty checks -- nineteen
comparative facets and §4.14's cohort-independent `no-environment-label` --
reads GKE control-plane and node-pool metadata through `gcloud container`, no
`kubectl` and no kubeconfig, so it needs no per-cluster credential fetch —
`clusters list` alone, once per project, returns every field every facet
compares, the same shape `clusters describe` would (the SOP's own §1.3
frames `describe` as each finding's `evidence.command`; this collector
issues the cheaper per-project `list` instead and records that as the
command it actually ran: the manifest publishes what happened, not what
the SOP originally described).

**What is procedural here and what stays judgment.** Normalizing a raw
field to one comparable token per facet (§4), computing the majority and
its confidence (§3), and walking the severity ladder are all closed-form.
What does not move: a finding still needs the three-field `recommendation` prose the
validator requires non-empty, and a `kind: manifest` remediation still
needs a human's GitOps declaration lookup. Those stay in the SOP for the
agent to do once the manifest hands it the outliers.
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
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

MANIFEST_VERSION = 1
AUDIT_ID = "fleet-consistency-drift"

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
# counts the rest. The summary is read in a terminal beside a manifest too
# large to scan, so one check with forty outliers must not push the total off
# the top of the screen.
SUMMARY_MAX_CLUSTER_NAMES = 5

# The manifest contract's outcomes and the target name for a project whose
# `clusters list` failed (`project/<id>`, one of the three name shapes §2 of
# the contract admits).
OUTCOME_COLLECTED = "collected"
OUTCOME_GATE_FAILED = "gate-failed"
PROJECT_TARGET_PREFIX = "project/"
QUALIFIED_TARGET_SEPARATOR = "/"
# The manifest target standing for the projects a failed `gcloud projects list`
# never named. Uppercase because a GCP project id cannot be, so no real project
# can collide with it.
UNENUMERATED_PROJECTS_TARGET = PROJECT_TARGET_PREFIX + "UNENUMERATED_PROJECTS"
# What that same target stands for when the operator narrowed the scope on
# purpose. `--project` skips discovery, so a scoped run reads one project and
# names no other -- which is the loss a failed `projects list` produces, minus
# the failure. Without a row saying so the manifest is indistinguishable from a
# fleet of one project, and `finish` resolves every ledger finding on a cluster
# the run never looked at, and closes its remediation pull request.
SCOPED_RUN_NOTE = (
    "scope narrowed to project {project!r} by `--project`: discovery was skipped, so no other "
    "project in this fleet was named or read, and this run cannot speak for their clusters."
)

# gcloud's own words for a project whose Kubernetes Engine API is off. Such a
# project holds no GKE cluster by construction -- enabling the API is what
# creates the ability to hold one -- so reading its `clusters list` failure as
# a lost project puts a `gate-failed` row in every manifest for as long as the
# project exists: a coverage gap on every run, `resolved` pinned at 0, and no
# stale remediation pull request ever closed. A credential that can see an
# organisation's projects sees mostly projects without GKE, so that is the
# ordinary case rather than the exotic one. Permission denied is *not* in here
# on purpose: a project this credential may not list may well hold clusters,
# and that one is a real loss. `terraform/examples/full-install/lifecycle.sh`
# matches the same three forms for the same reason.
API_DISABLED_MARKERS = ("SERVICE_DISABLED", "accessNotConfigured", "has not been used in project")

# gcloud's words for a `clusters list` that some zones answered and others did
# not. It reports that on stderr and still exits 0, and `--format json` carries
# only the clusters that came back -- the API's own `missingZones` field is
# dropped by the formatter, so stderr is the only place the shortfall is
# stated. Read as a complete listing it is the worst shape this collector can
# take: the project looks enumerated, so no `gate-failed` row is written, and
# the clusters in the silent zones are indistinguishable from clusters that do
# not exist. Their cohorts then vote without them -- a majority computed
# against a fleet nobody knows is short -- and `finish` resolves every ledger
# finding on them and closes the remediation pull request, on the strength of
# a run that never saw them.
ZONE_TIMEOUT_MARKER = "did not respond"

# SOP §1: only a cluster with a settled configuration votes, and a cluster
# younger than this has not settled.
VOTING_STATUSES = ("RUNNING", "RECONCILING")
NEW_CLUSTER_AGE_S = 24 * 3600

# SOP §2.2: the label keys that carry the environment signal, in the order
# the SOP resolves them.
ENV_LABEL_KEYS = ("environment", "env", "stage", "tier")

# SOP §2.4 and §3: the cohort floor, the share of a cohort a token needs to be
# its baseline, the two consensus thresholds and the split count that each
# cost a finding one severity step, and the outlier count past which §3.6
# treats a cluster as uncohorted rather than drifting.
COHORT_FLOOR = 3
BASELINE_MIN_RATIO = 2 / 3
LADDER_RATIO_STEP_1 = 0.90
LADDER_RATIO_STEP_2 = 0.80
LADDER_SPLIT_K = 3
SPLIT_CLUSTER_FACETS = 6
UNCOHORTED_SLUG = "uncohorted"
UNCOHORTED_SEVERITY = "major"

# SOP §3.8: how many baseline holders an excerpt's `peers:` line names.
EXCERPT_PEER_NAMES = 6

# GKE spellings the normalizers read around: Google's own reserved label
# prefix, the image-type suffix that is a rename rather than a divergence,
# and the image family secure boot cannot cover.
GOOGLE_LABEL_PREFIX = "goog"
# Taint keys GKE applies to a node pool on the owner's behalf, so a pool
# carrying only these is an ordinary pool rather than one pinned by hand.
# `_is_pinned_pool` says why that distinction decides whether a ledger closes.
# Accelerators are not the only case: GKE taints an Arm pool
# `kubernetes.io/arch=arm64`, a GKE Sandbox pool `sandbox.gke.io/runtime=gvisor`
# and a Windows pool `node.kubernetes.io/os=windows`, none of them a choice its
# owner made or can revisit. Reading any of them as a pin drops every pool of a
# single-architecture cluster from the vote, which is the permanent gap the
# GPU case used to cause.
GKE_MANAGED_TAINT_KEYS = (
    "nvidia.com/gpu",
    "google.com/tpu",
    "kubernetes.io/arch",
    "sandbox.gke.io/runtime",
    "node.kubernetes.io/os",
)
# The one facet whose absent value is another audit's to act on; see
# `_shape_rules_out`.
RELEASE_CHANNEL_SLUG = "release-channel"
IMAGE_TYPE_RUNTIME_SUFFIX = "_CONTAINERD"
WINDOWS_IMAGE_PREFIX = "WINDOWS"

# The one check here that is not a facet. Every `Facet` is comparative and so
# runs only inside a cohort that reached `COHORT_FLOOR`; this one asks why a
# cluster reached no cohort, which is a question the cohort cannot be a
# precondition for. Named rather than inlined because it appears in `IMPACT`,
# in the candidate, and in the `checks_run` roster, and the three must agree.
UNLABELLED_SLUG = "no-environment-label"

# `minor`, matching `label-keys` -- the facet this stands in for on a cluster
# no facet reaches. It is a governance and cost-attribution gap, not an outage,
# and the SOP's ladder has nothing to downgrade it with: there is no cohort, so
# no ratio and no `k`.
UNLABELLED_SEVERITY = "minor"
SEVERITY_LEVELS = ("critical", "major", "minor")
ENV_SYNONYMS = {
    "prod": "prod", "prd": "prod", "production": "prod",
    "staging": "staging", "stg": "staging", "stage": "staging", "preprod": "staging",
    "dev": "dev", "development": "dev", "sandbox": "dev", "sbx": "dev",
    "test": "test", "qa": "test", "uat": "test",
}


def log(msg: str) -> None:
    print(f"[fleet_drift] {msg}", file=sys.stderr, flush=True)


class Run(NamedTuple):
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]


def default_run(argv: list[str], *, timeout: int = DEFAULT_TIMEOUT_S) -> Run:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        return Run(argv, TIMEOUT_RC, exc.stdout or "", exc.stderr or "", time.monotonic() - t0)
    except Exception as exc:
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def run_and_gate(argv: list[str], *, run: RunFn) -> tuple[object | None, Run]:
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


# --------------------------------------------------------------------------- #
# §1: fleet enumeration
# --------------------------------------------------------------------------- #

class Discovery(NamedTuple):
    projects: list[str]
    # Set when no project could be resolved at all -- neither the active
    # project nor `projects list` answered. An empty fleet is then a failure
    # to look, not a fleet with nothing in it, and the manifest says so.
    error: str | None
    # Set when discovery resolved part of the fleet and lost the rest: the
    # active project answered and `projects list` did not, so the scope is one
    # project out of an unknown number. A warning on stderr was all this used
    # to be, and stderr is not in the manifest -- so the run read one project,
    # marked every entry `collected`, and the document that followed carried no
    # coverage gap. `finish` then has no reason to hold a previous ledger
    # finding on a cluster in any other project: it is absent from the
    # document, absent from the manifest, and therefore announced resolved and
    # its remediation pull request stale-closed. `collect_fleet` turns this
    # into a `gate-failed` target so the loss is a row the document must
    # account for, the way a project whose `clusters list` failed already is.
    partial: str | None = None


def discover_fleet(base_project: str | None, *, run: RunFn) -> Discovery:
    """§1.1's project scope. `--project` scopes the run to one project;
    without one it is the active project plus every project
    `gcloud projects list` returns.

    Discovery names projects and lists none of them. It used to decide scope
    by listing each candidate and keeping the ones that answered with at least
    one cluster, which made the sweep's thread pool a cache lookup -- every
    project was listed serially here, at `DEFAULT_TIMEOUT_S` apiece, and a
    project whose list had failed was then listed a second time by the sweep.
    A project holding no clusters contributes no manifest entry either way, so
    nothing was bought with that round trip."""
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
                f"{list_result.stderr.strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}"
            )
            log(f"WARNING: {error}; no project to audit")
            return Discovery([], error)
        partial = (
            f"`gcloud projects list` rc={list_result.rc}: "
            f"{list_result.stderr.strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}. The scope "
            f"fell back to the active project {base!r}; how many other projects the fleet "
            f"holds is unknown, so every cohort here was compared against part of it."
        )
        log(f"WARNING: {partial}")
        return Discovery(projects, None, partial)

    listed = [p.strip() for p in (list_result.stdout or "").splitlines() if p.strip()]
    for candidate in listed:
        if candidate not in projects:
            projects.append(candidate)
    if base and base not in listed:
        # rc 0 and the active project absent from its own output. The
        # credential just resolved that project and is about to list clusters
        # in it, so the listing is filtered rather than complete -- a role
        # carrying `container.clusters.list` but not `resourcemanager.projects.get`,
        # or a proxy that drops rows instead of denying the call. Treated like
        # the rc != 0 branch above: the scope is provably short by at least one
        # project, and a scope that silently narrowed lets `finish` resolve
        # every finding outside it.
        partial = (
            f"`gcloud projects list` rc=0 did not name the active project {base!r}, "
            f"so it is filtered rather than complete: it returned {len(listed)} "
            "project(s) and this run reads clusters in one it did not return. How "
            "many other projects the fleet holds is unknown, so every cohort here "
            "was compared against part of it."
        )
        log(f"WARNING: {partial}")
        return Discovery(projects, None, partial)
    if not projects:
        # Both calls answered rc 0 and neither named a project: the `project`
        # property is unset and the credential can list none. An empty
        # `clusters` array with no `error` is indistinguishable from a fleet
        # holding no clusters, which is the one thing the manifest contract
        # says this key exists to prevent.
        error = (
            "project discovery named no project: `gcloud config get-value project` is unset "
            "and `gcloud projects list` returned nothing"
        )
        log(f"WARNING: {error}; no project to audit")
        return Discovery([], error)
    return Discovery(projects, None)


def discover_projects(base_project: str | None, *, run: RunFn) -> list[str]:
    """The project list alone; `discover_fleet` is what the sweep reads."""
    return discover_fleet(base_project, run=run).projects


def enumerate_project_clusters(project: str, *, run: RunFn) -> tuple[list[dict], dict | None, str | None]:
    """One `clusters list` call, the full Cluster resources this collector
    reads every facet from. Returns `(clusters, command_record, error)` --
    `clusters` is `[]`, `command_record` is `None` and `error` carries what
    gcloud said when the call itself failed, so the caller knows this project
    contributed nothing rather than that it genuinely has no clusters, and can
    say so in the manifest rather than only in a log line.

    One failure is not a loss: a project whose Kubernetes Engine API is off
    cannot hold a GKE cluster, so it answers with zero clusters and no error.
    §1.1 puts every project the credential can see in scope "whether or not
    they hold a cluster", and that sentence only reads as intended if a project
    that cannot hold one reads as empty rather than as unread -- otherwise a
    credential with organisation-wide visibility turns every non-GKE project
    into a permanent coverage gap. See `API_DISABLED_MARKERS`.

    The third shape returns all three: clusters, their command record, and an
    error, for a listing gcloud answered partially and still exited 0 on. The
    clusters it did return are compared; the error is what makes the ones it
    did not say so. See `ZONE_TIMEOUT_MARKER`."""
    argv = ["gcloud", "container", "clusters", "list", "--project", project, "--format", "json"]
    parsed, result = run_and_gate(argv, run=run)
    if parsed is None:
        if any(marker in result.stderr for marker in API_DISABLED_MARKERS):
            log(f"{project}: Kubernetes Engine API is not enabled; no cluster can exist here")
            # No command record either: the call failed, so it backs no
            # finding, and a project contributing no cluster entry has nothing
            # for `commands` to hang on.
            return [], None, None
        log(f"{project}: clusters list gate failed (rc={result.rc}); no clusters known from this project")
        return [], None, f"clusters list rc={result.rc}: {result.stderr.strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}"
    for c in parsed:
        c["_project"] = project
    record = _record(shlex.join(argv), result)
    # rc 0, parseable JSON, and a warning saying the JSON is short. The
    # clusters that did come back are real and are returned to be compared;
    # the error travels with them so the project also gets its `gate-failed`
    # row, which is the only thing that stops the missing ones reading as
    # absent. See `ZONE_TIMEOUT_MARKER`.
    incomplete = [line.strip() for line in result.stderr.splitlines() if ZONE_TIMEOUT_MARKER in line]
    if incomplete:
        detail = " ".join(incomplete)[:ERROR_EXCERPT_CHARS]
        log(f"{project}: clusters list returned {len(parsed)} cluster(s) but is incomplete: {detail}")
        return parsed, record, f"clusters list rc=0 but incomplete: {detail}"
    return parsed, record, None


def cluster_eligibility(c: dict, *, now: datetime) -> str | None:
    """§1's scope rules for a cluster read but not compared. Returns the
    `limitations` sentence, or None when the cluster is a normal voting
    candidate."""
    status = c.get("status", "")
    # `RECONCILING` votes. GKE sets it while work proceeds on a cluster that is
    # otherwise up, and this module compares configuration rather than reading
    # inside the cluster -- the config a reconcile is converging *towards* is
    # exactly what `clusters list` returns, so it is the right thing to compare.
    # Excluding it meant any routine change silently dropped that cluster out
    # of every cohort, which is worse than comparing it: a cohort is a majority
    # vote, and a missing member can flip the majority it was meant to define.
    if status not in VOTING_STATUSES:
        return f"status {status}: excluded from every cohort, no facet compared."
    # `.get("createTime", "")` returns `None` for a key that is present and null,
    # and `None.replace` is an `AttributeError` that `except ValueError` does not
    # catch. There is no caller between here and `main` that catches it either,
    # and the SOP invokes this module as `fleet_drift.py > manifest_….json`, so
    # the shell has already truncated the manifest by the time the traceback
    # prints: one cluster with an unexpected `createTime` loses the whole fleet.
    # An unreadable creation time means "cannot tell how old this is", which is
    # the same answer as an absent one -- treat the cluster as settled rather
    # than excluding it from every cohort on a field it may simply not carry.
    created = c.get("createTime")
    if isinstance(created, str) and created:
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if (now - created_dt).total_seconds() < NEW_CLUSTER_AGE_S:
                return f"created {created}: under 24h, excluded from every cohort."
        except (ValueError, TypeError):
            # `TypeError`, not only `ValueError`: `fromisoformat` accepts
            # `2025-01-01` and `2025-01-01T00:00:00` and returns a naive
            # datetime, and it is the subtraction against an aware `now` one
            # line later that raises. Outside this `except` that traceback
            # reaches `main` with stdout already redirected and truncated,
            # which is the whole failure this guard exists to stop.
            pass
    return None


# --------------------------------------------------------------------------- #
# §2: cohorts
# --------------------------------------------------------------------------- #


def cluster_mode(c: dict) -> str:
    return "autopilot" if (c.get("autopilot") or {}).get("enabled") else "standard"


def environment_of(c: dict) -> tuple[str, str]:
    """Returns `(environment, source)` -- `source` is `"label"` when a real
    label/field supplied it, `"inferred"` when a name-token guess did, and
    `"unknown"` when neither could. §4's confidence ladder reads `source`
    to decide whether a finding rests on an inferred cohort."""
    labels = c.get("resourceLabels") or {}
    for key in ENV_LABEL_KEYS:
        val = (labels.get(key) or c.get(key) or "").lower()
        if val:
            return ENV_SYNONYMS.get(val, val), "label"
    tokens = re.split(r"[-_]", c.get("name", "").lower())
    for token in tokens:
        if token in ENV_SYNONYMS:
            return ENV_SYNONYMS[token], "inferred"
    return "unknown", "unknown"


def decide_cohort_strategy(clusters: list[dict]) -> str:
    """Which axis splits this fleet into comparable groups.

    A single resolved environment used to be enough to pick `environment` for
    the whole fleet, and on a fleet nobody has labelled that makes coverage
    *worse* than having no signal at all. Sixteen live clusters, two of them
    merely carrying `test` somewhere in their name: inference resolved those
    two, the fleet switched to environment cohorts, and both landed alone in
    cohorts of one while the other fourteen piled into `unknown`. Two clusters
    lost all coverage. Strip those two names and the same fleet cohorts by mode
    and compares all sixteen.

    So the two signals are not interchangeable. A label is the customer
    declaring how they organize their fleet and any one of them settles it. A
    name token is our guess, and a guess about a couple of clusters should not
    redraw the cohorts for everybody -- it earns the strategy only when it
    resolves enough of the fleet to be the fleet's actual naming convention.
    """
    envs = [environment_of(c) for c in clusters]
    if any(source == "label" for _, source in envs):
        return "environment"
    named = sum(1 for env, _ in envs if env != "unknown")
    if named and named * 2 >= len(clusters):
        return "environment"
    if len({c.get("_project", "") for c in clusters}) > 1:
        return "project"
    return "mode-only"


# The cohort a cluster-level facet compares in when the fleet gives no
# environment and no second project to split on: one cohort holding the whole
# fleet, both modes together. `mode-only` names the node-level counterpart,
# which still splits, so the two cannot share a label.
ALL_CLUSTERS_COHORT = "all"


def cohort_key(c: dict, strategy: str, env: str, *, standard_only: bool) -> tuple:
    """§2.3's key, which is not the same key for every facet.

    `standard_only` is the facet class, not a property of the cluster. Mode
    belongs in the key only for the eleven facets Autopilot leaves nothing to
    compare -- there, an Autopilot cluster and a Standard one genuinely are not
    peers. For the other eight the setting is configurable on both modes, so
    keying on mode buys nothing and costs the comparison: it strands every
    Autopilot cluster on a fleet with fewer than three of them in a cohort
    under the floor, which is a `limitations` string per facet, a `partial`
    run, and `resolved` pinned at 0 for as long as the fleet stays that shape.
    That is the degradation #1226 fixed, and this argument is the fix.
    """
    if strategy == "environment":
        axis: tuple = (env,)
    elif strategy == "project":
        axis = (c.get("_project", ""),)
    else:
        axis = () if standard_only else (ALL_CLUSTERS_COHORT,)
    return (cluster_mode(c), *axis) if standard_only else axis


# --------------------------------------------------------------------------- #
# §4: facet normalization. Each returns a token, or `None` to exclude the
# cluster from that facet's vote (an unreadable or inapplicable value) --
# the cluster still counts toward every other facet and stays in
# `scope.clusters`.
# --------------------------------------------------------------------------- #


def _pool_fraction(cluster: dict, get_flag: Callable[[dict], bool], exclude_pool: Callable[[dict], bool] | None = None) -> str | None:
    pools = [p for p in cluster.get("nodePools") or [] if not (exclude_pool and exclude_pool(p))]
    if not pools:
        return None
    flags = [bool(get_flag(p)) for p in pools]
    if all(flags):
        return "ALL"
    if not any(flags):
        return "NONE"
    return "SOME"


def _is_pinned_pool(p: dict) -> bool:
    """§4.8's exclusion from the `pool-autoscaling` vote: a pool whose taints
    mark it dedicated or pinned capacity, which its owner deliberately keeps a
    fixed size.

    Not "carries any taint". GKE taints some pools itself -- a GPU pool comes
    up with `nvidia.com/gpu=present:NoSchedule`, an Arm pool with
    `kubernetes.io/arch=arm64:NoSchedule`, a sandboxed pool with
    `sandbox.gke.io/runtime=gvisor:NoSchedule` -- whether or not its owner
    asked for one, and reading that as a pin drops every pool of a GPU-only,
    Arm-only or sandboxed cluster from the vote. `_pool_fraction` then returns `None`,
    `_shape_rules_out` declines (pools exist and are not Windows), and
    `unvoted_facets` writes a `limitations` sentence that `coverage_gaps` turns
    into a gap on every run: the stream never leaves `partial`, `resolved`
    stays 0, and no stale remediation pull request is ever closed. §4.0 defends
    that outcome on the grounds that "a taint is a configuration choice its
    owner can revisit", which is true of a taint an owner applied and false of
    one GKE applies for them.
    """
    taints = (p.get("config") or {}).get("taints") or []
    return any(
        str(t.get("key", "")) not in GKE_MANAGED_TAINT_KEYS
        for t in taints
        if isinstance(t, dict)
    )


def _is_windows_pool(p: dict) -> bool:
    return ((p.get("config") or {}).get("imageType") or "").upper().startswith(WINDOWS_IMAGE_PREFIX)


def norm_release_channel(c: dict) -> str | None:
    ch = (c.get("releaseChannel") or {}).get("channel") or ""
    return None if ch in ("", "UNSPECIFIED") else ch


def norm_shielded_nodes(c: dict) -> str:
    return "ON" if (c.get("shieldedNodes") or {}).get("enabled") else "OFF"


def norm_secure_boot(c: dict) -> str | None:
    return _pool_fraction(c, lambda p: ((p.get("config") or {}).get("shieldedInstanceConfig") or {}).get("enableSecureBoot"), _is_windows_pool)


def norm_integrity_monitoring(c: dict) -> str | None:
    return _pool_fraction(c, lambda p: ((p.get("config") or {}).get("shieldedInstanceConfig") or {}).get("enableIntegrityMonitoring"), _is_windows_pool)


def norm_network_policy(c: dict) -> str:
    # Two tokens, not three. Dataplane V2 and Calico are both enforcement, and
    # which engine enforces is `datapath-provider`'s own question, so splitting
    # them here cost baseline mass and bought no finding -- `_flag_off_only`
    # never flagged one engine against the other anyway. On a fleet of six
    # running three DPV2, two Calico and one with no enforcement at all, the
    # three-token read peaked at 3/6 = 0.50, missed the two-thirds floor, and
    # compared nothing; the enforcement read gives 5/6 and publishes the
    # cluster whose network policy is off.
    if (c.get("networkConfig") or {}).get("datapathProvider") == "ADVANCED_DATAPATH":
        return "ENFORCED"
    enabled = (c.get("networkPolicy") or {}).get("enabled")
    disabled_addon = ((c.get("addonsConfig") or {}).get("networkPolicyConfig") or {}).get("disabled")
    return "ENFORCED" if enabled and not disabled_addon else "OFF"


def norm_private_nodes(c: dict) -> str:
    # Both surfaces, as `norm_private_endpoint` reads both for the endpoint.
    # A cluster created through the newer control-plane-endpoints API reports
    # node privacy on `.networkConfig.defaultEnablePrivateNodes` and carries no
    # `privateClusterConfig` at all, so reading the older path alone normalized
    # a private cluster to `OFF` against a private cohort -- a base-`critical`
    # finding manufactured out of an API migration.
    pcc = c.get("privateClusterConfig") or {}
    if "enablePrivateNodes" in pcc:
        return "ON" if pcc.get("enablePrivateNodes") else "OFF"
    nc = c.get("networkConfig") or {}
    if "defaultEnablePrivateNodes" in nc:
        return "ON" if nc.get("defaultEnablePrivateNodes") else "OFF"
    return "OFF"


def norm_private_endpoint(c: dict) -> str:
    pcc = c.get("privateClusterConfig") or {}
    if "enablePrivateEndpoint" in pcc:
        return "ON" if pcc.get("enablePrivateEndpoint") else "OFF"
    cpe = (c.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}
    if "enablePublicEndpoint" in cpe:
        return "OFF" if cpe.get("enablePublicEndpoint") else "ON"
    return "OFF"


def norm_authorized_networks(c: dict) -> str:
    # GKE carries this on `masterAuthorizedNetworksConfig` or, for clusters on the
    # newer surface, `controlPlaneEndpointsConfig.ipEndpointsConfig`, and rejects
    # both at once -- so reading one field alone drifts the other's clusters OFF.
    ip_cfg = (c.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}
    for manc in (c.get("masterAuthorizedNetworksConfig"), ip_cfg.get("authorizedNetworksConfig")):
        manc = manc or {}
        if manc.get("enabled") and manc.get("cidrBlocks"):
            return "ON"
    return "OFF"


def _component_set(cfg: dict | None) -> str:
    comps = sorted(set((cfg or {}).get("enableComponents") or []))
    return ",".join(comps) if comps else "NONE"


def norm_logging_components(c: dict) -> str:
    return _component_set((c.get("loggingConfig") or {}).get("componentConfig"))


def norm_monitoring_components(c: dict) -> str:
    return _component_set((c.get("monitoringConfig") or {}).get("componentConfig"))


def norm_managed_prometheus(c: dict) -> str:
    return "ON" if ((c.get("monitoringConfig") or {}).get("managedPrometheusConfig") or {}).get("enabled") else "OFF"


def norm_binary_authorization(c: dict) -> str:
    ba = c.get("binaryAuthorization") or {}
    mode = ba.get("evaluationMode")
    if mode is not None:
        return "OFF" if mode in ("DISABLED", "EVALUATION_MODE_UNSPECIFIED") else "ON"
    return "ON" if ba.get("enabled") else "OFF"


def norm_node_autoprovisioning(c: dict) -> str:
    return "ON" if (c.get("autoscaling") or {}).get("enableNodeAutoprovisioning") else "OFF"


def norm_pool_autoscaling(c: dict) -> str | None:
    return _pool_fraction(c, lambda p: (p.get("autoscaling") or {}).get("enabled"), _is_pinned_pool)


def norm_intra_node_visibility(c: dict) -> str:
    return "ON" if (c.get("networkConfig") or {}).get("enableIntraNodeVisibility") else "OFF"


def norm_datapath_provider(c: dict) -> str:
    return "ADVANCED_DATAPATH" if (c.get("networkConfig") or {}).get("datapathProvider") == "ADVANCED_DATAPATH" else "LEGACY_DATAPATH"


def norm_label_keys(c: dict) -> str:
    keys = sorted(k for k in (c.get("resourceLabels") or {}) if not k.startswith(GOOGLE_LABEL_PREFIX))
    return ",".join(keys) if keys else "NONE"


def norm_image_type(c: dict) -> str | None:
    types = set()
    for p in c.get("nodePools") or []:
        if _is_windows_pool(p):
            continue
        img = ((p.get("config") or {}).get("imageType") or "").upper()
        if img:
            types.add(img.replace(IMAGE_TYPE_RUNTIME_SUFFIX, ""))  # a rename, not a divergence
    # `None` and not a `NONE` token: this facet is compared with
    # `_flag_not_superset`, and the empty set is a subset of every baseline, so
    # a cluster with no Linux pool to read scored as missing every image type
    # its cohort runs. The remediation for that finding is to set an image type
    # on a node pool that does not exist. The other three pool facets abstain
    # on the same shape -- `_pool_fraction` returns `None` -- and
    # `unvoted_facets` declares it as the node-pool surface Autopilot lacks.
    return ",".join(sorted(types)) if types else None


def norm_database_encryption(c: dict) -> str:
    return "ENCRYPTED" if (c.get("databaseEncryption") or {}).get("state") == "ENCRYPTED" else "DECRYPTED"


def _flag_ne(observed: str, baseline: str) -> bool:
    return observed != baseline


# Every two-token facet spells "the control is not in force" one of two ways.
_DEGRADED = frozenset({"OFF", "DECRYPTED"})


def _flag_off_only(observed: str, baseline: str) -> bool:
    return observed in _DEGRADED and baseline not in _DEGRADED


def _flag_less_only(observed: str, baseline: str) -> bool:
    """`_flag_off_only` for the three-token `_pool_fraction` facets.

    Flags a cluster that covers fewer of its pools than the cohort does, and
    stays quiet when it covers more. The facets scored this way state an impact
    ("nodes boot unverified", "cannot absorb load the way its peers do") and
    carry a remediation that turns the feature *on*, so in the other direction
    the finding reads backwards and cannot be closed: enabling the feature on
    the remaining pools moves the token to ALL, which still differs from a NONE
    baseline, so the same finding returns on the next run.
    """
    rank = {"NONE": 0, "SOME": 1, "ALL": 2}
    return rank.get(observed, 0) < rank.get(baseline, 0)


def _tokens(value: str) -> set[str]:
    return set(value.split(",")) if value != "NONE" else set()


def _flag_not_superset(observed: str, baseline: str) -> bool:
    return not _tokens(observed).issuperset(_tokens(baseline))


def _missing_tokens(observed: str, baseline: str) -> list[str]:
    """The baseline tokens the outlier does not carry.

    `_flag_not_superset` computes this difference to decide and throws it away,
    leaving the model to re-derive it from `observed` and `baseline` in order to
    write a title. It got that derivation wrong on the live fleet:
    `drift-peer-std-4` observed `NONE` against a `SYSTEM_COMPONENTS,WORKLOADS`
    baseline and published as "logging component set missing WORKLOADS relative
    to its cohort" -- one of the two missing components, reading as though system
    logging still worked. The cluster carried `loggingService: none` and logged
    nothing at all. Two lines below, the same finding's excerpt said
    `observed: NONE` and its impact line said "no logging component config at
    all", so the title contradicted its own evidence, and the title is the line a
    reader sees first and the one the ledger's finding table shows.

    Meaningful only for the `_flag_not_superset` facets, whose tokens are
    comma-joined sets; the call site passes `None` for the rest rather than
    emitting a line whose set framing does not apply to an `ON`/`OFF` facet.
    """
    return sorted(_tokens(baseline) - _tokens(observed))


def _logging_severity(observed: str) -> str:
    parts = observed.split(",") if observed != "NONE" else []
    return "major" if "SYSTEM_COMPONENTS" not in parts else "minor"


class Facet(NamedTuple):
    slug: str
    field_path: str
    base_severity: str | Callable[[str], str]
    # Node-level or Google-fixed on Autopilot: #1226's eleven. Two things
    # follow from the one flag, and they have to travel together. The facet is
    # not computed on an Autopilot cluster and is declared
    # `checks_not_applicable` there instead; and it is the only class of facet
    # whose cohort key carries the mode, because it is the only class where an
    # Autopilot cluster and a Standard one are not comparable. Every other
    # facet is configurable on both modes and is compared across them.
    standard_only: bool
    normalize: Callable[[dict], str | None]
    should_flag: Callable[[str, str], bool]


FACETS: tuple[Facet, ...] = (
    Facet("release-channel", ".releaseChannel.channel", "minor", False, norm_release_channel, _flag_ne),
    Facet("shielded-nodes", ".shieldedNodes.enabled", "major", True, norm_shielded_nodes, _flag_off_only),
    Facet("secure-boot", ".nodePools[].config.shieldedInstanceConfig.enableSecureBoot", "major", True, norm_secure_boot, _flag_less_only),
    Facet("integrity-monitoring", ".nodePools[].config.shieldedInstanceConfig.enableIntegrityMonitoring", "minor", True, norm_integrity_monitoring, _flag_less_only),
    Facet("network-policy", ".networkConfig.datapathProvider / .networkPolicy.enabled", "major", False, norm_network_policy, _flag_off_only),
    Facet("private-nodes", ".privateClusterConfig.enablePrivateNodes / .networkConfig.defaultEnablePrivateNodes", "critical", False, norm_private_nodes, _flag_off_only),
    Facet("private-endpoint", ".privateClusterConfig.enablePrivateEndpoint / .controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint", "major", False, norm_private_endpoint, _flag_off_only),
    Facet("authorized-networks", ".masterAuthorizedNetworksConfig / .controlPlaneEndpointsConfig.ipEndpointsConfig.authorizedNetworksConfig", "critical", False, norm_authorized_networks, _flag_off_only),
    Facet("logging-components", ".loggingConfig.componentConfig.enableComponents", _logging_severity, True, norm_logging_components, _flag_not_superset),
    Facet("monitoring-components", ".monitoringConfig.componentConfig.enableComponents", "minor", True, norm_monitoring_components, _flag_not_superset),
    Facet("managed-prometheus", ".monitoringConfig.managedPrometheusConfig.enabled", "minor", True, norm_managed_prometheus, _flag_off_only),
    Facet("binary-authorization", ".binaryAuthorization.evaluationMode", "major", False, norm_binary_authorization, _flag_off_only),
    Facet("node-autoprovisioning", ".autoscaling.enableNodeAutoprovisioning", "minor", True, norm_node_autoprovisioning, _flag_off_only),
    Facet("pool-autoscaling", ".nodePools[].autoscaling.enabled", "minor", True, norm_pool_autoscaling, _flag_less_only),
    Facet("intra-node-visibility", ".networkConfig.enableIntraNodeVisibility", "minor", True, norm_intra_node_visibility, _flag_ne),
    Facet("datapath-provider", ".networkConfig.datapathProvider", "major", True, norm_datapath_provider, _flag_ne),
    Facet("label-keys", ".resourceLabels", "minor", False, norm_label_keys, _flag_not_superset),
    Facet("image-type", ".nodePools[].config.imageType", "minor", True, norm_image_type, _flag_not_superset),
    Facet("database-encryption", ".databaseEncryption.state", "critical", False, norm_database_encryption, _flag_off_only),
)
FACETS_BY_SLUG = {f.slug: f for f in FACETS}


def _path_private_nodes(c: dict) -> str | None:
    if "enablePrivateNodes" in (c.get("privateClusterConfig") or {}):
        return ".privateClusterConfig.enablePrivateNodes"
    if "defaultEnablePrivateNodes" in (c.get("networkConfig") or {}):
        return ".networkConfig.defaultEnablePrivateNodes"
    return None


def _path_private_endpoint(c: dict) -> str | None:
    if "enablePrivateEndpoint" in (c.get("privateClusterConfig") or {}):
        return ".privateClusterConfig.enablePrivateEndpoint"
    if "enablePublicEndpoint" in ((c.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}):
        return ".controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint"
    return None


def _path_authorized_networks(c: dict) -> str | None:
    if c.get("masterAuthorizedNetworksConfig") is not None:
        return ".masterAuthorizedNetworksConfig"
    ip_cfg = (c.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}
    if ip_cfg.get("authorizedNetworksConfig") is not None:
        return ".controlPlaneEndpointsConfig.ipEndpointsConfig.authorizedNetworksConfig"
    return None


# §4.0: "Where two paths are plausible (a field that migrated between API
# versions), read the first present and record which one in the excerpt."
# `Facet.field_path` names both, joined, which is what the facet table owes a
# reader and not what a finding does: an operator checking `observed: OFF`
# against a cluster needs the key it came from, and on these three facets the
# two keys are set on disjoint sets of clusters.
PATH_RESOLVERS: dict[str, Callable[[dict], str | None]] = {
    "private-nodes": _path_private_nodes,
    "private-endpoint": _path_private_endpoint,
    "authorized-networks": _path_authorized_networks,
}


def read_path(facet: Facet, c: dict) -> str:
    """The path `facet.normalize` actually read on this cluster.

    Falls back to the facet's own `field_path` -- the single-surface facets,
    and a dual-surface facet where neither key is present, whose token is the
    normalizer's absent-field default and belongs to no one path."""
    resolver = PATH_RESOLVERS.get(facet.slug)
    return (resolver(c) if resolver else None) or facet.field_path


# --------------------------------------------------------------------------- #
# §3: baseline, confidence, severity ladder
# --------------------------------------------------------------------------- #


def facet_tokens(facet: Facet, members: list[dict]) -> dict[tuple, str]:
    """Each member's token for this facet, keyed by `ckey`, minus the members
    whose normalizer returned `None` -- §3.2's "readable values", which is the
    `n` the baseline is computed over.

    Shared by the vote and by `unvoted_facets`, for the same reason
    `cohort_layout` is shared by the vote and by `cohort_limitations`: the
    second has to agree with the first about who voted, and a disagreement
    shows up as a check that is in neither the roster nor the gaps.
    """
    tokens: dict[tuple, str] = {}
    for c in members:
        token = facet.normalize(c)
        if token is not None:
            tokens[ckey(c)] = token
    return tokens


def compute_baseline(tokens: dict[str, str]) -> tuple[str, int, int, float] | None:
    n = len(tokens)
    if n < COHORT_FLOOR:
        return None
    counts = Counter(tokens.values())
    t_star, m = counts.most_common(1)[0]
    r = m / n
    if r < BASELINE_MIN_RATIO:
        return None
    return t_star, m, n, r


def apply_severity_ladder(base: str, r: float, k: int, inferred: bool) -> tuple[str | None, list[str]]:
    steps, applied = 0, []
    if r < LADDER_RATIO_STEP_1:
        steps += 1
        applied.append(f"r={r:.2f}<{LADDER_RATIO_STEP_1:.2f}")
    if r < LADDER_RATIO_STEP_2:
        steps += 1
        applied.append(f"r={r:.2f}<{LADDER_RATIO_STEP_2:.2f}")
    if k >= LADDER_SPLIT_K:
        steps += 1
        applied.append(f"k={k}>={LADDER_SPLIT_K}")
    if inferred:
        steps += 1
        applied.append("inferred environment")
    idx = SEVERITY_LEVELS.index(base) + steps
    if idx >= len(SEVERITY_LEVELS):
        return None, applied
    return SEVERITY_LEVELS[idx], applied


def build_excerpt(field_path: str, t_star: str, m: int, n: int, cohort_label: str, peer_names: list[str], observed: str, sev: str, base_sev: str, downgrades: list[str], r: float, missing: list[str] | None = None, observed_path: str | None = None) -> str:
    peers = peer_names[:EXCERPT_PEER_NAMES]
    more = f", +{len(peer_names) - EXCERPT_PEER_NAMES} more" if len(peer_names) > EXCERPT_PEER_NAMES else ""
    downgrade_text = ", ".join(downgrades) if downgrades else "none"
    missing_line = f"missing: {', '.join(missing)}\n" if missing else ""
    # Only where it says something the `baseline:` line did not. On the three
    # dual-surface facets `field_path` names both keys and this names the one
    # this cluster carried; everywhere else the two are the same string and
    # printing it twice is noise in a clipped excerpt.
    read_from = f"  (read from {observed_path})" if observed_path and observed_path != field_path else ""
    return (
        f"baseline: {field_path}={t_star} in {m}/{n} clusters of cohort {cohort_label}\n"
        f"peers: {', '.join(peers)}{more}\n"
        f"observed: {observed}{read_from}\n"
        f"{missing_line}"
        f"consensus: {r:.2f} -> severity {sev} (base {base_sev}, {downgrade_text})"
    )


IMPACT = {
    "release-channel": "This cluster receives control-plane patches on a different schedule than its cohort.",
    "shielded-nodes": "Nodes boot unverified where every peer verifies them.",
    "secure-boot": "Nodes boot unverified where every peer verifies them.",
    "integrity-monitoring": "Node boot integrity is unmonitored where every peer monitors it.",
    "network-policy": "Pod-to-pod traffic is unrestricted here where peers segment it.",
    "private-nodes": "Node surface is exposed here that every peer keeps private.",
    "private-endpoint": "The control plane is reachable here in a way every peer keeps private.",
    "authorized-networks": "The control plane accepts connections from an unrestricted range here where peers restrict it.",
    "logging-components": "This cluster is invisible to fleet dashboards and alerts built on the peers' component set.",
    "monitoring-components": "This cluster is invisible to fleet dashboards and alerts built on the peers' component set.",
    "managed-prometheus": "This cluster's metrics are not queryable the way its peers' are.",
    "binary-authorization": "Unsigned or unattested images can run here where peers block them.",
    "node-autoprovisioning": "This cluster cannot absorb load the way its peers do without manual intervention.",
    "pool-autoscaling": "This cluster cannot absorb load the way its peers do without manual intervention.",
    "intra-node-visibility": "This cluster emits different flow telemetry than its cohort.",
    "datapath-provider": "This cluster enforces network policy through a different engine than its cohort.",
    "label-keys": "This cluster drops out of cost attribution and label-scoped queries its peers appear in.",
    UNLABELLED_SLUG: (
        "This cluster is absent from every environment-scoped cost report, policy binding and "
        "alert route its peers appear in -- and, because every other check here compares a cluster "
        "against a cohort it cannot join, it is also the one cluster in the fleet whose "
        "configuration this audit never examines, on this run or any future one."
    ),
    "image-type": "This cluster's nodes carry a different patch cadence and hardening baseline than its peers.",
    "database-encryption": "Secrets in this cluster's etcd are not wrapped with the customer-managed key every peer uses.",
    UNCOHORTED_SLUG: "This cluster diverges from its cohort on so many facets that its cohort labelling, not each facet, is likely wrong.",
}


def _emit(slug: str, leaf_name: str, excerpt: str, severity: str) -> dict:
    """One candidate. `leaf_name` is the cluster's bare GKE name, not the
    qualified target.

    `derive_finding_id` keys on `(check, cluster, namespace, object)` and the
    entry this candidate hangs under already supplies the qualified cluster, so
    `Cluster/<project>/<location>/<name>` would spell the location twice in one
    id. That is not merely redundant: the id has a 100-character ceiling and
    `_shorten_id` eats the longest segment first, so the second copy pushed the
    pair over it and took the cluster's own name off the end --
    `authorized-networks.agentic-harness-demo-us-central1-a._.cluster-agentic-harness-demo-us-cen-3284a2`
    names no cluster a reader or a `/remediate` caller can recognise. The leaf
    keeps the id under the ceiling and readable, and loses no identity, because
    the segment before it already says which project and location this is.
    """
    return {
        "check": slug,
        "namespace": "",
        "object": f"Cluster/{leaf_name}",
        "severity": severity,
        "excerpt": excerpt,
        "impact": IMPACT[slug],
        "needs_triage": None,
    }


def qualify_targets(clusters: list[dict]) -> None:
    """Give every cluster the name the manifest will call it by, in `_target`.

    A GKE cluster name is unique inside one project *and location* and nothing
    wider, but the manifest is fleet-wide and everything downstream keys on the
    name it carries: `audit_report._vouching_clusters` builds a dict of it, so two
    clusters called `seeded-a` in two projects collapse into whichever entry
    the loop wrote last. The document that follows either names the cluster
    that lost (cross-checked against a manifest that no longer holds it, and
    rejected) or covers both with one row, which publishes an all-clear over a
    cluster nobody compared. On the evaluation pool that is the ordinary case
    rather than a corner: every project there carries `seeded-a`, `-b` and
    `-c`.

    Every cluster is qualified, not only one whose name collides today. Two
    reasons, and the second is the one that costs a ledger:

    - A name is unique per project *and location*, not per project. `ckey`
      already says so by keying on all three, and `prod` in `us-central1`
      beside `prod` in `europe-west1` is one project's ordinary multi-region
      shape. Qualifying by project alone leaves that pair collapsed.
    - Qualifying only a colliding name makes a cluster's identity depend on
      what the *rest* of the fleet contains. `Cluster/seeded-a` becomes
      `Cluster/acme/seeded-a` the week a second project gains a `seeded-a`,
      and reverts when that cluster is deleted; `finish` derives a finding's
      id from its cluster and object, so the old id vanishes from the
      document, is announced resolved, and the same drift is refiled as new.
      That is §3.7's "no unstable identity" red line, broken by the collector
      rather than by the model.

    The cost is a longer name in `peers:` lines and finding objects. It buys a
    target that is unique by construction and moves for no reason short of the
    cluster being rebuilt somewhere else -- and the three segments are exactly
    what `gcloud container clusters describe` asks for, so the qualified form
    is no harder to act on than the bare one. `commands[]` carries the real
    `--project` regardless: this renames the target, not the call.
    """
    for c in clusters:
        name = c.get("name", "")
        project = c.get("_project", "")
        location = c.get("location") or c.get("zone") or ""
        parts = [p for p in (project, location) if p]
        if parts:
            c["_target"] = QUALIFIED_TARGET_SEPARATOR.join([*parts, name])


def target_name(c: dict) -> str:
    """What to call this cluster in the manifest, in a candidate's `object`,
    and in a `peers:` list. `qualify_targets` sets `_target` on every cluster
    whose project and location are known; the bare name is the fallback for a
    listing that carried neither."""
    return c.get("_target") or c.get("name", "")


def ckey(c: dict) -> tuple[str, str, str]:
    """A cluster's identity inside this collector.

    Not its name. Cluster names are project-scoped in GKE, so `prod` in two
    projects is two clusters -- and this is the one collector that sweeps every
    project in the fleet by design, which is exactly where the collision lands.
    Keying the per-cluster dicts by bare name merged the pair three ways:
    `checks_run` accumulated both clusters' facets into one list, which §6's
    validator rejects as a duplicate `checks_run` entry and which fails the run;
    `candidates` handed each manifest entry the other cluster's findings, each
    published under its own `Cluster/<name>` object and so indistinguishable;
    and `outlier_facet_count` summed the two, which trips §3.6's six-facet
    split-cluster guard on a pair of clusters that each diverge on three.
    `ineligible` and `env_of` were last-write-wins on top of that.

    The finding's `object` stays `Cluster/<name>` -- §6 specifies that, and the
    harness derives identity from `cluster` alongside it.
    """
    return (c.get("_project", ""), c.get("location") or c.get("zone") or "", c.get("name", ""))


class Layout(NamedTuple):
    """§1's eligibility and §2's cohorting, keyed by `ckey` where per-cluster.

    Two cohortings, not one, because §2.3 keys the eleven node-level facets on
    mode and the other eight across it. `cohorts_for` is how a caller picks;
    reaching for the wrong member is the whole failure mode this type exists to
    make visible, so nothing here is called `cohorts`.
    """

    ineligible: dict[tuple, str]
    node_cohorts: dict[tuple, list[dict]]
    cluster_cohorts: dict[tuple, list[dict]]
    env_of: dict[tuple, tuple[str, str]]
    strategy: str

    def cohorts_for(self, standard_only: bool) -> dict[tuple, list[dict]]:
        return self.node_cohorts if standard_only else self.cluster_cohorts

    def key_for(self, c: dict, standard_only: bool) -> tuple:
        return cohort_key(c, self.strategy, self.env_of[ckey(c)][0], standard_only=standard_only)

    def cohort_of(self, c: dict, standard_only: bool) -> list[dict]:
        return self.cohorts_for(standard_only).get(self.key_for(c, standard_only)) or []


def cohort_layout(clusters: list[dict], *, now: datetime) -> Layout:
    """Shared by the vote and by `cohort_limitations`, which has to agree with
    it exactly: a cluster the vote skipped and the limitations did not explain
    is the silent-clean failure this stream is most prone to.
    """
    ineligible: dict[tuple, str] = {}
    eligible: list[dict] = []
    for c in clusters:
        why = cluster_eligibility(c, now=now)
        if why is None:
            eligible.append(c)
        else:
            ineligible[ckey(c)] = why

    strategy = decide_cohort_strategy(eligible)
    env_of: dict[tuple, tuple[str, str]] = {ckey(c): environment_of(c) for c in eligible}

    node_cohorts: dict[tuple, list[dict]] = {}
    cluster_cohorts: dict[tuple, list[dict]] = {}
    for c in eligible:
        env, _ = env_of[ckey(c)]
        node_cohorts.setdefault(cohort_key(c, strategy, env, standard_only=True), []).append(c)
        cluster_cohorts.setdefault(cohort_key(c, strategy, env, standard_only=False), []).append(c)
    return Layout(ineligible, node_cohorts, cluster_cohorts, env_of, strategy)


def cohort_limitations(clusters: list[dict], *, now: datetime) -> dict[tuple, str]:
    """§2.4's `limitations` sentence for every cluster no facet could compare.

    Drift is comparative, so a cluster with too few peers has nothing to drift
    from and every facet abstains for it. That is a legitimate outcome and the
    SOP says so — but until this function existed the manifest recorded it as
    `outcome: "collected"` with an empty `commands` list, and `"collected"`
    tells the model that every applicable check already ran and it must not
    re-run the cluster by hand. A live four-cluster fleet split into cohorts of
    2, 1 and 1, every one under the floor, and the collector reported four
    clusters fully collected four seconds after it started, having compared
    nothing.

    Two exclusions, both named by the SOP. `cluster_eligibility` already
    returns its sentence and only ever needed plumbing; the undersized-cohort
    sentence is §2.4's own wording.

    A cluster whose cohort reached the floor and then lost individual facets
    is `unvoted_facets`' business rather than this one's: the sentence here is
    "nothing compared this cluster", and a cluster compared on fifteen facets
    out of nineteen needs the four named instead.

    §2.3 gives a cluster two cohorts, so there are two sentences. An undersized
    *cluster-level* cohort is the one above: no facet of either class compared
    it. An undersized *node-level* cohort on a cluster whose cluster-level
    cohort is fine is the narrower case #1226 names -- the eight configurable
    facets ran and the eleven node-level ones did not -- and it is a
    `limitations` string rather than a `checks_not_applicable` entry because
    those eleven could have been compared and were not.

    An Autopilot cluster never earns the narrow sentence. Its eleven are
    already declared inapplicable by `autopilot_not_applicable`, so an
    undersized Autopilot node cohort leaves nothing uncompared and saying it
    did would put a gap in the denominator that the mode, not the fleet's
    size, had already settled.
    """
    layout = cohort_layout(clusters, now=now)
    out = dict(layout.ineligible)
    env_of = layout.env_of
    labelled = sum(1 for _, source in env_of.values() if source == "label")
    for key, members in layout.cluster_cohorts.items():
        if len(members) >= COHORT_FLOOR:
            continue
        label = "/".join(str(k) for k in key)
        # A floored-out cohort is 1 or 2 members, so this line reads "only 1
        # comparable clusters" in the single-member case that kube-agents-host
        # hits on every run -- the most-read sentence the stream emits.
        noun = "cluster" if len(members) == 1 else "clusters"
        for c in members:
            out[ckey(c)] = (
                f"cohort {label} has only {len(members)} comparable {noun} "
                f"(minimum {COHORT_FLOOR}), no facet compared"
                f"{_unlabelled_cause(key, labelled, len(env_of), layout, c)}"
            )
    node_level = sum(1 for f in FACETS if f.standard_only)
    for key, members in layout.node_cohorts.items():
        if len(members) >= COHORT_FLOOR or key[0] == "autopilot":
            continue
        label = "/".join(str(k) for k in key)
        noun = "cluster" if len(members) == 1 else "clusters"
        for c in members:
            if ckey(c) in out:
                continue
            out[ckey(c)] = (
                f"cohort {label} has only {len(members)} comparable {noun} "
                f"(minimum {COHORT_FLOOR}); {node_level} node-level facets uncompared"
            )
    return out


def joinable_environments(
    cohorts: dict[tuple, list[dict]], *, mode: str | None = None
) -> list[tuple[str, int]]:
    """The environment values that would actually put a cluster into a cohort
    that reaches the floor, commonest first.

    Shared by the coverage-gap sentence and by `no-environment-label`'s
    finding, which is the whole point of it being a function. The gap says
    "only `environment=test` would reach the floor here" and the finding
    prescribes a label; computing that value twice is how the audit ends up
    telling an operator to apply a label its own gap sentence says will not
    work.

    `COHORT_FLOOR - 1` and not `COHORT_FLOOR`, because the cluster being
    advised is the one that would join: a value held by two peers reaches
    three with it.

    `mode` picks which of §2.3's two cohortings is being asked about, and the
    caller passes the cohort dict to match. Omitted, this reads the
    cluster-level cohorts, whose keys carry no mode: a peer of either mode
    counts toward the label's floor, and it should, because the eight facets
    that key on `(environment)` are the ones configurable on Autopilot and
    Standard alike. Filtering those by mode is what made the advice narrower
    than the comparison it was advising about, which is the defect #1226 fixed.

    Passed a mode, it reads the node-level cohorts, whose keys are
    `(mode, environment)`, and counts only peers at that mode -- because the
    eleven facets those cohorts carry are compared within a mode and a label
    that reaches the floor across both modes closes nothing for them. The two
    answers differ on any fleet whose unlabelled clusters straddle the split,
    and a cluster can need a value that satisfies both.
    """
    return sorted(
        ((k[-1], len(m)) for k, m in cohorts.items()
         if k[-1:] != ("unknown",) and len(m) >= COHORT_FLOOR - 1
         and (mode is None or k[0] == mode)),
        key=lambda t: (-t[1], t[0]),
    )


def joinable_for_cluster(layout: Layout, c: dict, *, cluster_short: bool, node_short: bool) -> list[tuple[str, int]]:
    """The environment values that close *every* cohort gap this cluster has,
    commonest first -- `joinable_environments` resolved against §2.3's two
    cohortings at once.

    A value has to satisfy each axis the cluster is short on, so where both are
    short that is an intersection, and the intersection can be empty while
    either axis alone offers a value. Prescribing a label off one axis is a
    remedy that closes half the gap: the eight configurable facets start
    comparing and the eleven node-level ones still do not, and the operator who
    applied it sees the same coverage gap on the next run.

    Shared by §4.14's finding and by `_unlabelled_cause`'s remedy clause for the
    same reason `joinable_environments` is shared by anything -- the two say the
    same thing about the same cluster in the same report, and computing it twice
    is how one comes to prescribe a value the other withholds.
    """
    joinable = joinable_environments(layout.cluster_cohorts) if cluster_short else []
    if node_short:
        by_mode = joinable_environments(layout.node_cohorts, mode=cluster_mode(c))
        joinable = (
            [(v, n) for v, n in joinable if v in {w for w, _ in by_mode}]
            if cluster_short
            else by_mode
        )
    return joinable


def _unlabelled_cause(key: tuple, labelled: int, total: int, layout: Layout, c: dict) -> str:
    """Why the `unknown` cohort floored out, when the rest of the fleet did not.

    The floor sentence is true and gives the reader nothing to do with it. On
    the live fleet fifteen of sixteen clusters carry `environment=test` and
    kube-agents-host carries no environment label at all, so it cohorts alone
    under §2.3's rule that `unknown` never merges into a named cohort -- and
    the install's own host cluster is the one cluster this stream can never
    compare, on this run or any future one. Nothing in "cohort
    standard/unknown has only 1 comparable cluster" says that a label is the
    difference, so the gap reads as a quirk of fleet size and gets waited out
    rather than fixed.

    Counted by label rather than by resolved environment, because the sentence
    claims the other clusters carry one. Under the inferred strategy they do
    not -- their environment came from a name token -- and "12 of 16 do" would
    then be false. Counting the source keeps it literally true, and doubles as
    the guard on the `unknown` test: `decide_cohort_strategy` returns
    `environment` for any label at all, so a non-zero count means the key's
    last element really is an environment and not a project that happens to be
    named `unknown`.

    Naming the values is the rest of it. "Label it to compare it" is true of
    exactly one value on most fleets and silently false of the rest: a
    cluster-level cohort key is `(environment)`, so a label only reaches the
    floor if `COHORT_FLOOR - 1` clusters already carry it.
    kube-agents-host sits on a fleet whose ten other clusters are all `test`,
    so `environment=test` compares it and `prod`, `platform` and `hub` -- the
    three an operator would actually reach for on an install's own host
    cluster -- each open a new cohort of one and change nothing. The
    second run then says less than the first: `prod` is not `unknown`, so the
    guard above drops this whole clause and the operator who did what the
    sentence asked is told only that the cohort is too small. Advice whose
    likeliest application is a no-op belongs with the sizing findings the cost
    SOP's §3.7 withholds for the same reason, so the sentence names the values that work
    and says plainly that the others do not.

    "Work" means both of §2.3's cohorts, which is why this takes the whole
    `Layout` and the cluster rather than one cohort dict. A Standard cluster
    whose cluster-level cohort floored out has a `(standard, unknown)` cohort
    that floored out underneath it -- the node-level cohort is a subset -- so a
    value read off the cluster-level cohorts alone can be one that starts the
    eight configurable facets comparing and leaves the eleven node-level ones
    exactly as they were. §4.14's finding already intersects the two through
    `joinable_for_cluster`; this clause calls the same function, so the
    `limitations` sentence cannot name a value the finding beside it withheld.
    """
    if key[-1:] != ("unknown",) or not labelled:
        return ""
    # Reached from the cluster-level loop, so that cohort is short by
    # construction. The node-level one is asked on its own terms: on Autopilot
    # its eleven facets are inapplicable, so its floor withholds nothing and no
    # label is being asked to close it.
    mode = cluster_mode(c)
    node_short = mode != "autopilot" and len(layout.cohort_of(c, True)) < COHORT_FLOOR
    joinable = joinable_for_cluster(layout, c, cluster_short=True, node_short=node_short)
    if joinable:
        named = " or ".join(f"`environment={v}`" for v, _ in joinable)
        peers = ", ".join(f"{n} other cluster{'' if n == 1 else 's'} carry `{v}`"
                          for v, n in joinable)
        remedy = (
            f" Only {named} would reach the floor here -- {peers}. Any other"
            " value opens a new cohort of one and compares nothing, so a label"
            " chosen to describe this cluster rather than to match its peers"
            " leaves the coverage gap exactly as it is."
        )
    elif node_short and joinable_environments(layout.cluster_cohorts):
        # A value reaches the cluster-level floor and none reaches both. Saying
        # only "no value reaches the floor" would be read against the cohort the
        # sentence just named and look false, so the clause names the half that
        # works and the half that does not -- an operator who applies it anyway
        # gets the eight and should know the eleven stay where they are.
        named = " or ".join(
            f"`environment={v}`" for v, _ in joinable_environments(layout.cluster_cohorts)
        )
        remedy = (
            f" {named} would reach this cohort's floor, but no value reaches"
            f" the floor of the `{mode}` cohort the {sum(1 for f in FACETS if f.standard_only)}"
            " node-level facets are compared within, so no single label closes"
            " both gaps and this cluster stays partly uncompared until the"
            " fleet grows."
        )
    else:
        remedy = (
            f" No environment value on this fleet has the {COHORT_FLOOR - 1}"
            " other clusters a label would need to reach the floor, so"
            " no label compares this cluster until the fleet grows."
        )
    return (
        f"; it carries no environment label while {labelled} of {total} do,"
        " and an unlabelled cluster never joins a named cohort."
        + remedy
    )


def unlabelled_environment_candidates(clusters: list[dict], *, now: datetime) -> tuple[dict[tuple, list[str]], dict[tuple, list[dict]], dict[tuple, list[dict]]]:
    """§4.14's `no-environment-label`, as `(checks_run, candidates,
    not_applicable)` keyed by `ckey` -- the one check here that is not a
    `Facet`.

    Every `Facet` is comparative, so `compute_drift` only evaluates one inside
    a cohort that reached `COHORT_FLOOR`, and under the `environment` strategy
    neither of §2.3's two keys -- `(environment)` for the eight configurable
    facets, `(mode, environment)` for the eleven node-level ones -- is one an
    unlabelled cluster can match, because §2.3 keeps `unknown` out of every
    named cohort. The cluster
    with the fleet's one divergent label set is therefore the one cluster
    `label-keys` structurally cannot see, and the same holds for the other
    eighteen facets. `cohort_limitations` already says so in a sentence, and a
    sentence in `limitations` opens no pull request; this publishes the same
    conclusion as a finding with the label to set in it.

    Three conditions, each of which the finding's own claims rest on:

    - **The fleet cohorts by environment.** Under `project` or `mode-only` no
      cohort key holds an environment at all, so the cluster is already being
      compared and a label would change nothing about that. The check does not
      apply -- and says so in `checks_not_applicable` for every cluster on the
      fleet rather than staying silent, because a slug missing from
      `checks_run` is indistinguishable from a check nobody got to, which is
      the coverage gap `autopilot_not_applicable` exists to prevent.
    - **This cluster resolves to `unknown`.** An `inferred` cluster joins a
      cohort on a name token; that membership is a guess and §3.5 downgrades
      what rests on it, but it is not this defect and "joins no cohort" would
      be false of it.
    - **One of its two cohorts is under the floor.** Both are asked, because
      §2.3 draws both from the environment and a missing label can leave
      either short. Enough unlabelled clusters pile into `(unknown)` to reach
      three and they compare each other on the eight, which is coverage this
      finding would otherwise be claiming is missing -- but `(mode, unknown)`
      can still be short underneath it, and the eleven it carries compare
      nothing. On an Autopilot cluster the node-level cohort is not asked:
      those eleven are `checks_not_applicable` there, so its floor withholds
      nothing.

    And it publishes only where `joinable_environments` names a value that
    reaches the floor. Where none does, no label closes the gap, so the
    finding's remediation would be a no-op -- the same reason the cost SOP's
    §3.7 withholds a sizing finding whose own prescription changes nothing. The check still ran
    and still counts toward §6's denominator; `limitations` keeps the sentence.
    """
    checks_run: dict[tuple, list[str]] = {}
    candidates: dict[tuple, list[dict]] = {}
    not_applicable: dict[tuple, list[dict]] = {}

    layout = cohort_layout(clusters, now=now)
    cohorts, env_of, strategy = layout.cluster_cohorts, layout.env_of, layout.strategy
    if strategy != "environment":
        reason = (
            f"§2.3 cohorts this fleet by `{strategy}`, not by environment, so no cohort key holds "
            "an environment and every cluster is compared regardless of its labels -- an "
            "environment label would not change which clusters this one is measured against."
        )
        # A cluster §1 excluded is not compared at all, so the sentence above
        # would contradict its `limitations`; it gets nothing here, as it does
        # under `environment` below.
        for c in clusters:
            if ckey(c) in env_of:
                not_applicable[ckey(c)] = [{"check": UNLABELLED_SLUG, "reason": reason}]
        return checks_run, candidates, not_applicable

    labelled = sum(1 for _, source in env_of.values() if source == "label")
    for c in clusters:
        key = ckey(c)
        if key not in env_of:
            continue
        mode = cluster_mode(c)
        checks_run[key] = [UNLABELLED_SLUG]
        _env, source = env_of[key]
        if source != "unknown":
            continue
        # §2.3 draws two cohorts and a missing label can leave either one
        # short, so both are asked. The cluster-level cohort merges the modes,
        # which means a fleet with three unlabelled clusters spread across the
        # split reaches the floor there and compares the eight configurable
        # facets against a bag of clusters that have nothing in common but the
        # absent label -- while the node-level cohort, keyed `(mode, unknown)`,
        # is still short and the other eleven compare nothing. Reading only the
        # cluster-level cohort made the check silent on exactly that fleet,
        # which is the larger half of the gap and the one a label still closes.
        node_short = mode != "autopilot" and len(layout.cohort_of(c, True)) < COHORT_FLOOR
        cluster_short = len(layout.cohort_of(c, False)) < COHORT_FLOOR
        if not (node_short or cluster_short):
            continue
        # An Autopilot cluster's eleven are `checks_not_applicable`, so its
        # node-level cohort being short is not a gap and no label would be
        # closing one; `node_short` is false for it above rather than here, so
        # the value search below never has to satisfy a constraint that does
        # not apply. Where it does, `joinable_for_cluster` is what makes a
        # value satisfy both axes rather than one; publishing off one half is
        # the no-op §3.7 of the cost SOP withholds.
        joinable = joinable_for_cluster(layout, c, cluster_short=cluster_short, node_short=node_short)
        if not joinable:
            continue
        value, peers = joinable[0]
        # `labelled` counts the *source* of each environment, not the resolved
        # value: §2.3 picks the `environment` strategy off any label anywhere on
        # the fleet, so every other cluster can have reached its cohort by a
        # name token instead. "0 of 7 eligible clusters on this fleet do",
        # printed beside a finding about the one cluster that does not, is a
        # sentence arguing against itself -- and `_unlabelled_cause` drops its
        # own version of the clause on exactly this count, so the two halves of
        # the audit contradicted each other on the same fleet.
        others = (
            f"while {labelled} of {len(env_of)} eligible clusters on this fleet do"
            if labelled
            else f"and nor does any of the other {len(env_of) - 1} eligible clusters -- "
            f"§2.3 still cohorts this fleet by environment, because their names resolve to one"
        )
        # Same distinction one sentence later: a peer whose environment was
        # inferred does not *carry* the label the finding is telling the
        # operator to set. Setting it still works -- a cohort key holds the
        # resolved value, however it resolved -- so only the verb changes.
        # The peers the sentence names are the ones whose cohort the label
        # would reach. Where the gap is node-level alone, that is the peers at
        # this cluster's mode -- naming the cluster-level count there would
        # quote a number that does not reach the floor being described.
        peer_members = (
            layout.node_cohorts.get((mode, value)) or []
            if node_short and not cluster_short
            else cohorts.get((value,)) or []
        )
        peer_source = (
            "carry it"
            if any(env_of[ckey(p)][1] == "label" for p in peer_members)
            else "resolve to it from their names"
        )
        # Every facet minus the ones Autopilot withholds, which is the same
        # filter `compute_drift` applies and the same denominator
        # `autopilot_not_applicable` leaves behind. Counted rather than
        # written down, so a facet added to `FACETS` is in this sentence
        # without anyone remembering to change it.
        abstaining = sum(1 for f in FACETS if not (f.standard_only and mode == "autopilot"))
        # Which gap this is, in the excerpt's own words. Under §2.3 a cluster
        # short on the node-level cohort alone is still compared on the eight,
        # so "all nineteen abstain" would be false of it -- and the eight it
        # does get are drawn from the `unknown` cohort, which is not a peer
        # group so much as the set of clusters that share the same omission.
        # Saying so is what stops the finding from reading as the smaller
        # complaint it is not.
        node_level = sum(1 for f in FACETS if f.standard_only)
        scope = (
            f"so its node-level cohort holds fewer than {COHORT_FLOOR} and the "
            f"{node_level} facets keyed `(mode, environment)` compare nothing for "
            f"it; the other {len(FACETS) - node_level} reach a baseline only "
            f"because `unknown` is a cohort in its own right, drawn from the "
            f"clusters that share no property but the missing label"
            if node_short and not cluster_short
            else f"so this one cohorts alone as `unknown` against a minimum of "
            f"{COHORT_FLOOR}, and all {abstaining} comparative checks in this "
            f"audit abstain for it"
        )
        alternatives = (
            ""
            if len(joinable) == 1
            else " ("
            + ", ".join(f"`{v}` would also reach it, on {n}" for v, n in joinable[1:])
            + ")"
        )
        excerpt = (
            f"carries no environment label -- `resourceLabels` sets none of "
            f"`environment`, `env`, `stage` or `tier` -- {others}. An unlabelled "
            f"cluster joins no named cohort under either of §2.3's two keys, "
            f"{scope} -- on this run and on every future run until "
            f"it is labelled. Set `resourceLabels.environment` to `{value}`: "
            f"{peers} other cluster{'' if peers == 1 else 's'} {peer_source}, which "
            f"reaches the floor with this one{alternatives}. Any value no peer holds "
            f"opens a new cohort of one and leaves the gap exactly as it is."
        )
        candidates[key] = [_emit(UNLABELLED_SLUG, c.get("name", ""), excerpt, UNLABELLED_SEVERITY)]
    return checks_run, candidates, not_applicable


def autopilot_not_applicable(clusters: list[dict]) -> dict[tuple, list[dict]]:
    """§6's `checks_not_applicable` for the facets `compute_drift` refuses to
    compute on Autopilot.

    Eleven facets carry `standard_only` — #1226's list — and `compute_drift`
    drops each of them for an Autopilot cohort. Dropping them is right: every
    one reads a field under `.nodePools[]`, or names a node-management or
    dataplane setting Google owns on Autopilot and no operator can diverge on.
    Dropping them silently is not: a slug missing from `commands` is exactly
    how a check nobody ran looks, so §6 counts it as a coverage gap unless the
    model happens to know which GKE settings Autopilot withholds and excuses
    it by hand. Declaring them here is what makes the denominator right
    without that knowledge.

    This is also the half of §2.3 that keeps an Autopilot minority from pinning
    the ledger. The eight facets that are configurable on both modes compare
    across them, so a lone Autopilot cluster is never in an undersized cohort
    for those; the eleven it cannot be compared on are declared here and leave
    the denominator. Neither half works without the other: declare fewer than
    eleven while keying every cohort on mode, and the difference becomes a
    `limitations` string that makes every run `partial`.

    Keyed off the cluster's own mode rather than its cohort's. The two agree
    wherever `compute_drift` drops them — an Autopilot cohort's members are all Autopilot —
    but the roster arithmetic in §6 is per-cluster, so an Autopilot cluster
    whose cohort floored out has the same eleven inapplicable checks and should
    have the same eleven in its `checks_not_applicable`. Its `limitations`
    sentence then accounts for the checks that remain instead of overstating
    the full roster.
    """
    standard_only_slugs = [f.slug for f in FACETS if f.standard_only]
    out: dict[tuple, list[dict]] = {}
    for c in clusters:
        if cluster_mode(c) != "autopilot":
            continue
        out[ckey(c)] = [
            {
                "check": slug,
                "reason": (
                    "GKE Autopilot: Google manages the nodes and the dataplane and exposes "
                    f"no user node pool, so `{FACETS_BY_SLUG[slug].field_path}` holds no "
                    "operator-chosen value to compare against the cohort."
                ),
            }
            for slug in standard_only_slugs
        ]
    return out


def unvoted_facets(
    clusters: list[dict], checks_run: dict[tuple, list[str]], *, now: datetime
) -> tuple[dict[tuple, list[dict]], dict[tuple, str]]:
    """Account for every facet a compared cluster did *not* vote on, as
    `(checks_not_applicable, limitations)` keyed by `ckey`.

    §6 reads `commands` as the roster of checks that ran, and `coverage_gaps`
    divides it by the full roster: a slug that is in neither `commands`, nor
    `checks_not_applicable`, nor a `limitations` sentence is a cluster reported
    as `partial` forever with nothing saying what is missing or why. Two ways
    for a cluster past the cohort floor to lose a facet, and until this
    function neither said anything:

    - **The cluster abstained.** Its normalizer returned `None`, so it held no
      readable value and cast no vote while its peers did.
    - **The cohort reached no baseline.** §3.3 wants `COHORT_FLOOR` readable
      values with one token on at least `BASELINE_MIN_RATIO` of them; a cohort
      that splits evenly has no majority to be an outlier from, and the facet
      is then uncompared for every one of its members.

    Both are `limitations` rather than `checks_not_applicable`, because §3.1 is
    narrow about which is which: `checks_not_applicable` leaves the coverage
    denominator, so it is for a facet the cluster leaves nothing for this
    stream to compare, and a facet you could have compared and did not is a
    real gap.

    `_shape_rules_out` holds that line, and holds it in two places. A cluster
    with no node pool at all, or none this facet looks at, has no
    `.nodePools[]…` to compare in the same sense an Autopilot cluster does not,
    and `autopilot_not_applicable` already makes that call for the same five
    slugs on the other mode. A cluster on no release channel is the other:
    §4.1 assigns enrolment to another audit, so a gap here is one nothing this
    stream may recommend would ever close.

    Clusters `cohort_limitations` already covers -- ineligible ones and those
    whose cohort floored out -- are skipped here: their sentence is that
    nothing compared them, and naming nineteen facets under it adds nothing.
    """
    not_applicable: dict[tuple, list[dict]] = {}
    limitations: dict[tuple, str] = {}
    layout = cohort_layout(clusters, now=now)
    env_of = layout.env_of
    for c in clusters:
        key = ckey(c)
        if key not in env_of:
            continue
        mode = cluster_mode(c)
        ran = set(checks_run.get(key, ()))
        abstained: list[str] = []
        no_baseline: list[str] = []
        # Per facet class, because §2.3 gives the two classes different
        # cohorts and either can be under the floor while the other is not.
        # `cohort_limitations` owns the sentence for a class that floored out
        # wholesale; what is left for this loop is the facets a cohort that
        # *did* reach the floor still failed to compare.
        floored = {
            standard_only: len(layout.cohort_of(c, standard_only)) < COHORT_FLOOR
            for standard_only in (True, False)
        }
        for facet in FACETS:
            if facet.slug in ran or (facet.standard_only and mode == "autopilot"):
                continue
            if floored[facet.standard_only]:
                continue
            if facet.normalize(c) is None:
                reason = _shape_rules_out(facet, c)
                if reason:
                    not_applicable.setdefault(key, []).append({"check": facet.slug, "reason": reason})
                else:
                    abstained.append(facet.slug)
                continue
            no_baseline.append(facet.slug)
        parts = []
        if abstained:
            parts.append(
                f"{len(abstained)} facet(s) held no readable value on this cluster, so it cast "
                f"no vote and nothing compared it on them: "
                + ", ".join(f"`{slug}` (`{FACETS_BY_SLUG[slug].field_path}`)" for slug in abstained)
            )
        if no_baseline:
            # Grouped by cohort, since the two classes no longer share one: a
            # single sentence naming one label over facets compared in two
            # different cohorts would misattribute half of them.
            for standard_only in (True, False):
                slugs = [s for s in no_baseline if FACETS_BY_SLUG[s].standard_only == standard_only]
                if not slugs:
                    continue
                cohort = layout.cohort_of(c, standard_only)
                label = "/".join(str(k) for k in layout.key_for(c, standard_only))
                detail = []
                for slug in slugs:
                    tokens = facet_tokens(FACETS_BY_SLUG[slug], cohort)
                    top = Counter(tokens.values()).most_common(1)
                    detail.append(
                        f"`{slug}` ({len(tokens)} readable value(s)"
                        + (f", commonest on {top[0][1]}" if top else "")
                        + ")"
                    )
                parts.append(
                    f"{len(slugs)} facet(s) reached no baseline in cohort {label} and were "
                    f"compared for none of its {len(cohort)} members -- §3.3 needs {COHORT_FLOOR} "
                    f"readable values with one token on {BASELINE_MIN_RATIO:.0%} of them: "
                    + ", ".join(detail)
                )
        if parts:
            limitations[key] = "; ".join(parts) + "."
    return not_applicable, limitations


def _shape_rules_out(facet: Facet, c: dict) -> str | None:
    """The `checks_not_applicable` reason for a facet this cluster's shape
    leaves nothing to read, or `None` where the abstention is a gap instead.

    Two surfaces qualify. The node-pool one is the same
    `autopilot_not_applicable` names on the other mode: a Standard cluster
    running no node pool the facet reads is in the position Autopilot puts
    every cluster in.

    The other is a cluster on no release channel. §4.1 hands enrolment to the
    Upgrade & Patch Readiness audit and tells this stream to emit nothing, so
    calling it a coverage gap instead asks the ledger to stay open on a
    recommendation this stream is forbidden to make. `coverage_gaps` reads a
    `limitations` sentence as a gap, a gap forces `resolved` to 0 and
    `partial` true, and one static-version cluster would therefore keep every
    remediation pull request in the fleet from ever being closed -- for as
    long as it runs, which nothing here can shorten.
    """
    if facet.slug == RELEASE_CHANNEL_SLUG:
        return (
            "the cluster is on no release channel, so it has no upgrade cadence to compare "
            "against the cohort; enrolling it is the Upgrade & Patch Readiness audit's "
            "recommendation to make, not this one's (§4.1)."
        )
    if not facet.standard_only or ".nodePools[]" not in facet.field_path:
        return None
    pools = c.get("nodePools") or []
    if not pools:
        why = "the cluster runs no node pools"
    elif all(_is_windows_pool(p) for p in pools):
        why = "every node pool runs Windows, which this facet does not compare"
    else:
        return None
    return (
        f"{why}, so `{facet.field_path}` has no value to compare against the cohort."
    )


def _autoscaling_countable_pools(c: dict) -> list[dict]:
    """The pools `norm_pool_autoscaling` actually votes over -- the same
    `exclude_pool` predicate it hands `_pool_fraction`, which drops the pools
    §4.8 calls deliberately fixed-size."""
    return [p for p in c.get("nodePools") or [] if not _is_pinned_pool(p)]


def _shape_mismatch(facet: Facet, cluster: dict, baseline_clusters: list[dict]) -> bool:
    """§4.8's "do NOT flag single-pool clusters against multi-pool peers".

    A one-pool cluster can only ever normalize to `ALL` or `NONE`; `SOME` is
    unreachable for it. So against a cohort whose baseline is `SOME` -- a
    baseline only multi-pool clusters can hold -- it is an outlier that no
    change can bring into line: enabling autoscaling on its single pool moves it
    to `ALL`, still not `SOME`, and the finding returns next week having cost a
    node-pool update. That is the same unclosable shape `_flag_less_only`
    already guards in the other direction, and §4.8 names it explicitly.

    Scoped to `pool-autoscaling` because that is the facet §4.8 says it for.
    §4.3's `secure-boot` and `integrity-monitoring` share the ALL/SOME/NONE
    scale but list a different set of suppressions and not this one.
    """
    if facet.slug != "pool-autoscaling":
        return False
    if len(_autoscaling_countable_pools(cluster)) != 1:
        return False
    return any(len(_autoscaling_countable_pools(peer)) > 1 for peer in baseline_clusters)


def compute_drift(clusters: list[dict], *, now: datetime) -> tuple[dict[tuple, list[str]], dict[tuple, list[dict]]]:
    """Returns `(checks_run_by_cluster, candidates_by_cluster)` -- the
    facets actually voted on for each cluster, and the outlier findings
    that survived the severity ladder. Both are keyed by `ckey`, not by
    cluster name; see `ckey` for why a name is not an identity here."""
    checks_run: dict[tuple, list[str]] = {ckey(c): [] for c in clusters}
    candidates: dict[tuple, list[dict]] = {ckey(c): [] for c in clusters}
    outlier_facet_count: dict[tuple, int] = {ckey(c): 0 for c in clusters}
    # §3.6's guard rewrites a cluster's candidates from its `ckey` alone, so it
    # needs the same name the per-facet emits used.
    targets: dict[tuple, str] = {ckey(c): target_name(c) for c in clusters}

    layout = cohort_layout(clusters, now=now)
    env_of, strategy = layout.env_of, layout.strategy
    # §3.5 downgrades a finding whose "cohort membership rests on an inferred
    # environment". Under the `project` and `mode-only` strategies no cohort key
    # holds an environment at all, so no membership rests on one -- but
    # `environment_of` still reports `inferred` for any cluster with `test` or
    # `prod` somewhere in its name, and reading that unconditionally downgraded
    # every finding in the cohort (`baseline_inferred` is an `any()` over the
    # baseline holders, so one such name was enough). That is a step the SOP
    # does not ask for, and a step is the difference between a `minor` finding
    # and a dropped one.
    env_matters = strategy == "environment"

    # §2.3's two cohortings, each voted separately. The node-level pass keys on
    # mode and the cluster-level pass across it, so the same cluster votes in
    # two different groups of peers -- which is the point: `image-type` on an
    # Autopilot cluster has no meaning, while `binary-authorization` on one is
    # the same setting its Standard peers carry.
    for standard_only in (True, False):
        facets = [f for f in FACETS if f.standard_only == standard_only]
        for key, members in layout.cohorts_for(standard_only).items():
            if len(members) < COHORT_FLOOR:
                continue
            # Only the node-level key carries a mode; the cluster-level pass
            # runs for every mode by construction, so there is nothing to skip.
            if standard_only and key[0] == "autopilot":
                continue
            cohort_label = "/".join(str(k) for k in key)
            for facet in facets:
                tokens = facet_tokens(facet, members)
                baseline = compute_baseline(tokens)
                if baseline is None:
                    continue
                t_star, m, n, r = baseline
                voters = [c for c in members if ckey(c) in tokens]
                for c in voters:
                    checks_run[ckey(c)].append(facet.slug)
                baseline_clusters = [c for c in voters if tokens[ckey(c)] == t_star]
                baseline_inferred = env_matters and any(env_of[ckey(c)][1] == "inferred" for c in baseline_clusters)
                # The clusters that hold the baseline, not every cluster that voted.
                # `peers:` sits one line under "in {m}/{n} clusters" and one line
                # over the outlier's own `observed:`, so listing all `n` names
                # contradicted both of its neighbours: it printed 10 names beside a
                # claim that 9 clusters agree, and among them the very cluster the
                # finding is about. A reader checking the comparison against
                # `drift-peer-std-4 emits no logging components` found
                # `drift-peer-std-4` in the list of clusters that do.
                peer_names = sorted(target_name(c) for c in baseline_clusters)
                # §3.2 defines `k` as `n - m`, the count of voting members not on
                # the baseline token -- how split the cohort is. `len(outliers)` is
                # a different number wherever `should_flag` is narrower than "differs
                # from `t*`", which is every facet except the two on `_flag_ne`: a
                # cluster that diverges upward is not flagged but is still divergent.
                # `len(outliers) <= n - m` always, so reading it here under-counted
                # the split and under-applied §3.5's `k >= 3` step -- publishing at a
                # severity above the one the SOP specifies, and keeping findings the
                # SOP would have dropped below `minor`.
                k = n - m
                for c in voters:
                    observed = tokens[ckey(c)]
                    if not facet.should_flag(observed, t_star):
                        continue
                    if _shape_mismatch(facet, c, baseline_clusters):
                        continue
                    name = target_name(c)
                    inferred = baseline_inferred or (env_matters and env_of[ckey(c)][1] == "inferred")
                    base_sev = facet.base_severity(observed) if callable(facet.base_severity) else facet.base_severity
                    sev, downgrades = apply_severity_ladder(base_sev, r, k, inferred)
                    if sev is None:
                        continue
                    # Only the set-valued facets have a "missing" to name, and
                    # `_flag_not_superset` is exactly the predicate that says so:
                    # it is the gate that already took this difference.
                    missing = _missing_tokens(observed, t_star) if facet.should_flag is _flag_not_superset else None
                    excerpt = build_excerpt(facet.field_path, t_star, m, n, cohort_label, peer_names, observed, sev, base_sev, downgrades, r, missing, read_path(facet, c))
                    candidates[ckey(c)].append(_emit(facet.slug, c.get("name", ""), excerpt, sev))
                    outlier_facet_count[ckey(c)] += 1

    # §3.6 split-cluster guard
    for cluster_key, count in outlier_facet_count.items():
        if count >= SPLIT_CLUSTER_FACETS:
            facet_names = sorted({cand["check"] for cand in candidates[cluster_key]})
            candidates[cluster_key] = [
                _emit(
                    UNCOHORTED_SLUG, cluster_key[-1],
                    f"outlier on {count} facets in one run: {', '.join(facet_names)} -- likely a cohort-labelling problem, not {count} independent drifts.",
                    UNCOHORTED_SEVERITY,
                )
            ]
    return checks_run, candidates


def collect_project(project: str, *, run: RunFn, now: datetime) -> tuple[list[dict], dict | None, str | None]:
    """One project's clusters, as the sweep's pool calls it. `now` is unused
    here and kept because the pool passes the run's clock to every collector
    call; eligibility is decided later, over the whole fleet at once."""
    return enumerate_project_clusters(project, run=run)


def collect_fleet(project: str | None = None, *, run: RunFn = default_run, max_workers: int = MAX_WORKERS, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    started_at = time.strftime(TIMESTAMP_FORMAT, time.gmtime())
    discovery = discover_fleet(project, run=run)

    all_clusters: list[dict] = []
    command_by_project: dict[str, dict] = {}
    failed_projects: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(collect_project, p, run=run, now=now): p
            for p in discovery.projects
        }
        for future in as_completed(futures):
            try:
                clusters, record, error = future.result()
            except Exception as exc:  # noqa: BLE001
                # `future.result()` re-raises, so one unhandled exception on
                # one project used to abort the whole run — and the SOP
                # invokes this as `fleet_drift.py … > manifest_….json`, so by
                # then the shell had truncated the file and the fleet was lost
                # to one bad object. A failed project is a shape this loop
                # already has: it lands in `failed_projects`, which §6 turns
                # into a coverage gap the document must account for.
                log(f"{futures[future]}: collector raised {type(exc).__name__}: {exc}")
                clusters, record, error = [], None, f"collector raised {type(exc).__name__}: {exc}"[:ERROR_EXCERPT_CHARS]
            all_clusters.extend(clusters)
            if record is not None:
                command_by_project[futures[future]] = record
            # Not `elif`. A partial listing carries both -- a record, because
            # the clusters it returned are evidence and their findings need the
            # command that produced them, and an error, because the project is
            # still short. Under `elif` the record silently swallowed the
            # error and the project read as fully enumerated, which is the one
            # outcome `ZONE_TIMEOUT_MARKER` exists to prevent.
            if error:
                failed_projects[futures[future]] = error

    # Before anything reads a cluster's name: `compute_drift` writes names into
    # `peers:` lists and candidate objects, and the entry loop below writes the
    # manifest's own key.
    qualify_targets(all_clusters)

    checks_run, candidates = compute_drift(all_clusters, now=now)
    # Merged rather than returned by `compute_drift`, because this check is the
    # one that has to run *outside* the cohort loop -- see
    # `unlabelled_environment_candidates`. Extending both dicts is what turns a
    # cluster no facet reached from "0 of 19 applicable checks ran", which §6
    # reads as a total coverage gap, into "1 of 20", which is the truth.
    unlabelled_run, unlabelled_candidates, unlabelled_na = unlabelled_environment_candidates(all_clusters, now=now)
    for cluster_key, slugs in unlabelled_run.items():
        checks_run.setdefault(cluster_key, []).extend(slugs)
    for cluster_key, found in unlabelled_candidates.items():
        candidates.setdefault(cluster_key, []).extend(found)
    limitations = cohort_limitations(all_clusters, now=now)
    not_applicable = autopilot_not_applicable(all_clusters)
    for cluster_key, skipped in unlabelled_na.items():
        not_applicable.setdefault(cluster_key, []).extend(skipped)
    # Everything a compared cluster did not vote on. `cohort_limitations`
    # speaks for the clusters nothing compared at all, and these two never
    # cover the same cluster, but the join is written to survive one of them
    # changing its mind.
    unvoted_na, unvoted_limits = unvoted_facets(all_clusters, checks_run, now=now)
    for cluster_key, skipped in unvoted_na.items():
        not_applicable.setdefault(cluster_key, []).extend(skipped)
    for cluster_key, sentence in unvoted_limits.items():
        existing = limitations.get(cluster_key)
        limitations[cluster_key] = f"{existing} {sentence}" if existing else sentence

    entries = []
    for c in all_clusters:
        project_name = c.get("_project", "")
        record = command_by_project.get(project_name)
        cluster_key = ckey(c)
        ran = checks_run.get(cluster_key, [])
        commands = [{"check": slug, **record} for slug in ran] if record else []
        found = candidates.get(cluster_key, [])
        if record:
            for candidate in found:
                # §3.6's `uncohorted` is derived from other facets' verdicts
                # rather than voted on, so its slug is in no cohort's
                # `checks_run` and `commands` holds no record for it. That is
                # the one shape `adopt_collector_evidence` cannot adopt --
                # excerpt and command move together, and with no command it
                # keeps neither, leaving the collector's excerpt to be
                # paraphrased by the model in the one place §6 says the
                # collector's own words are what the evidence is. A candidate
                # may carry the command that produced *it*, and adoption
                # prefers that over the per-slug record, so the listing call
                # every facet behind the verdict was read from goes here.
                if candidate["check"] not in ran:
                    candidate.setdefault("command", record["command"])
        entry = {
            "name": target_name(c),
            "project": project_name,
            "location": c.get("location") or c.get("zone") or "",
            "autopilot": cluster_mode(c) == "autopilot",
            # Still `collected` when a cohort floored out. Nothing was
            # compared, but nothing the model can run by hand would compare it
            # either -- the peers do not exist -- and `gate-failed` asks for
            # exactly that retry. The `limitations` sentence beside it is what
            # carries the truth, and §6's coverage arithmetic reads it.
            "outcome": OUTCOME_COLLECTED,
            "commands": commands,
            "candidates": found,
        }
        note = limitations.get(cluster_key)
        if note:
            entry["limitations"] = note
        skipped = not_applicable.get(cluster_key)
        if skipped:
            entry["checks_not_applicable"] = skipped
        entries.append(entry)

    # A project whose `clusters list` failed contributed no clusters, and with
    # no entry of its own it contributes no evidence of that either -- the
    # manifest then reads exactly like a fleet that never had those clusters in
    # it. That is worse here than in a per-cluster stream: drift compares each
    # cluster against its cohort peers, so clusters missing from the comparison
    # quietly change what counts as an outlier, and every surviving cluster's
    # verdict is computed against a fleet nobody knows is short. Recording the
    # project as a gate-failed target is what makes the loss say so --
    # cross_check_manifest requires the document to account for it, and §6
    # turns that into a coverage gap.
    entries += [
        {
            "name": f"{PROJECT_TARGET_PREFIX}{project_name}",
            "project": project_name,
            "location": "global",
            "outcome": OUTCOME_GATE_FAILED,
            "error": error,
        }
        for project_name, error in sorted(failed_projects.items())
    ]

    # The same argument one rung up. A project that failed its own `clusters
    # list` is named above; a `projects list` that failed took the names with
    # it, so the row stands for every project nobody enumerated and is named
    # for the call rather than for a project. `UNENUMERATED_PROJECTS` cannot
    # collide with a real id -- project ids are lowercase.
    if discovery.partial:
        entries.append(
            {
                "name": UNENUMERATED_PROJECTS_TARGET,
                "project": "",
                "location": "global",
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
    if discovery.error:
        # `finish` carries a top-level key it does not read, and the SOP's
        # §1.4 zero-cluster rule tells the worker what an empty fleet with
        # this key set means: do not publish, report the failure.
        manifest["error"] = discovery.error
    elif failed_projects and not all_clusters:
        # Discovery named projects and the run read no cluster from any of
        # them. The gate-failed entries above record each loss, but the stop
        # rule §4 gives the worker reads this key, and without it a run that
        # read no cluster at all differs from a healthy empty fleet only by
        # rows the worker has to notice and add up. The SOP redirects this
        # script's stdout into the manifest without checking its status, so
        # the non-zero exit `main` also returns is not what stops the run:
        # this key is.
        first, error = sorted(failed_projects.items())[0]
        manifest["error"] = (
            f"no cluster could be read: {len(failed_projects)} of "
            f"{len(discovery.projects)} project(s) in scope failed `clusters list` "
            f"and the rest held none; {first}: {error}"
        )[:ERROR_EXCERPT_CHARS]
    return manifest


def candidate_summary(manifest: dict) -> list[str]:
    """The closing stderr lines: how many candidates this run produced, and where.

    `candidates` is nested under each cluster, so the number a worker has to
    account for is spread across a file of tens of kilobytes, and nothing else
    in the run states it. On 2026-09-21 a run whose manifest carried three
    candidates published `findings: []` with a `resolved_because` for each,
    reasoning that "the collector found 0 drift findings on this run". `finish`
    held the close on the manifest cross-check, which is what that guard is
    for, but the run had already reported an all-clear over two unlabelled
    clusters and an authorized-networks outlier.

    Accounting for a candidate is not the same as publishing it: §4.1's
    pin/freeze marker and §4.3's young burst or spot pool are judgements the
    collector cannot make, so the second line says report *or* drop with the
    exclusion named, which is what the procedure asks. Projects whose listing
    failed are counted too — the cluster total alone reads like a whole fleet.

    stderr, because stdout is redirected into the manifest file.
    """
    clusters = [c for c in manifest.get("clusters") or [] if isinstance(c, dict)]
    collected = [c for c in clusters if c.get("outcome") == OUTCOME_COLLECTED]
    unread = [c for c in clusters if c.get("outcome") == OUTCOME_GATE_FAILED]
    by_check: dict[str, list[str]] = {}
    for cluster in collected:
        for candidate in cluster.get("candidates") or []:
            if isinstance(candidate, dict):
                by_check.setdefault(str(candidate.get("check", "")), []).append(
                    str(cluster.get("name", ""))
                )
    total = sum(len(names) for names in by_check.values())
    head = f"{len(collected)} cluster(s) collected"
    if unread:
        head += f", {len(unread)} project(s) unread"
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
        "report each candidate above as a finding, or drop it with the hand "
        "exclusion named in its note; a resolved_because for one contradicts "
        "this manifest",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="single project to audit; omit to run §1's project discovery")
    args = parser.parse_args(argv)
    manifest = collect_fleet(args.project)
    print(json.dumps(manifest, indent=2))
    if manifest.get("error"):
        # The manifest is still written -- the shell has already redirected
        # stdout -- but a run that could not find a project to audit is a
        # failed run, and the exit code says so where a log line can be missed.
        log(f"WARNING: {manifest['error']}")
        return 1
    for line in candidate_summary(manifest):
        log(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
