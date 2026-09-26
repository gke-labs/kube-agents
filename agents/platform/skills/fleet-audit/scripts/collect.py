#!/usr/bin/env python3
"""collect.py — Procedural collector for the fleet-audit checks that are code
wearing prose.

The manifest it prints is the contract in
docs/designs/fleet-audit-collector-manifest.md, which `audit_report.py finish
--manifest-file` cross-checks the published document against.

**Scope: three streams' check tables in full.** `obtainability-audit`'s
twenty-three-check roster (§3.1–§3.23 of `governance/obtainability_audit_sop.md`),
`compliance-audit`'s sixteen-check roster (§2.1–§2.16 of
`governance/compliance_audit_sop.md`), and `ai-security-audit`'s six-check
roster (§3.1–§3.6 of `governance/ai_security_audit_sop.md`). Every check the
three SOPs define is mechanical — none needed a `needs_triage` judgment call,
including ai-security's §3.4, whose severity forks on whether the same
container also trips §3.2, a fact both checks compute from the same dump — so
nothing was left on the SOP side to skip. Other streams have their own
collectors (`fleet_drift.py`, `patch_readiness.py`) or none. The three streams
collect in different shapes: obtainability answers every check from one
workload dump plus the reads its declaration fields need; compliance issues
several distinct `kubectl` and `gcloud` reads per cluster, three of them
best-effort because their CRDs are not installed everywhere; ai-security reads
a workload dump and a Service dump, the second backing exactly one check
(`inference-endpoint-public`) that joins it against the first.
`collect_cluster` is a thin dispatcher over a per-stream *context builder* for
exactly this reason — the engine below is what every stream shares, not what
the first one happened to need.

What this file does for the checks it covers:

  1. Discovers every project the caller can see (`gcloud projects list`,
     or the one `--project` names) and enumerates each one's clusters
     (`gcloud container clusters list`), naming every cluster
     `<project>/<location>/<name>` because a bare name is unique only inside
     one project and location.
  2. Fetches per-cluster credentials into an isolated kubeconfig — the same
     path convention every SOP already uses (`AGENTS.md`, "Cluster
     Credentials") — then runs the stream's context builder: one dump for
     obtainability, several distinct `kubectl`/`gcloud` reads for
     compliance, two dumps for ai-security, each behind its own fail-closed
     `jq -e`-equivalent gate so a truncated or empty result cannot read as a
     clean cluster.
  3. Runs every covered check's filter against the collected context, in
     parallel across clusters (a thread pool; each cluster's kubeconfig is a
     private file, so no cluster's read can bleed into another's).
  4. Emits the run manifest: for every enumerated cluster, an outcome,
     the literal collection commands (so `checks_run` stays falsifiable),
     and every candidate finding.

The agent's job on a covered check shrinks to: run this script, read the
manifest, and — because every check converted so far is fully mechanical,
needing no `needs_triage` judgment — copy each candidate into `findings.json`
with the recommendation prose the validator requires. Nothing here writes to
a cluster; every subprocess this module runs is `gcloud`/`kubectl` read
verbs, in the same register `command_policy.py` already allows an agent's
own shell to run.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import ipaddress
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, NamedTuple

MANIFEST_VERSION = 1

# A digest of this file, published in the manifest so a reader can tell which
# collector source produced a run. It is carried, not read: nothing downstream
# compares it today.
# Long enough that two collector sources will not collide, short enough to
# read in a log line. It has to agree across every collector: the comparison
# is between one run's revision and the last one's, so a file that truncated
# differently would report a moved collector on the run that changed it.
REVISION_DIGEST_CHARS = 12
CHECKS_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[
    :REVISION_DIGEST_CHARS
]

SCRATCH_DIR = os.environ.get("FLEET_AUDIT_SCRATCH_DIR") or "/opt/data/scratch"
KUBECONFIG_DIR = Path(os.environ.get("HERMES_HOME") or "/opt/data") / ".kubeconfigs"
DEFAULT_TIMEOUT_S = 60
MAX_WORKERS = 8

# What `default_run` reports when *it* gave up, following the shell convention
# for a command killed by `timeout(1)`. It is this collector's own marker and
# never something a `kubectl` or `gcloud` actually returned, so any branch that
# reads an exit code as a statement about the cluster has to exclude it first.
# On 2026-09-06 `kcc-object-wedged` did not: it took `rc != 0` on `kubectl get
# gcp -A` to mean the API server does not serve the category, and published
# "Config Connector is not installed on this cluster" about the one cluster in
# the fleet that runs Config Connector, on the strength of its own timeout.
TIMEOUT_RC = 124

# The manifest contract's outcomes, and the target names a sweep of more than
# one project needs (§2 of `docs/designs/fleet-audit-collector-manifest.md`).
# A cluster is `<project>/<location>/<name>`; a project whose `clusters list`
# failed is `project/<id>`.
OUTCOME_COLLECTED = "collected"
OUTCOME_GATE_FAILED = "gate-failed"
OUTCOME_UNREACHABLE = "unreachable"
PROJECT_TARGET_PREFIX = "project/"
QUALIFIED_TARGET_SEPARATOR = "/"
# The target standing for the projects a failed `gcloud projects list` never
# named. Uppercase because a GCP project id cannot be, so no real project can
# collide with it.
UNENUMERATED_PROJECTS_TARGET = PROJECT_TARGET_PREFIX + "UNENUMERATED_PROJECTS"
# What that same target stands for when the operator narrowed the scope on
# purpose. `--project` skips discovery, so a scoped run reads one project and
# names no other; without a row saying so, `finish` resolves every ledger
# finding on a cluster in any other project. `fleet_drift.SCOPED_RUN_NOTE`
# states the same rule.
SCOPED_RUN_NOTE = (
    "scope narrowed to project {project!r} by `--project`: discovery was skipped, so no other "
    "project in this fleet was named or read, and this run cannot speak for their clusters."
)
# gcloud's own words for a project whose Kubernetes Engine API is off. Such a
# project cannot hold a GKE cluster, so it reads as empty rather than as a lost
# project -- otherwise every non-GKE project a credential can see is a
# permanent coverage gap. Permission denied is deliberately absent: a project
# this credential may not list may well hold clusters. Same markers as
# `fleet_drift.API_DISABLED_MARKERS`.
API_DISABLED_MARKERS = ("SERVICE_DISABLED", "accessNotConfigured", "has not been used in project")
# gcloud's words for a `clusters list` some zones answered and others did not.
# It still exits 0 with a JSON array, so the silent zones' clusters would read
# as clusters that do not exist. See `fleet_drift.ZONE_TIMEOUT_MARKER`.
ZONE_TIMEOUT_MARKER = "did not respond"
# kubectl's words for a resource type the API server does not serve. Only this
# answer reads as "absent"; a Forbidden, a 5xx or a refused connection also
# exits non-zero and says nothing about whether the type exists.
RESOURCE_TYPE_ABSENT_MARKER = "doesn't have a resource type"
# How a collector read that failed says so in its reason; see `unevaluated`.
UNDETERMINED_PREFIX = "Undetermined:"
ERROR_EXCERPT_CHARS = 300
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# `workload_declarations` walks a GitOps clone. A file is applied to a known
# cluster only when it sits at `clusters/<name>/...`, which is three path
# components at minimum -- the root, the cluster, and the file itself.
GITOPS_CLUSTER_TREE_ROOT = "clusters"
GITOPS_CLUSTER_TREE_DEPTH = 2
GIT_DIR_NAME = ".git"

# `release_declarations` indexes the objects that render a workload a GitOps
# repo holds no manifest for -- an Argo CD `Application`, from either a chart
# or a Kustomize overlay, and a Flux `HelmRelease` -- plus the two it needs to
# resolve them: Argo CD's cluster registration Secret (which is how
# `spec.destination.server` becomes a cluster name) and Flux's `HelmRepository`
# (which is how a `HelmRelease`'s chart reference becomes a URL `helm show
# values` can read).
ARGOCD_APPLICATION_KIND = "Application"
ARGOCD_CLUSTER_SECRET_LABEL = "argocd.argoproj.io/secret-type"
ARGOCD_CLUSTER_SECRET_VALUE = "cluster"
ARGOCD_IN_CLUSTER_SERVER = "https://kubernetes.default.svc"
FLUX_HELM_RELEASE_KIND = "HelmRelease"
FLUX_HELM_REPOSITORY_KIND = "HelmRepository"
# The two shapes a key in that index takes. An Argo CD chart Application is
# found by the Application name its tracking id carries; a Helm release proper
# -- `helm install`, or Flux driving it -- by release namespace and name.
RELEASE_KEY_APPLICATION = "application"
RELEASE_KEY_RELEASE = "release"
# A third key on that same index, answering a different question: not "where
# is this object declared" but "where would a *new* object for this namespace
# go". Only a local Kustomize root can answer it, because only a local root is
# a directory in this repository that renders into the namespace.
RELEASE_KEY_NAMESPACE = "namespace"
# `(cluster, RELEASE_KEY_NAMESPACE, namespace)` is three wide, as the
# Application key is; only the release key is four. The tag discriminates on
# its own, and the width is checked with it so that reading `key[2]` stays
# safe if a later key shape is added at a different width.
NAMESPACE_KEY_WIDTH = 3
# The two ways that question resolves, carried on the record so the SOP can
# tell them apart: a directory already holding objects applied to the
# namespace, or the Kustomize root an Application renders into it. A sibling
# needs no wiring; an overlay's `resources:` list has to name the new file, and
# a remediation carries one file, so the SOPs make that fix `kind: manual`.
NAMESPACE_DIRECTORY_SIBLING = "sibling"
NAMESPACE_DIRECTORY_OVERLAY = "overlay"
NAMESPACE_DIRECTORY_CLUSTER = "cluster"
# The namespace slot the `cluster` arm's entry occupies. Empty is free for it:
# both other arms key off a namespace an object declares, and neither index
# records an object without one. A cluster-scoped object -- a
# ClusterRoleBinding, a StorageClass -- carries the empty namespace itself and
# so reaches this entry on the first lookup rather than the fallback, which is
# right, since the directory is where a new one of those belongs too.
NAMESPACE_KEY_ANY = ""
# What would make the `cluster` arm wrong, and the one thing that suppresses
# it. An Argo CD AppProject can whitelist the namespaces its Applications may
# write to, and an object outside that list is refused at sync rather than
# applied. This collector cannot tell which project owns a plain-directory
# Application -- it parses only chart and overlay sources -- so it cannot check
# the right project and instead checks all of them: one restrictive
# `destinations` entry anywhere in the clone withdraws the arm fleet-wide.
ARGOCD_APPPROJECT_KIND = "AppProject"
ARGOCD_DESTINATIONS_FIELD = "destinations"
ARGOCD_DESTINATION_WILDCARD = "*"
# Where a values override goes, per reconciler. Argo CD accepts both a YAML
# string (`values`) and a structured block (`valuesObject`); this names the one
# already in the file, and `valuesObject` when neither is, because a structured
# block is what a programmatic edit can extend without reindenting a string.
ARGOCD_VALUES_OBJECT_FIELD = "valuesObject"
ARGOCD_VALUES_STRING_FIELD = "values"
FLUX_VALUES_FIELD = "values"
ARGOCD_KUSTOMIZE_PATCHES_FIELD = "patches"
# Which of those two overrides a declaration takes, since they are written
# differently: a values mapping the chart reads by key, or a list of patches
# Kustomize applies to a matched object. Carried on the entry so the SOP can
# branch on it rather than inferring one from the shape of `values_field`.
RENDERER_HELM = "helm"
RENDERER_KUSTOMIZE = "kustomize"
# What marks a directory as a Kustomize root. `kustomization.yaml` is what
# `kustomize create` writes; the other two are the legacy spellings `kustomize
# build` still accepts, and a repo that uses one is exactly as unresolvable
# without this as one that uses the first.
KUSTOMIZATION_FILE_NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
# Spelled the same way `audit_report.KCC_API_GROUP_SUFFIX` spells it, and
# copied rather than imported for the reason `SYSTEM_NAMESPACES` is below.
KCC_API_GROUP_SUFFIX = "cnrm.cloud.google.com"
# `check_kcc_object_wedged`. `Ready` is the one condition every Config
# Connector kind carries; `audit_report.KCC_READY_CONDITION` spells it the
# same way, copied for the reason above.
KCC_READY_CONDITION = "Ready"
# The `Ready=False` reason KCC sets on an object that is fine in itself and is
# only waiting on another object. It is what separates a cause from a symptom,
# and the whole reason this check does not emit one finding per unready object:
# on 2026-09-05 a single `PubSubTopic` failing on a missing IAM role put
# fifteen `ContainerCluster`s into this state behind it.
#
# Only this one. `DependencyNotFound` is a different reason with a different
# meaning -- the referent does not exist at all, so no other object's repair
# will ever clear it -- and it is deliberately not listed here, which leaves it
# a cause. A live probe on 2026-09-06 confirmed KCC writes the two distinctly:
# a `ComputeFirewall` pointed at an absent `networkRef` got
# `DependencyNotFound`, and the same firewall pointed at a network that existed
# but had failed to create got `DependencyNotReady`.
KCC_DEPENDENCY_NOT_READY_REASON = "DependencyNotReady"
# How much of a controller message survives into the excerpt. KCC pastes the
# whole upstream API error in, which for a 403 runs to several hundred
# characters of URL and quota-project boilerplate after the part that matters.
MAX_KCC_WEDGE_MESSAGE_CHARS = 240
# How many blocked objects an excerpt names before it stops and counts. Naming
# all fifteen is not more informative than naming three and saying fifteen.
MAX_KCC_BLOCKED_NAMED = 3

# The fleet-wide system-namespace set (S1), spelled identically in every SOP
# that names it. Kept here rather than imported from audit_report.py: this
# script ships and runs standalone (see the module docstring), and a
# constant four SOPs already agree on is safer copied once than imported
# across a module boundary that changes what "the collector" depends on.
SYSTEM_NAMESPACES = frozenset(
    {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "gmp-system",
        "gmp-public",
        "gke-gmp-system",
        "cnrm-system",
        "configconnector-operator-system",
        "krmapihosting-system",
        "istio-system",
        "asm-system",
        "anthos-identity-service",
        "gatekeeper-system",
        "composer-system",
    }
)


def _is_system_namespace(ns: str) -> bool:
    return (
        ns in SYSTEM_NAMESPACES
        or ns.startswith("gke-")
        or ns.startswith("config-management-")
    )


def log(msg: str) -> None:
    print(f"[collect] {msg}", file=sys.stderr, flush=True)


class Run(NamedTuple):
    """One subprocess's outcome, in the shape the manifest records it."""

    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]


def default_run(argv: list[str], *, env: dict | None = None, timeout: int = DEFAULT_TIMEOUT_S) -> Run:
    """The real subprocess call. Tests inject a fake in its place — every
    driver function below takes `run` as a parameter rather than calling
    `subprocess.run` directly, so nothing here needs a live cluster to test.
    """
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, env=env, timeout=timeout
        )
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        # `TimeoutExpired` carries whatever the child wrote as bytes, `text=True`
        # notwithstanding, and every consumer of `Run` slices and joins it as str.
        return Run(argv, TIMEOUT_RC, _text(exc.stdout), _text(exc.stderr), time.monotonic() - t0)


def _text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


# GKE sets `RECONCILING` while work proceeds on a cluster whose
# API server stays up, it is transient and ordinary, and excluding it dropped
# clusters from audits for the duration of any routine config change. Each
# collector is a standalone script with no shared import, so the set is
# duplicated rather than imported (`fleet_drift.VOTING_STATUSES` is the same
# two states).
AUDITABLE_STATUSES = frozenset({"RUNNING", "RECONCILING"})


def not_running_entry(c: dict, project: str) -> dict:
    """A manifest target for a cluster whose state rules out auditing it.

    This collector filters `clusters list` down to `AUDITABLE_STATUSES`,
    which is right — a PROVISIONING cluster has no API server to read and a
    STOPPING one is on its way out. Dropping the rest on the floor is what is wrong. The
    manifest is the run's only account of the fleet it saw, so a cluster that
    never reaches it is indistinguishable from a cluster that does not exist,
    and the document is free to publish a fleet-wide all-clear over a fleet
    quietly missing it. DEGRADED is the case that makes this bite: the state
    likeliest to be worth a finding is the one the filter throws away silently.

    Recorded as a non-`collected` target, it becomes something the document has
    to place in `scope.skipped` with a reason, and `coverage_gaps` renders it as
    "not audited". `fleet_drift.py` reaches the same conclusion through
    `cluster_eligibility`; it enumerates every cluster and carries the reason as
    a limitation instead, because its checks compare clusters against each other
    rather than reading inside them.
    """
    location = c.get("location") or c.get("zone") or ""
    return {
        "name": target_name(project, location, c.get("name", "")),
        "project": project,
        "location": location,
        "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
        "outcome": OUTCOME_UNREACHABLE,
        "error": f"cluster status is {c.get('status') or 'unknown'}, which is neither RUNNING nor RECONCILING; no check was evaluated against it",
    }


def target_name(project: str, location: str, name: str) -> str:
    """What the manifest calls a cluster: `<project>/<location>/<name>`.

    A GKE name is unique only inside one project and location, and
    `audit_report._vouching_clusters` keys on the manifest's name, so two
    `seeded-a` clusters in two projects would collapse into one entry. Every
    cluster is qualified, not only one that collides today: a name qualified
    only on collision moves when the rest of the fleet changes, and a
    finding's id moves with it. `patch_readiness.target_name` states the same
    rule. A candidate's `object` stays the bare resource.
    """
    return QUALIFIED_TARGET_SEPARATOR.join([p for p in (project, location) if p] + [name])


class Discovery(NamedTuple):
    projects: list[str]
    # Set when no project could be resolved at all: an empty fleet is then a
    # failure to look, and the manifest says so with a top-level `error`.
    error: str | None
    # Set when discovery resolved part of the fleet, or the operator narrowed
    # it with `--project`. `collect_fleet` turns it into a `gate-failed`
    # target so the loss is a row the document must account for.
    partial: str | None = None


def discover_fleet(base_project: str | None, *, run: RunFn = default_run) -> Discovery:
    """The project scope. `--project` scopes the run to one project; without
    one it is the active project plus every project `gcloud projects list`
    returns -- the scope `fleet_drift.discover_fleet` and
    `patch_readiness.discover_fleet` use, so every collector audits the same
    fleet. Discovery names projects and lists none of them."""
    if base_project:
        return Discovery([base_project], None, SCOPED_RUN_NOTE.format(project=base_project))

    result = run(["gcloud", "config", "get-value", "project"])
    base = result.stdout.strip() if result.rc == 0 else ""
    projects = [base] if base else []

    list_result = run(["gcloud", "projects", "list", "--format", "value(projectId)"])
    if list_result.rc != 0:
        stderr = list_result.stderr.strip()[:ERROR_EXCERPT_CHARS] or "no stderr"
        if not projects:
            error = (
                f"project discovery failed: `gcloud config get-value project` rc={result.rc} "
                f"named no project and `gcloud projects list` rc={list_result.rc}: {stderr}"
            )
            log(f"WARNING: {error}; no project to audit")
            return Discovery([], error)
        partial = (
            f"`gcloud projects list` rc={list_result.rc}: {stderr}. The scope fell back to "
            f"the active project {base!r}; how many other projects the fleet holds is unknown."
        )
        log(f"WARNING: {partial}")
        return Discovery(projects, None, partial)

    listed = [p.strip() for p in (list_result.stdout or "").splitlines() if p.strip()]
    for candidate in listed:
        if candidate not in projects:
            projects.append(candidate)
    if base and base not in listed:
        # rc 0 and the active project absent from its own output: the listing
        # is filtered rather than complete, so the scope is provably short.
        partial = (
            f"`gcloud projects list` rc=0 did not name the active project {base!r}, "
            f"so it is filtered rather than complete: it returned {len(listed)} "
            "project(s) and this run reads clusters in one it did not return. How "
            "many other projects the fleet holds is unknown."
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


def enumerate_clusters(project: str, *, run: RunFn = default_run) -> tuple[list[dict], list[dict], str | None]:
    """Every auditable cluster in `project`, as `{name, target, location,
    project, autopilot}`, plus a manifest target for each cluster that is not,
    plus the error that makes the project a `gate-failed` target.

    `name` is the bare GKE name, which `get-credentials` and the GitOps tree's
    `clusters/<name>/` both take; `target` is what the manifest calls it.

    A failed listing returns `([], [], error)`: the project contributed
    nothing, and says so. A project whose Kubernetes Engine API is off returns
    `([], [], None)`, because it cannot hold a cluster. A listing some zones
    did not answer returns the clusters it has *and* an error, so the ones in
    the silent zones do not read as clusters that do not exist.
    """
    result = run(["gcloud", "container", "clusters", "list", "--project", project, "--format", "json"])
    if result.rc != 0:
        if any(marker in result.stderr for marker in API_DISABLED_MARKERS):
            log(f"{project}: Kubernetes Engine API is not enabled; no cluster can exist here")
            return [], [], None
        return [], [], f"clusters list rc={result.rc}: {result.stderr.strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}"
    try:
        clusters = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], [], f"clusters list returned non-JSON: {exc}"[:ERROR_EXCERPT_CHARS]
    if not isinstance(clusters, list):
        return [], [], "clusters list returned JSON that is not a list"
    running = []
    for c in clusters:
        if c.get("status") not in AUDITABLE_STATUSES:
            continue
        location = c.get("location") or c.get("zone") or ""
        running.append(
            {
                "name": c["name"],
                "target": target_name(project, location, c["name"]),
                "location": location,
                "project": project,
                "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
            }
        )
    not_running = [not_running_entry(c, project) for c in clusters if c.get("status") not in AUDITABLE_STATUSES]
    incomplete = [line.strip() for line in result.stderr.splitlines() if ZONE_TIMEOUT_MARKER in line]
    if incomplete:
        detail = " ".join(incomplete)[:ERROR_EXCERPT_CHARS]
        log(f"{project}: clusters list returned {len(clusters)} cluster(s) but is incomplete: {detail}")
        return running, not_running, f"clusters list rc=0 but incomplete: {detail}"
    return running, not_running, None


def kubeconfig_path(project: str, cluster: str, location: str) -> Path:
    return KUBECONFIG_DIR / f"kubeconfig_{project}_{cluster}_{location}.yaml"


def fetch_credentials(
    project: str, cluster: str, location: str, *, run: RunFn = default_run
) -> tuple[Path, Run]:
    """Isolated per-cluster kubeconfig — the collector's own copy of the
    convention every SOP already follows, so a parallel read of one cluster
    cannot answer with another's contents (a per-command `KUBECONFIG`,
    never `export`, which is exactly the shared state that would let
    two threads race each other's context).
    """
    kc = kubeconfig_path(project, cluster, location)
    kc.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "KUBECONFIG": str(kc)}
    result = run(
        [
            "gcloud", "container", "clusters", "get-credentials", cluster,
            "--location", location, "--project", project,
        ],
        env=env,
    )
    return kc, result


DUMP_COMMAND_KINDS = (
    "deployments,statefulsets,daemonsets,poddisruptionbudgets,horizontalpodautoscalers,services,limitranges"
    # `cronjobs,jobs` serve 3.12 alone. They are safe to add to the shared dump
    # because `normalize_workloads` filters on `WORKLOAD_KINDS`, so neither kind
    # reaches the workload set the other checks read -- and this dump has
    # exactly one caller, `_collect_obtainability`, so no other stream pays for
    # them either.
    ",cronjobs,jobs"
    # `endpointslices` serves 3.16 alone, and is here rather than as a second
    # read because it is the cheapest kind in this list: the controller writes
    # one small object per Service, so the dump grows by roughly what
    # `services` already costs. Pods would answer the same question and are
    # deliberately not dumped -- they are the largest kind in any cluster, and
    # 3.16 needs the endpoint count rather than the pods behind it.
    ",endpointslices"
    # `persistentvolumeclaims` serves 3.21 alone, and is the cheapest kind
    # here after `endpointslices`: a claim is a few hundred bytes of spec --
    # access modes, a storage class, a size -- and a cluster holds one per
    # volume rather than one per pod. It carries nothing from inside the
    # volume and no credential of any kind; a claim is a request for storage,
    # not the storage.
    ",persistentvolumeclaims"
)

# How long a CronJob must have been firing without a success before 3.12 calls
# it chronically failing, measured as `lastScheduleTime - lastSuccessfulTime`.
#
# Both ends come out of the dump, so this check needs no clock -- which is why
# it is a gap between two recorded times rather than an age against `now`.
# Nothing else in this file reads wall-clock time, the tests would have to
# freeze it, and a collector whose verdict depends on when it ran is a
# collector whose findings cannot be reproduced from its own manifest.
#
# Deliberately not scaled to the schedule's period. The instinct is that a
# weekly CronJob should get a week of grace and an hourly one an hour, but that
# has it backwards: the rarer the schedule, the *more* a single failure costs,
# because the next attempt is a week away rather than sixty seconds. A flat
# floor gives an hourly job twenty-four consecutive failures before it is worth
# saying anything, and reports a weekly job the day after its one failure --
# which is the right answer in both directions. It also avoids parsing cron
# expressions, which this collector has no reason to learn.
STALE_SUCCESS_GAP_HOURS = 24
SECONDS_PER_HOUR = 3600
# Minutes, not hours, for the overlap check's excerpt: the schedules it can
# fire on are the ones whose period a run can outlast, and printing "0.1h"
# where a reader wants "6m" hides the comparison the finding is making.
SECONDS_PER_MINUTE = 60


def run_and_gate(
    argv: list[str],
    kubeconfig: Path,
    *,
    run: RunFn = default_run,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> tuple[dict | None, Run]:
    """One collection command, behind a fail-closed gate.

    The gate is the ai-security SOP's pattern (its §2 manual fallback):
    a `kubectl`/`gcloud` that failed leaves empty or truncated output, and
    reading that as "nothing here" is indistinguishable from a genuinely
    empty result unless something checks the output is well-formed *before*
    any check trusts it. Returns `(parsed_json_or_None, command_run)` —
    `None` means the gate failed; the caller records that as
    `outcome: "gate-failed"` for the whole cluster, never as a shorter
    candidate list from the checks that happened to run first.

    A `kubectl get <kinds> ... -o json` list response gates on `.items`
    being a list; a `gcloud ... --format=json(...)` object response has no
    such envelope, so it gates on parsing as an object at all. Both share
    the same failure mode this function exists to catch: exit 0 with
    truncated or empty output, which the credential proxy's output cap makes
    a real possibility, not a theoretical one. That cap is 8 MiB per stream
    as the operator deploys it, 4 MiB if `CREDENTIAL_PROXY_MAX_OUTPUT_BYTES`
    is unset; either way a 16-cluster `-A -o json` dump can reach it.
    """
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    result = run(argv, env=env, timeout=timeout)
    if result.rc != 0 or not result.stdout.strip():
        return None, result
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, result
    if "get" in argv and isinstance(parsed, dict) and not isinstance(parsed.get("items"), list):
        return None, result
    if not isinstance(parsed, (dict, list)):
        return None, result
    return parsed, result


class GateFailure(Exception):
    """Raised by a stream's context builder when a required collection
    command fails its gate. `collect_cluster` turns this into
    `outcome: "gate-failed"` for the whole cluster — a stream that collects
    from several commands fails closed on any one of them, the same way a
    single-dump stream does; a partially-gated cluster is not a smaller
    success, it is the false-all-clear shape at a different scale."""


def dump_state(
    kubeconfig: Path, cluster: str, *, project: str = "", location: str = "", run: RunFn = default_run
) -> tuple[Path, Run, bool]:
    """`obtainability-audit`'s one dump, behind `run_and_gate`. Kept as its
    own function (rather than inlined into its context builder) because its
    fixed dump-to-a-named-file shape predates the multi-collection builder
    contract and nothing else needs a file on disk — every check reads the
    parsed dict `run_and_gate` already returns.

    Keyed the way `kubeconfig_path` is, on the whole `(project, cluster,
    location)` triple. The design's thread-safety rule is that a worker writes
    only to paths keyed by its own cluster, and it named this file as the
    example of a name no two threads can collide on — but a cluster name is
    unique within a project, not across the fleet, and this collector runs
    eight projects at once. Two clusters called `prod` in two projects wrote
    the same path, and the loser re-read the winner's dump: not a truncated
    file or a crash, but one cluster's workloads published under the other's
    name, with a manifest recording a clean rc=0 read.
    """
    dump_path = Path(SCRATCH_DIR) / f"wra_state_{project}_{cluster}_{location}.json"
    parsed, result = run_and_gate(["kubectl", "get", DUMP_COMMAND_KINDS, "-A", "-o", "json"], kubeconfig, run=run)
    if parsed is not None:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(result.stdout, encoding="utf-8")
    return dump_path, result, parsed is not None


def output_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# obtainability-audit: §3.1 `no-requests`, §3.2 `no-memory-limit`.
#
# Both are pure functions over the parsed dump — no subprocess, no cluster
# access — which is what "detection is code that happens to be written in
# prose" means concretely: everything below is a direct
# transcription of the SOP's "Flag when" / "Do NOT flag" prose, and it is
# exercised by golden-dump tests the prose alone could never have.
# --------------------------------------------------------------------------- #

WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")
#: The group every kind in `WORKLOAD_KINDS` lives in. Named because a
#: `scaleTargetRef` is matched on it as well as on kind and name.
_WORKLOAD_API_VERSION = "apps/v1"
#: Kubernetes' own default when an HPA omits `minReplicas`. Named because an
#: absent floor and a floor of one are the same workload: `hpa-floors-at-one`
#: has to say which of the two it found, and `blocking-pdb` has to compare
#: against the same number either way.
DEFAULT_HPA_MIN_REPLICAS = 1
OPT_OUT_KEY = "kubeagents.x-k8s.io/reliability-audit"

# The owners S3 is entitled to defer to. Kind and API group both have to
# match: `Job` in `batch` is the built-in whose children S5 drops, while a
# `Job` some operator defines in its own group is a CRD wearing the name.
BUILTIN_OWNER_GROUPS = frozenset({"", "apps", "batch"})
BUILTIN_OWNER_KINDS = frozenset(WORKLOAD_KINDS) | {
    "ReplicaSet",
    "ReplicationController",
    "Job",
    "CronJob",
}


def _defers_to_owner(meta: dict) -> bool:
    """True when S3's promise — audit the owning controller instead — is one
    this audit can keep.

    S3 skips an owned workload on the grounds that its replica count, PDB, and
    probes belong to its controller rather than to a human. That holds for a
    built-in controller: the audit reads Deployments, StatefulSets, and
    DaemonSets directly, a ReplicaSet's own owner is one of those, and S5 puts
    Jobs and CronJobs out of scope outright. It does not hold for a CRD the
    audit never dumps. There the deferral has nowhere to defer to — the
    workload is dropped and the owner is never looked at either — so a real gap
    goes unreported forever instead of being reported against a better object.

    Across the sixteen clusters of this fleet, S3 as an unconditional
    `ownerReferences` test suppressed exactly one workload:
    `kubeagents-system/platform-agent-gateway`, owned by a `PlatformAgent`.
    That is the harness's own gateway, in the one namespace S1 deliberately
    keeps in scope so that the harness audits itself. The rule was costing
    nothing but its single counterexample, and the counterexample was the
    object the surrounding prose most wanted covered.
    """
    for ref in meta.get("ownerReferences") or []:
        api_version = str(ref.get("apiVersion", ""))
        # `apps/v1` → `apps`; `v1` and an absent apiVersion → the core group.
        group = api_version.rsplit("/", 1)[0] if "/" in api_version else ""
        if str(ref.get("kind", "")) in BUILTIN_OWNER_KINDS and group in BUILTIN_OWNER_GROUPS:
            return True
    return False


# The markers a reconciling controller stamps on an object it owns, and the
# phrase each one licenses. All three are set by the writer at apply time and
# read here off the same dump every check already runs on, so this costs no
# extra call.
#
# Helm 3 writes both halves of its pair on every object in a release, and the
# release namespace is not the object's -- cert-manager's Deployments live in
# `cert-manager` under a release of the same name, while the kube-agents chart
# installs into `kubeagents-system`. Argo CD's tracking id is
# `<application>:<group>/<Kind>:<namespace>/<name>`, so the Application name is
# the leading segment. `app.kubernetes.io/managed-by` is the fallback, and only
# the fallback: Helm sets it to the literal `Helm`, which names no release and
# is strictly worse than the annotation pair beside it.
_HELM_RELEASE_ANNOTATION = "meta.helm.sh/release-name"
_HELM_NAMESPACE_ANNOTATION = "meta.helm.sh/release-namespace"
_ARGOCD_TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
_HELM_MANAGED_BY = "helm"


def reconciler_of(meta: dict) -> str | None:
    """What continuously reasserts this object's spec, named, or None.

    The fact a `manual` remediation needs and never carried. A manual
    remediation is prose telling a reader to make a change by hand, and on an
    object some controller reconciles, the hand-made change is undone -- by the
    next `helm upgrade`, by Argo CD's next sync, by an operator within seconds.
    The ledger said none of that. On 2026-09-06, 23 of the fleet's 67
    workload-scoped findings were `manual` on a reconciled object: twelve under
    Argo CD, ten under Helm, and one -- `probes-liveness` on
    `Deployment/platform-agent-gateway` -- under the harness's own operator.
    Compliance's `podsecurity-gaps` finding on `Deployment/litellm` was the
    clearest of them, telling the reader the workload was "applied out-of-band"
    and to patch the Deployment directly, when it is a Helm release the
    kube-agents chart declares.

    Same defect 749f8304 fixed one layer down, where a `gcloud` remediation
    proposed a change Config Connector would revert. This is its Kubernetes
    half.

    Returns a noun phrase, not a slug, because the only consumer is a sentence.
    None means no marker was found -- an object applied with `kubectl apply -f`
    and reconciled by nobody, where a hand-applied change is exactly as durable
    as the finding implies.
    """
    annotations = meta.get("annotations") or {}
    release = str(annotations.get(_HELM_RELEASE_ANNOTATION) or "").strip()
    if release:
        namespace = str(annotations.get(_HELM_NAMESPACE_ANNOTATION) or "").strip()
        where = f" in {namespace}" if namespace else ""
        return f"the Helm release `{release}`{where}"
    tracking = str(annotations.get(_ARGOCD_TRACKING_ANNOTATION) or "").strip()
    if tracking:
        return f"the Argo CD Application `{tracking.split(':', 1)[0]}`"
    managed_by = str((meta.get("labels") or {}).get(_MANAGED_BY_LABEL) or "").strip()
    if managed_by and managed_by.lower() != _HELM_MANAGED_BY:
        return f"`{managed_by}`"
    return None


def release_of(meta: dict) -> dict | None:
    """The chart release holding this object, as data, or None.

    `reconciler_of` above answers the same question in prose, for a sentence in
    the finding. This answers it in the form `release_declaration_for` can look
    up: `{"namespace", "name", "application"}`, any of which may be `""`.

    The two markers are disjoint in practice and both are read. Helm's client
    writes `meta.helm.sh/release-{name,namespace}` when it installs, so those
    are what a `helm install` or a Flux-driven release leaves behind. Argo CD
    renders a chart with `helm template` and applies the output, which carries
    no such annotation -- the ownership marker is `argocd.argoproj.io/tracking-id`,
    whose leading segment is the Application name. A workload can carry both if
    Argo CD is driving Helm through a plugin, and then both keys are worth
    trying, so this returns whatever it finds rather than choosing.

    None means no marker at all: an object applied with `kubectl apply -f`,
    whose own manifest `workload_declarations` already resolves.
    """
    annotations = meta.get("annotations") or {}
    name = str(annotations.get(_HELM_RELEASE_ANNOTATION) or "").strip()
    namespace = str(annotations.get(_HELM_NAMESPACE_ANNOTATION) or "").strip()
    tracking = str(annotations.get(_ARGOCD_TRACKING_ANNOTATION) or "").strip()
    application = tracking.split(":", 1)[0].strip() if tracking else ""
    if not name and not application:
        return None
    return {"namespace": namespace, "name": name, "application": application}


def normalize_workloads(dump: dict) -> list[dict]:
    """Every workload template surviving S1–S5, as `{kind, ns, name, spec}`.

    Templates, not live pods (`spec.template.spec`) — admission-time
    defaulting never reaches here, matching the SOP's own reason for reading
    templates over Pods.
    """
    out = []
    for item in dump.get("items", []) or []:
        if item.get("kind") not in WORKLOAD_KINDS:
            continue
        meta = item.get("metadata") or {}
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):  # S1
            continue
        labels = meta.get("labels") or {}
        annotations = meta.get("annotations") or {}
        if "addonmanager.kubernetes.io/mode" in labels:  # S2
            continue
        if _defers_to_owner(meta):  # S3
            continue
        if labels.get(OPT_OUT_KEY) == "exempt" or annotations.get(OPT_OUT_KEY) == "exempt":  # S4
            continue
        spec = item.get("spec") or {}
        if spec.get("replicas") == 0:  # S5 (Job/CronJob ownership is covered by S3 above)
            continue
        template_meta = (spec.get("template") or {}).get("metadata") or {}
        template_spec = (spec.get("template") or {}).get("spec") or {}
        out.append(
            {
                "kind": item["kind"],
                "ns": ns,
                "name": meta.get("name", ""),
                "spec": spec,
                "template": template_spec,
                "pod_labels": template_meta.get("labels") or {},
                "reconciler": reconciler_of(meta),
                "release": release_of(meta),
            }
        )
    return out


def selector_matches(selector: dict, labels: dict) -> bool:
    """Kubernetes `LabelSelector` semantics: `matchLabels` (exact) and every
    `matchExpressions` term are ANDed together. An absent or empty selector
    matches every pod in the namespace — the exact footgun 3.3's remediation
    guards against emitting, not a bug to guard against here; callers that
    read a *live* selector into this function are the ones responsible for
    that check.
    """
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr.get("key"), expr.get("operator"), expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and labels.get(key) in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def _by_namespace(dump: dict, kind: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in dump.get("items", []) or []:
        if item.get("kind") != kind:
            continue
        ns = (item.get("metadata") or {}).get("namespace", "")
        out.setdefault(ns, []).append(item)
    return out


def limitranges_by_namespace(dump: dict) -> dict[str, list[dict]]:
    return _by_namespace(dump, "LimitRange")


def pdbs_by_namespace(dump: dict) -> dict[str, list[dict]]:
    return _by_namespace(dump, "PodDisruptionBudget")


def services_by_namespace(dump: dict) -> dict[str, list[dict]]:
    return _by_namespace(dump, "Service")


def hpas_by_namespace(dump: dict) -> dict[str, list[dict]]:
    """Every HPA, owned ones included; see `_hpa_is_owned` for who skips them."""
    return _by_namespace(dump, "HorizontalPodAutoscaler")


def _hpa_is_owned(hpa: dict) -> bool:
    """An HPA carrying `ownerReferences`, KEDA's `ScaledObject` included.

    Skipped only by the checks that grade the HPA's own `min`/`max` (3.6,
    3.22): on an owned HPA those are a copy the operator writes from a CRD
    this audit cannot read, so grading them grades a projection. The checks
    that ask whether a workload is autoscaled at all (3.4, 3.5, 3.11) keep
    it -- a KEDA-scaled Deployment is autoscaled, and its floor is the one
    KEDA wrote. This is not `_defers_to_owner`: a workload owned by a CRD
    still answers for its own PDB and probe gaps.
    """
    return bool((hpa.get("metadata") or {}).get("ownerReferences"))


def workload_keys(dump: dict) -> set[tuple[str, str, str]]:
    """Every workload in the dump as `(ns, kind, name)`, suppressed or not.

    "Is this object this audit's business" and "does this object exist" are
    different questions, and `workloads` only answers the first. A check that
    reports a reference as broken is answering the second, so it has to read
    the dump rather than the audited set — otherwise opting a Deployment out of
    the audit is enough to make something else report it as missing.
    """
    return {
        ((item.get("metadata") or {}).get("namespace", ""), item["kind"], (item.get("metadata") or {}).get("name", ""))
        for item in dump.get("items", []) or []
        if item.get("kind") in WORKLOAD_KINDS
    }


def cronjobs_with_jobs(dump: dict) -> list[dict]:
    """Every non-system CronJob paired with the Jobs it still retains.

    Joined here rather than in the check because the join is the awkward part:
    a Job names its CronJob in `ownerReferences`, so the dump has to be walked
    once per CronJob to find them, and doing that inside the check would make
    it quadratic on a fleet where one namespace holds hundreds of Jobs.

    S1, S2 and S4 are applied here. S3 is not, because it would drop every
    CronJob in the fleet -- a CronJob is owned by nothing. Neither is S5, which
    exists to keep Jobs and CronJobs out of the *workload* checks and is
    exactly the exclusion 3.12 is written to reach past; the SOP says so under
    S5 rather than leaving a reader to infer it from this function.

    S4 matters more here than anywhere else in this file. Every other check in
    this audit reports a shape the operator can change, so an unwanted finding
    can be answered by fixing it. This one reports that a schedule is failing,
    and an operator who knows why and is not going to act -- a CronJob kept
    deliberately broken, one whose fix is waiting on something else -- has no
    way to make it stop except the opt-out label.
    """
    out = []
    for item in dump.get("items", []) or []:
        if item.get("kind") != "CronJob":
            continue
        meta = item.get("metadata") or {}
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):  # S1
            continue
        labels = meta.get("labels") or {}
        annotations = meta.get("annotations") or {}
        if "addonmanager.kubernetes.io/mode" in labels:  # S2
            continue
        if labels.get(OPT_OUT_KEY) == "exempt" or annotations.get(OPT_OUT_KEY) == "exempt":  # S4
            continue
        name = meta.get("name", "")
        owned = [
            job
            for job in dump.get("items", []) or []
            if job.get("kind") == "Job"
            and (job.get("metadata") or {}).get("namespace", "") == ns
            and any(
                ref.get("kind") == "CronJob" and ref.get("name") == name
                for ref in ((job.get("metadata") or {}).get("ownerReferences") or [])
            )
        ]
        owned.sort(key=lambda job: (job.get("metadata") or {}).get("creationTimestamp", ""))
        out.append({"cronjob": item, "ns": ns, "name": name, "jobs": owned})
    return out


# Each EndpointSlice names the Service it belongs to in this label; it is how
# the controller groups slices, and it is the whole join.
ENDPOINTSLICE_SERVICE_LABEL = "kubernetes.io/service-name"

# A Service of one of these types has an address published outside the
# cluster, so one that resolves to nothing is a black hole something external
# is already pointed at rather than a broken in-cluster reference.
EXTERNAL_SERVICE_TYPES = frozenset({"LoadBalancer", "NodePort"})

# `ExternalName` is a DNS alias with no selector and no endpoints, so it is not
# a Service that can select nothing. Headless Services (`clusterIP: None`) are
# not excluded: they select pods and resolve to their addresses like any other,
# and a headless Service selecting nothing breaks its clients the same way.
EXTERNALNAME_SERVICE_TYPE = "ExternalName"

# `spec.type` defaults to this and is therefore usually unstated, so an excerpt
# that just echoed the field would read `type=None` on most Services.
DEFAULT_SERVICE_TYPE = "ClusterIP"

# The one `spec.type` that asks the cloud provider for a public address and a
# forwarding rule, which is the whole premise of `check_lb_world_open`.
SERVICE_TYPE_LOAD_BALANCER = "LoadBalancer"


def services_with_endpoints(dump: dict) -> list[dict]:
    """Every non-system, pod-selecting Service paired with what its
    EndpointSlices actually resolve to.

    EndpointSlice rather than the v1 Endpoints API, which is deprecated from
    Kubernetes 1.33 — a check written against a deprecated kind acquires an
    expiry date on the day it ships. Each slice names its Service in a label,
    so the join is a lookup rather than a walk of `subsets`.

    Joined here for the reason `cronjobs_with_jobs` is: done inside the check,
    the dump would be walked once per Service, which is quadratic in a
    namespace holding hundreds of them.

    Terminating endpoints are not counted. A pod being drained during a rollout
    is still listed in the slice, with `conditions.terminating` set, and
    counting it would make this check read a routine rollout as healthy at the
    exact moment it is not — and, worse, would let a Service that genuinely
    resolves to nothing look fine for as long as one old pod takes to go away.

    `resolved_ports` and `named_targets` serve §3.17 and answer a second
    question off the same join: which of this Service's ports the controller
    could actually resolve. A `targetPort` given as a name is resolved against
    the pod's declared `containerPort`s, and when no container declares that
    name the endpoint-slice controller drops the port from the slice entirely
    rather than writing it with a null number — so a service-port name missing
    from `resolved_ports` is the controller saying it could not resolve it. A
    numeric `targetPort` always resolves and is therefore not collected.

    S1, S2 and S4 are applied, as for CronJobs. S3 is not: an operator-owned
    Service black-holes traffic exactly like a hand-written one, and
    `reconciler` is what tells the reader the fix belongs in the operator's
    chart rather than in the cluster's tree. S5 has no meaning for a Service,
    which has no replicas — the workload behind it does, and that is
    `zeroed_by` below rather than an exclusion here.
    """
    slices_by_service: dict[tuple[str, str], list[dict]] = {}
    for item in dump.get("items", []) or []:
        if item.get("kind") != "EndpointSlice":
            continue
        meta = item.get("metadata") or {}
        owner = (meta.get("labels") or {}).get(ENDPOINTSLICE_SERVICE_LABEL)
        if not owner:
            continue
        slices_by_service.setdefault((meta.get("namespace", ""), owner), []).append(item)

    out = []
    for item in dump.get("items", []) or []:
        if item.get("kind") != "Service":
            continue
        meta = item.get("metadata") or {}
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):  # S1
            continue
        labels = meta.get("labels") or {}
        annotations = meta.get("annotations") or {}
        if "addonmanager.kubernetes.io/mode" in labels:  # S2
            continue
        if labels.get(OPT_OUT_KEY) == "exempt" or annotations.get(OPT_OUT_KEY) == "exempt":  # S4
            continue
        spec = item.get("spec") or {}
        selector = spec.get("selector") or {}
        # No selector at all means the endpoints are managed by hand -- the
        # sanctioned way to point a Service at an address outside the cluster.
        # There is nothing to select, so nothing to select wrongly.
        if not selector or spec.get("type") == EXTERNALNAME_SERVICE_TYPE:
            continue
        name = meta.get("name", "")
        live = 0
        resolved: set[str] = set()
        endpoint_nodes: list[str] = []
        for slice_ in slices_by_service.get((ns, name), []):
            for endpoint in slice_.get("endpoints") or []:
                conditions = endpoint.get("conditions") or {}
                if conditions.get("terminating"):
                    continue
                live += 1
                # §3.19's evidence: where the scheduler actually put the pod.
                # `ready` is tri-state and absent means ready, so only an
                # explicit False is excluded. An endpoint with no `nodeName`
                # is not a pod on a node -- it is skipped rather than counted
                # as an unknown node, which would read as spread.
                if conditions.get("ready") is not False and endpoint.get("nodeName"):
                    endpoint_nodes.append(endpoint["nodeName"])
            for port in slice_.get("ports") or []:
                if port.get("port") is not None:
                    resolved.add(port.get("name") or "")
        named_targets = [
            (p.get("name") or "", p["targetPort"])
            for p in spec.get("ports") or []
            if isinstance(p.get("targetPort"), str)
        ]
        # Only walked where a named target went unresolved, which is rare —
        # otherwise every Service in the dump would pay for a second pass over
        # every workload in it.
        backend, declared = None, []
        if live and any(port not in resolved for port, _ in named_targets):
            backend, declared = _container_ports_behind(dump, ns, selector)
        out.append(
            {
                "service": item,
                "ns": ns,
                "name": name,
                "selector": selector,
                "endpoints": live,
                "zeroed_by": _selector_matches_a_zeroed_workload(dump, ns, selector),
                "resolved_ports": resolved,
                "named_targets": named_targets,
                "backend": backend,
                "declared_ports": declared,
                "endpoint_nodes": endpoint_nodes,
            }
        )
    return out


def nodes_named_by_endpoints(dump: dict) -> set[str]:
    """Every node any EndpointSlice in the cluster names.

    Nodes are not a kind this audit collects, so this is the only evidence in
    the dump that the cluster has more than one of them. §3.19 needs exactly
    that much and no more: a cluster whose slices never name a second node
    cannot be faulted for putting a workload's replicas together.

    System namespaces are deliberately included. The question is about the
    cluster, not about the audited workloads, and `kube-system`'s slices are
    usually the widest sample of nodes available here.
    """
    nodes: set[str] = set()
    for item in dump.get("items", []) or []:
        if item.get("kind") != "EndpointSlice":
            continue
        for endpoint in item.get("endpoints") or []:
            if endpoint.get("nodeName"):
                nodes.add(endpoint["nodeName"])
    return nodes


def zones_named_by_endpoints(dump: dict) -> set[str]:
    """Every failure-injection zone any EndpointSlice in the cluster names.

    Read for the same reason as the node set and from the same field family:
    `endpoints[].zone` is the node's `topology.kubernetes.io/zone` label,
    copied onto the slice by the endpoint controller. It answers the one
    question §3.19's remediation turns on that the node set cannot -- whether
    zone-keyed spreading can do anything here at all. On a zonal cluster every
    node shares one zone, so raising a zone-keyed constraint to
    `DoNotSchedule` leaves the skew at nought and changes no placement: the
    pull request merges, the finding survives, and the reviewer's trust is
    spent on a no-op. Same sampling rule as the node set -- system namespaces
    included, because the question is about the cluster.
    """
    zones: set[str] = set()
    for item in dump.get("items", []) or []:
        if item.get("kind") != "EndpointSlice":
            continue
        for endpoint in item.get("endpoints") or []:
            if endpoint.get("zone"):
                zones.add(endpoint["zone"])
    return zones


def _selector_matches_a_zeroed_workload(dump: dict, ns: str, selector: dict) -> str | None:
    """`Kind/name` of a workload this selector matches that is scaled to zero.

    A Service with no endpoints because its only backend is deliberately at
    `replicas: 0` is not broken, and this is the only way to tell that apart
    from a selector that matches nothing at all. It reads the dump rather than
    the audited workload set on purpose: S5 drops zeroed workloads from that
    set, so asking it would return nothing and every scaled-down Service in
    the fleet would be reported as a black hole.
    """
    for item in dump.get("items", []) or []:
        if item.get("kind") not in WORKLOAD_KINDS:
            continue
        meta = item.get("metadata") or {}
        if meta.get("namespace", "") != ns:
            continue
        spec = item.get("spec") or {}
        if spec.get("replicas") != 0:
            continue
        pod_labels = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
        if selector_matches({"matchLabels": selector}, pod_labels):
            return f"{item['kind']}/{meta.get('name', '')}"
    return None


def _container_ports_behind(dump: dict, ns: str, selector: dict) -> tuple[str | None, list[str]]:
    """The first workload this selector matches, and every container port it names.

    What the remediation needs: a Service asking for a `targetPort` no
    container declares is fixed by naming one that is declared, and this is
    where the candidate names come from. Unnamed ports are rendered as their
    number, because `targetPort: 8080` is as valid an answer as `targetPort:
    http` and a reader given only the named ones would think there were none.

    Reads the dump rather than the audited workload set for the reason
    `_selector_matches_a_zeroed_workload` does — the exclusions are about what
    is worth auditing, not about what is behind a Service.
    """
    for item in dump.get("items", []) or []:
        if item.get("kind") not in WORKLOAD_KINDS:
            continue
        meta = item.get("metadata") or {}
        if meta.get("namespace", "") != ns:
            continue
        template = (item.get("spec") or {}).get("template") or {}
        pod_labels = (template.get("metadata") or {}).get("labels") or {}
        if not selector_matches({"matchLabels": selector}, pod_labels):
            continue
        declared = []
        pod_spec = template.get("spec") or {}
        # Native sidecars serve ports like any other container, so a named
        # `targetPort` one of them declares is declared -- the same reading
        # `_effective_containers` gives the audited workloads.
        sidecars = [ic for ic in pod_spec.get("initContainers") or [] if ic.get("restartPolicy") == "Always"]
        for container in (pod_spec.get("containers") or []) + sidecars:
            for port in container.get("ports") or []:
                declared.append(port.get("name") or str(port.get("containerPort", "")))
        return f"{item['kind']}/{meta.get('name', '')}", declared
    return None, []


def claims_by_key(dump: dict) -> dict[tuple[str, str], dict]:
    """Every PersistentVolumeClaim in the dump, keyed `(namespace, name)`.

    Keyed rather than grouped by namespace, which is how every other kind here
    is indexed, because §3.21's read is always one exact claim: a pod names it
    in `volumes[].persistentVolumeClaim.claimName` and that name is
    namespace-local, so grouping would only make each read walk a list to find
    the single entry it already knows the name of.
    """
    return {
        (
            (item.get("metadata") or {}).get("namespace", ""),
            (item.get("metadata") or {}).get("name", ""),
        ): item
        for item in dump.get("items", []) or []
        if item.get("kind") == "PersistentVolumeClaim"
    }


def build_context(dump: dict, workloads: list[dict]) -> dict:
    return {
        "claims": claims_by_key(dump),
        "limitranges": limitranges_by_namespace(dump),
        "pdbs": pdbs_by_namespace(dump),
        "hpas": hpas_by_namespace(dump),
        "services": services_by_namespace(dump),
        "cronjobs": cronjobs_with_jobs(dump),
        "service_endpoints": services_with_endpoints(dump),
        "endpoint_nodes_seen": nodes_named_by_endpoints(dump),
        "endpoint_zones_seen": zones_named_by_endpoints(dump),
        "workloads": workloads,
        "workload_keys": workload_keys(dump),
    }


def _selecting_services(workload: dict, context: dict) -> list[dict]:
    matched = []
    for svc in context["services"].get(workload["ns"], []):
        spec = svc.get("spec") or {}
        selector = spec.get("selector")
        if spec.get("type") == EXTERNALNAME_SERVICE_TYPE or not selector:
            continue
        if selector_matches({"matchLabels": selector}, workload["pod_labels"]):
            matched.append(svc)
    return matched


def _has_default(limitranges: dict, ns: str, field: str, resource: str) -> bool:
    """`field` is `"default"` (a limit) or `"defaultRequest"` (a request)."""
    for lr in limitranges.get(ns, []):
        for limit in (lr.get("spec") or {}).get("limits") or []:
            if resource in (limit.get(field) or {}):
                return True
    return False


def _effective_containers(workload: dict) -> list[dict]:
    """Regular containers plus native sidecars — `initContainers` with
    `restartPolicy: Always` count toward the pod's effective request, per
    §3.1. Plain init containers never do."""
    containers = list(workload["template"].get("containers") or [])
    for ic in workload["template"].get("initContainers") or []:
        if ic.get("restartPolicy") == "Always":
            containers.append(ic)
    return containers


_REQUEST_RESOURCES = ("cpu", "memory")

# §3.1's Impact, for the two arms where the sentence in `OBTAINABILITY_CHECKS`
# is false. That sentence ends "its pods are the first evicted under node
# pressure", which is a claim about QoS class, and only a BestEffort pod is
# first. §3.1 flags a container missing `cpu` *or* `memory`, so what it catches
# need not be BestEffort at all, and an owner told their pod goes first goes
# looking for a pressure event that reached it before it reached anything else.
#
# Note that the ubiquitous cpu-request-only workloads -- `kube-proxy`,
# `antrea-controller` -- are *not* what these arms are for, however often they
# get cited as the motivating case. They live in `kube-system`, S1 drops the
# namespace before `check_no_requests` ever runs, and no finding for either can
# exist. The arms serve user-namespace workloads in the same shape. All 7
# findings on this fleet are BestEffort, so neither arm has a live instance
# here; both were graded against the API server's own `status.qosClass`
# instead, over all 79 workloads on the host cluster.
#
# The sentence is two independent halves: what the missing requests cost, and
# what the pod's QoS class means. They vary separately -- a pod whose every
# missing request is limit-backed can still be Burstable because a *different*
# container declared a request and no ceiling -- so composing them is the only
# way each stays true. Deriving the class from the first half is what made the
# original single sentence wrong.
_QOS_BEST_EFFORT = "BestEffort"
_QOS_BURSTABLE = "Burstable"
_QOS_GUARANTEED = "Guaranteed"

# Scoped to a container, not to the pod. `unbacked_missing` is a union across
# containers, so a pod-level "sized without cpu or memory" is false as soon as
# a sibling declares one of them: two containers each limiting a different
# resource put both into the union while the pod is sized with both.
_IMPACT_UNRESERVED = (
    "{resources} goes unreserved on at least one container here, so the "
    "scheduler and cluster autoscaler size this cluster below the pod's real "
    "demand and that share of the cost cannot be attributed."
)
_IMPACT_CEILING_RESERVED = (
    "Every missing request is backed by a limit on the same container, so "
    "Kubernetes copies that limit into the request: the scheduler reserves this "
    "workload at its ceiling rather than at its steady-state size. The cost is "
    "bin-packing headroom held against a peak that may never arrive, and a "
    "reservation nobody wrote down and can review."
)
# Neither of these says "evicted first" or "evicted last by class", because the
# kubelet does not rank by class -- it sorts on whether usage exceeds requests,
# then Pod Priority, then usage relative to requests. Replacing one false
# eviction claim with another is the mistake this Impact already made once.
# §3.1's third arm. Named rather than left to `CheckSpec.impact` so the hit can
# set it like the other two: an arm that falls through to the table default is
# an arm `adopt_arm_impact` never sees, and the model is then free to publish a
# Burstable sentence over a BestEffort pod -- the exact substitution the two
# constants above exist to prevent. `OBTAINABILITY_CHECKS` points its table
# entry at this same string, so the fallback and the arm cannot drift apart.
_IMPACT_BEST_EFFORT = (
    "The scheduler and cluster autoscaler size this cluster as if this "
    "workload costs nothing; its pods are the first evicted under node "
    "pressure and its cost cannot be attributed."
)
_IMPACT_BY_QOS = {
    _QOS_GUARANTEED: (
        " Every container carries both limits with a request that matches, so "
        "the pod is Guaranteed: its usage cannot exceed its requests, which is "
        "the kubelet's first sort key under node pressure. It is in the last "
        "group evicted, not the first."
    ),
    _QOS_BURSTABLE: (
        " The pod is Burstable, not BestEffort. Eviction does not follow the "
        "class, though: the kubelet sorts on whether usage exceeds requests and "
        "then on Pod Priority, so a memory request left at zero puts a pod in "
        "the same first group as a BestEffort one, while an unreserved cpu "
        "request does not affect eviction at all."
    ),
}

# A quantity Kubernetes does not count: `0`, `0m`, `0Mi`, `0.0`. Anything that
# will not parse counts as non-zero -- a quantity this cannot read is far more
# likely to be a real reservation than a zero spelled strangely.
_QUANTITY_NUMBER = re.compile(r"^\s*([+-]?[0-9.]+)")


def _is_zero_quantity(quantity) -> bool:
    match = _QUANTITY_NUMBER.match(str(quantity))
    if not match:
        return False
    try:
        return float(match.group(1)) == 0.0
    except ValueError:
        return False


def _declared(resources: dict, field: str, resource: str) -> bool:
    """Whether `field` carries a countable quantity for `resource`.

    Kubernetes' QoS computation reads cpu and memory alone and skips any
    quantity that is not greater than zero, so `nvidia.com/gpu: 1`,
    `ephemeral-storage: 1Gi` and `cpu: 0` all leave a container silent.
    """
    quantity = (resources.get(field) or {}).get(resource)
    return quantity is not None and not _is_zero_quantity(quantity)


def _qos_containers(workload: dict) -> list[dict]:
    """Every container Kubernetes' QoS computation reads.

    Deliberately not `_effective_containers`. QoS iterates `spec.containers`
    and *all* of `spec.initContainers` with no `restartPolicy` filter, so a
    plain init container's requests decide the class even though they never
    count toward the pod's effective request — the two questions need two
    container sets, and reusing one for both is how the class came out wrong.
    Ephemeral containers are absent from both; upstream excludes them because
    they cannot declare resources.
    """
    template = workload["template"]
    return list(template.get("containers") or []) + list(template.get("initContainers") or [])


def _qos_class(containers: list[dict], limitranges: dict, namespace: str) -> str:
    """Kubernetes' QoS algorithm, read off the workload spec.

    Guaranteed needs every container to carry a `cpu` *and* a `memory` limit
    above zero with a request equal to it; an absent request is copied from the
    limit, so a container declaring limits alone qualifies. A pod where no
    container declares cpu or memory at all is BestEffort. Everything else is
    Burstable.

    Wrong in one direction on purpose. Two spellings of one quantity (`100m`
    and `0.1`) compare unequal, and a LimitRange `defaultRequest` can inject a
    request below the limit; both send the pod to Burstable. Burstable is the
    branch that claims the least, so an unmodelled shape landing there
    understates rather than misstates.
    """
    declares_compute = False
    guaranteed = True
    for container in containers:
        resources = container.get("resources") or {}
        for resource in _REQUEST_RESOURCES:
            has_request = _declared(resources, "requests", resource)
            has_limit = _declared(resources, "limits", resource)
            declares_compute = declares_compute or has_request or has_limit
            if not has_limit:
                guaranteed = False
            elif has_request:
                if resources["requests"][resource] != resources["limits"][resource]:
                    guaranteed = False
            elif _has_default(limitranges, namespace, "defaultRequest", resource):
                guaranteed = False
    if not declares_compute:
        return _QOS_BEST_EFFORT
    return _QOS_GUARANTEED if guaranteed else _QOS_BURSTABLE


def check_no_requests(workload: dict, context: dict) -> dict | None:
    limitranges = context["limitranges"]
    containers = _effective_containers(workload)
    missing_by_container = {}
    # Whether a limit already covers every request the check is about to report
    # missing. A property of the pod, not of one container, and half of the
    # Impact; the QoS class below is the other half.
    unbacked_missing: set[str] = set()
    for container in containers:
        resources = container.get("resources") or {}
        requests, limits = resources.get("requests") or {}, resources.get("limits") or {}
        missing = [
            resource
            for resource in _REQUEST_RESOURCES
            if resource not in requests
            and not _has_default(limitranges, workload["ns"], "defaultRequest", resource)
        ]
        # A limit with no request is not an unreserved resource: Kubernetes
        # copies the limit into the request before the scheduler sees the pod.
        # It stays a finding -- §3.1 wants the request declared, not inferred
        # from a ceiling -- but it is the opposite failure from an unreserved
        # one, so it must not draw the unreserved sentence.
        unbacked_missing |= {resource for resource in missing if resource not in limits}
        if missing:
            missing_by_container[container.get("name", "")] = missing
    if not missing_by_container:
        return None
    hit = {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": "; ".join(f"{c}: missing {','.join(m)}" for c, m in missing_by_container.items()),
    }
    qos = _qos_class(_qos_containers(workload), limitranges, workload["ns"])
    if qos == _QOS_BEST_EFFORT:
        # Same string the table carries, set here so this arm is flagged
        # authoritative like the other two rather than falling through.
        hit["impact"] = _IMPACT_BEST_EFFORT
        return hit
    if unbacked_missing:
        head = _IMPACT_UNRESERVED.format(resources=" or ".join(sorted(unbacked_missing)))
    else:
        head = _IMPACT_CEILING_RESERVED
    hit["impact"] = head + _IMPACT_BY_QOS[qos]
    return hit


# §3.2 has two arms and they point in opposite directions, because the kubelet
# ranks a memory-pressure eviction on whether usage exceeds the memory *request*
# -- never on the QoS class. An uncapped container that also declares no request
# exceeds zero on its first byte and sits in the first bucket from the start; an
# uncapped container with a request is in the last bucket until the leak passes
# that request. Only the second one shields itself at the neighbours' expense,
# and it is the first that every live finding is.
#
# kubernetes.io contradicts itself here -- `pod-qos.md` still says Burstable pods
# "are evicted only after all BestEffort Pods are evicted", which upstream issue
# #129759 has open against the code -- so both arms name the sort keys rather
# than asserting an order a reader can find an official page against.
_IMPACT_NO_LIMIT_UNREQUESTED = (
    "Nothing caps memory here and no memory request is declared either, so a "
    "leak makes this workload the node's first casualty rather than its "
    "neighbours'. The kubelet ranks memory-pressure eviction by whether usage "
    "exceeds the memory request, then by Pod Priority, then by how far usage "
    "sits above that request — never by QoS class — so a request of zero puts "
    "this pod in the first group from its first byte, and at equal priority the "
    "leak ranks it ahead of the BestEffort pods beside it. The kernel's OOM "
    "killer is a separate mechanism that can fire before the kubelet reacts, "
    "and it scores these containers at the top of its range too."
)
_IMPACT_NO_LIMIT_REQUESTED = (
    "Nothing caps memory here, so a leak grows until the node is under "
    "pressure, and up to the declared request the kubelet does evict "
    "co-located workloads first. Past that request this pod joins the group "
    "evicted first, ranked by how far its usage exceeds it. The kernel's OOM "
    "killer scores a container down in proportion to its memory request, so "
    "the larger the request the more of the node a leak here can take before "
    "the kernel picks it over a neighbour."
)


def check_no_memory_limit(workload: dict, context: dict) -> dict | None:
    limitranges = context["limitranges"]
    missing, unrequested, requested = [], [], []
    for container in workload["template"].get("containers") or []:
        resources = container.get("resources") or {}
        if "memory" in (resources.get("limits") or {}):
            continue
        name = container.get("name", "")
        missing.append(name)
        # Key presence decides whether the limit is missing, matching §3.1's
        # reading of the manifest; the request is read with `_declared`
        # instead, because the arm turns on the quantity the eviction ranking
        # subtracts and `requests.memory: 0` is a request of zero. A LimitRange
        # `defaultRequest` counts: it is injected before the scheduler sees the
        # pod, so the ranking reads it even though the manifest is silent.
        if _declared(resources, "requests", "memory") or _has_default(
            limitranges, workload["ns"], "defaultRequest", "memory"
        ):
            requested.append(name)
        else:
            unrequested.append(name)
    if not missing or _has_default(limitranges, workload["ns"], "default", "memory"):
        return None
    if unrequested and requested:
        impact = (
            f"{', '.join(unrequested)}: {_IMPACT_NO_LIMIT_UNREQUESTED} "
            f"{', '.join(requested)}: {_IMPACT_NO_LIMIT_REQUESTED}"
        )
    else:
        impact = _IMPACT_NO_LIMIT_UNREQUESTED if unrequested else _IMPACT_NO_LIMIT_REQUESTED
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"containers missing a memory limit: {', '.join(missing)}",
        "impact": impact,
    }


def check_no_pdb(workload: dict, context: dict) -> dict | None:
    if workload["kind"] == "DaemonSet":
        return None
    replicas = workload["spec"].get("replicas", 1) or 1
    if replicas < 2:
        return None
    for pdb in context["pdbs"].get(workload["ns"], []):
        # A null selector selects nothing in policy/v1; only `{}` selects all.
        selector = (pdb.get("spec") or {}).get("selector")
        if selector is not None and selector_matches(selector, workload["pod_labels"]):
            return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"replicas={replicas}, no PodDisruptionBudget matches this workload's pod labels",
    }


def _hpa_targeting(workload: dict, context: dict) -> dict | None:
    """The HorizontalPodAutoscaler that owns this workload's replica count.

    Four checks need this and each used to spell it out: `no-hpa` to stay quiet
    where one exists, `single-replica` to stay quiet for the same reason,
    `hpa-floors-at-one` to fire, and `blocking-pdb` to learn how far down the
    replica count it is comparing against can actually go. The match is on
    `scaleTargetRef`, which carries `apiVersion` as well as `kind` and `name` --
    a custom resource can be named `Deployment` in another group, and it is not
    this one.
    """
    for hpa in context["hpas"].get(workload["ns"], []):
        target = (hpa.get("spec") or {}).get("scaleTargetRef") or {}
        if (
            target.get("apiVersion") == _WORKLOAD_API_VERSION
            and target.get("kind") == workload["kind"]
            and target.get("name") == workload["name"]
        ):
            return hpa
    return None


#: Suffix marking a Kubernetes `IntOrString` as a percentage, and the scale it
#: is a percentage of. Named because `blocking-pdb` both tests for the suffix
#: and divides by the scale, and the two have to agree about which spelling of
#: `minAvailable` it is resolving.
PDB_PERCENT_SUFFIX = "%"
PDB_PERCENT_SCALE = 100


def _percent_value(quantity) -> int | None:
    """The number in a `"75%"`-shaped `IntOrString`, or None for anything else.

    Kubernetes restricts the percentage form to whole numbers, so a value that
    does not parse as one is not a percentage the apiserver would have accepted
    and is left to the integer arm.
    """
    if not isinstance(quantity, str) or not quantity.endswith(PDB_PERCENT_SUFFIX):
        return None
    try:
        return int(quantity[: -len(PDB_PERCENT_SUFFIX)])
    except ValueError:
        return None


def check_blocking_pdb(context: dict) -> list[dict]:
    """Cluster-scoped: iterates PDBs, not workloads, because the finding is
    about the PDB's own shape. A PDB matching no workload (orphaned) or a
    workload scaled to zero is deliberately left unreported here — the SOP
    rates the first `minor` at most and the second not at all, and this
    conversion covers the `critical` drain-blocking case, not every PDB
    config-rot shape. Multi-match (more than one workload sharing a PDB's
    selector) is measured against every matched workload's pods together,
    as the disruption controller counts them, and reported against the first.

    Where an HPA owns the matched workload's replica count, the comparison is
    against the autoscaler's floor rather than `spec.replicas`. That field is a
    snapshot of what the autoscaler wanted when the audit read it, so comparing
    against it answers "is the drain blocked at this instant" when the question
    is "can this pair of manifests block a drain at all" — and the instant an
    audit reads is the middle of the working day, while the drain that has to
    succeed is the 03:00 node upgrade, at the floor. `hpa-floors-at-one` makes
    the same correction to `single-replica`; both are the same mistake about
    the same field.

    A percentage `minAvailable` is resolved rather than compared literally.
    Kubernetes computes `desiredHealthy = ceil(pct * expectedCount)`, so
    `minAvailable: "75%"` over three replicas is a desiredHealthy of three and
    zero permitted evictions -- the identical hard block that `minAvailable: 3`
    produces, written in the spelling people reach for precisely because it
    looks proportional and therefore safe. Every percentage over a floor of one
    resolves the same way, since `ceil` of any non-zero fraction of one is one.
    `"100%"` used to be special-cased here and is now just the case where the
    arithmetic is obvious.
    """
    hits = []
    for ns, pdbs in context["pdbs"].items():
        # S1, stated here rather than relied on. `normalize_workloads` already
        # drops system-namespace workloads, and a PDB only matches a workload in
        # its own namespace, so nothing in `kube-system` or `gke-managed-*`
        # reaches the report either way. What this line adds is the reason --
        # a blocking PDB there really does wedge every drain and is still not
        # the operator's to edit, so a `critical` naming it is a finding nobody
        # can close -- and an exit before the selector scan, which the
        # GKE-written PDBs that dominate the population would otherwise walk
        # every workload for.
        if _is_system_namespace(ns):
            continue
        for pdb in pdbs:
            spec = pdb.get("spec") or {}
            meta = pdb.get("metadata") or {}
            name = meta.get("name", "")
            # S2 against the PDB's own labels, which nothing else here checks:
            # `normalize_workloads` reads the labels on the workload, and an
            # addon's PDB over an ordinary workload carries the marker on the
            # PDB alone.
            if "addonmanager.kubernetes.io/mode" in (meta.get("labels") or {}):
                continue
            max_unavailable, min_available = spec.get("maxUnavailable"), spec.get("minAvailable")
            selector = spec.get("selector")
            if selector is None:
                continue  # selects no pods in policy/v1, so it blocks nothing
            # Every workload the selector reaches, not the first: the
            # disruption controller counts every pod the PDB selects, so two
            # Deployments of two under one `minAvailable: 2` leave two
            # evictions, not none.
            # DaemonSets left out, as `check_no_pdb` leaves them: `kubectl
            # drain --ignore-daemonsets` and the node-pool upgrade path delete
            # their pods rather than evict them, so no budget over only those wedges a
            # drain -- and with no `replicas`, one pod read as the whole floor.
            # That leaves them out of the report, not out of the decision; see
            # `unscalable` below for a budget that selects both.
            selected = [
                wl for wl in context["workloads"] if wl["ns"] == ns and selector_matches(selector, wl["pod_labels"])
            ]
            matches = [wl for wl in selected if wl["kind"] != "DaemonSet"]
            # A DaemonSet beside a scalable workload is out of the report but
            # not out of the arithmetic: the disruption controller counts its
            # pods toward the floor, and they are slack. Its pod count is one
            # per eligible node, which nothing read here records, so the floor
            # is unknown and the integer `minAvailable` arm is undecided. The
            # other two spellings are decided the other way: `maxUnavailable`
            # and a percentage `minAvailable` need every selected pod's
            # controller to expose a scale subresource to compute the expected
            # count, a DaemonSet has none, and the controller then permits no
            # evictions at all.
            daemonset_selected = len(matches) != len(selected)
            unscalable = daemonset_selected and (max_unavailable is not None or _percent_value(min_available) is not None)
            matched = matches[0] if matches else None
            replicas = sum(wl["spec"].get("replicas", 1) or 1 for wl in matches) if matches else None
            hpa = _hpa_targeting(matched, context) if len(matches) == 1 else None
            # The lowest replica count nothing has to happen for the workloads
            # to reach. Without an autoscaler that is the declared count; with
            # one it is the floor, and the declared count is a reading rather
            # than a decision.
            floor = 0
            for wl in matches:
                wl_hpa = _hpa_targeting(wl, context)
                if wl_hpa is None:
                    floor += wl["spec"].get("replicas", 1) or 1
                else:
                    declared_floor = (wl_hpa.get("spec") or {}).get("minReplicas")
                    floor += DEFAULT_HPA_MIN_REPLICAS if declared_floor is None else declared_floor
            floor = floor if matches else None
            percent = _percent_value(min_available)
            # What the disruption controller will require to be up, for the
            # percentage spelling only -- the integer spelling is already the
            # number, and `maxUnavailable` is a different computation the two
            # unconditional arms below cover between them.
            desired_healthy = (
                math.ceil(percent * floor / PDB_PERCENT_SCALE)
                if percent is not None and floor
                else None
            )
            blocking = max_unavailable in (0, "0%") or unscalable
            if matched is not None and not daemonset_selected:
                if isinstance(min_available, int) and min_available >= floor:
                    blocking = True
                if desired_healthy is not None and desired_healthy >= floor:
                    blocking = True
            if not blocking or matched is None or replicas == 0:
                continue
            scale = f"replicas={replicas}"
            if len(matches) > 1:
                scale = (
                    "with " + ", ".join(wl["kind"] + "/" + wl["name"] for wl in matches[1:]) + "; "
                    f"{floor} pods selected at the floor"
                )
            elif hpa is not None:
                scale = (
                    f"replicas={replicas} now, but scaled by "
                    f"HorizontalPodAutoscaler/{(hpa.get('metadata') or {}).get('name', '')} "
                    f"with minReplicas {floor}, which is the count the eviction API "
                    f"has to satisfy this budget at"
                )
            arithmetic = ""
            if unscalable:
                arithmetic = (
                    f"; also selects DaemonSet {', '.join(wl['name'] for wl in selected if wl['kind'] == 'DaemonSet')}, "
                    f"which has no scale subresource, so the disruption controller cannot count "
                    f"the expected pods for this spelling and permits no evictions"
                )
            elif desired_healthy is not None:
                arithmetic = (
                    f"; minAvailable {min_available} of {floor} resolves to "
                    f"desiredHealthy {desired_healthy}, leaving "
                    f"{max(floor - desired_healthy, 0)} evictions permitted at the floor"
                )
            hits.append(
                {
                    "namespace": ns,
                    "object": f"PodDisruptionBudget/{name}",
                    "excerpt": f"maxUnavailable={max_unavailable!r} minAvailable={min_available!r} "
                    f"against {matched['kind']}/{matched['name']} ({scale}){arithmetic}",
                    # The PDB's own reconciler, not the matched workload's. The
                    # finding is about the PDB's shape and the fix edits the PDB,
                    # so a note naming what holds the Deployment would send the
                    # reader to the wrong chart.
                    "reconciler": reconciler_of(meta),
                    "release": release_of(meta),
                }
            )
    return hits


#: Fewest budgets over one workload that the eviction subresource refuses. One
#: is the norm; two is the outage. Named because the check tests for it and the
#: excerpt reports the count, and the two have to mean the same thing.
PDB_OVERLAP_MINIMUM = 2

_IMPACT_PDB_OVERLAPPING = (
    "The eviction API refuses every pod covered by more than one "
    "PodDisruptionBudget, so node-pool upgrades, node auto-repair and "
    "autoscaler scale-down all stall on this workload until one budget is "
    "removed -- and the budget the disruption controller is keeping current "
    "goes on reporting disruptions allowed while it happens."
)


def check_pdb_overlapping(context: dict) -> list[dict]:
    """Cluster-scoped: a workload whose pods more than one PDB selects.

    A second budget over the same pods is not additive protection, which is
    what it looks like and why nobody removes it. `Eviction` on a pod matched
    by two PDBs returns 500 -- `This pod has more than one PodDisruptionBudget,
    which the eviction subresource does not support` -- so `kubectl drain`,
    node-pool upgrades, auto-repair and scale-down all stop there. It is the
    same outage `blocking-pdb` reports and it hides better: the disruption
    controller picks one of the covering budgets per pod, arbitrarily, and only
    that one is woken by the pod's events, so it goes on reporting a healthy
    `status.disruptionsAllowed` while every eviction fails. The others freeze at
    whatever they last computed. Neither number answers the question, which is
    why the check counts budgets rather than reading either one's status.

    Anchored on workloads rather than on budgets, which decides two things.
    `normalize_workloads` has already applied S1-S5, so the system namespaces
    where GKE's own budgets overlap each other cost nothing and report nothing.
    And the object named is the workload, not either budget: which of the two
    should go is a judgement -- a chart's PDB beside a hand-written one, or one
    broad budget catching a workload it was never meant to cover -- and naming
    a budget would also make the finding id depend on which one sorted first,
    so a run where that order changed would read as one finding resolving and
    another appearing. 3.23 tells the model how to choose and §4 already
    permits a remediation to edit a manifest other than the object it names.

    S2 does *not* exclude a budget from the count. An addon-managed PDB
    overlapping a user's is the most likely way a real fleet reaches this
    state, and the eviction API does not care who wrote either one; suppressing
    the addon's half would hide the finding whose fix -- narrow the user's
    budget -- is the most clearly correct one there is. What S2 does is
    suppress the finding where *every* covering budget is addon-managed, since
    that is a drain block nobody can close, the reason 3.6 records.
    """
    hits = []
    for workload in context["workloads"]:
        covering = []
        for pdb in context["pdbs"].get(workload["ns"], []):
            # `policy/v1` splits the two spellings `selector_matches` collapses:
            # an *empty* selector matches every pod in the namespace, an absent
            # or null one matches none. Reading a missing selector as a match
            # would make every workload in a namespace holding one budget look
            # doubly covered.
            selector = (pdb.get("spec") or {}).get("selector")
            if selector is None or not selector_matches(selector, workload["pod_labels"]):
                continue
            covering.append(pdb)
        if len(covering) < PDB_OVERLAP_MINIMUM:
            continue
        addon = [
            (p.get("metadata") or {}).get("name", "")
            for p in covering
            if "addonmanager.kubernetes.io/mode" in ((p.get("metadata") or {}).get("labels") or {})
        ]
        if len(addon) == len(covering):
            continue
        names = sorted((p.get("metadata") or {}).get("name", "") for p in covering)
        note = f"; {', '.join(sorted(addon))} is addon-managed and not the operator's to edit" if addon else ""
        hits.append(
            {
                "namespace": workload["ns"],
                "object": f"{workload['kind']}/{workload['name']}",
                "excerpt": f"pods labelled {workload['pod_labels']} are selected by "
                f"{len(covering)} PodDisruptionBudgets: {', '.join(names)}{note}",
                "reconciler": workload.get("reconciler"),
                "release": workload.get("release"),
            }
        )
    return hits


def check_no_hpa(workload: dict, context: dict) -> dict | None:
    if workload["kind"] != "Deployment":
        return None
    replicas = workload["spec"].get("replicas", 1) or 1
    if replicas < 3:
        return None
    if _hpa_targeting(workload, context):
        return None
    return {
        "object": f"Deployment/{workload['name']}",
        "excerpt": f"replicas={replicas}, no HorizontalPodAutoscaler targets this Deployment",
    }


def _hpa_able_to_scale(hpa: dict) -> bool:
    """Whether the HPA controller reports it resolved its target.

    `AbleToScale=True` means the controller read the target's scale
    subresource, so a target missing from the dump is the dump's gap, not the
    cluster's. An absent condition -- an HPA too new to have one -- does not
    count as True.
    """
    return any(
        cond.get("type") == "AbleToScale" and cond.get("status") == "True"
        for cond in (hpa.get("status") or {}).get("conditions") or []
    )


def check_hpa_cannot_scale(context: dict) -> list[dict]:
    """Cluster-scoped: two independent flag conditions on the HPA itself,
    (a) `major` — pinned (`minReplicas == maxReplicas`) and (b) `minor` —
    dangling (target absent from the dump). Severity rides on the hit, not
    the check table default, because the two sub-cases disagree.

    Walks a `*_by_namespace` map with no workload to anchor it, so S1 and S2
    have to be applied here rather than inherited. `check_no_hpa` reads
    `context["hpas"]` under a workload's own namespace and gets them for free;
    `check_blocking_pdb` inherits S1 the same way, through the matched workload,
    and applies S2 to the budget's own labels because an addon can mark the PDB
    and not the Deployment. This one inherited neither, and GKE makes that
    expensive: `kube-state-metrics` lives in
    `gke-managed-cim` and `opentelemetry-collector` in `gke-managed-otel`, S1
    keeps their StatefulSet and Deployment out of `workloads`, and their HPAs
    then read as pointing at objects that do not exist. That was 17 `minor`
    findings across a 16-cluster fleet — one per cluster — about resources
    Google owns and the operator cannot edit.

    Dangling resolves against `workload_keys` rather than `workloads` for the
    other half of the same mistake: the target of a *user* HPA can leave the
    audited set by being exempted (S4) or scaled to zero (S5) while plainly
    still existing, and "scaleTargetRef … not found" is a claim about the
    cluster, not about this audit's roster.
    """
    known = context["workload_keys"]
    hits = []
    for ns, hpas in context["hpas"].items():
        if _is_system_namespace(ns):  # S1
            continue
        for hpa in hpas:
            meta = hpa.get("metadata") or {}
            if "addonmanager.kubernetes.io/mode" in (meta.get("labels") or {}):  # S2
                continue
            if _hpa_is_owned(hpa):
                continue
            name = meta.get("name", "")
            reconciler = reconciler_of(meta)
            spec = hpa.get("spec") or {}
            min_r, max_r = spec.get("minReplicas"), spec.get("maxReplicas")
            target = spec.get("scaleTargetRef") or {}
            if min_r is not None and min_r == max_r:
                hits.append(
                    {
                        "namespace": ns,
                        "object": f"HorizontalPodAutoscaler/{name}",
                        "excerpt": f"minReplicas == maxReplicas == {min_r}; autoscaling is cosmetic",
                        "severity": "major",
                        "reconciler": reconciler,
                        "release": release_of(meta),
                    }
                )
                continue
            target_key = (ns, target.get("kind"), target.get("name"))
            if (
                target.get("kind") in ("Deployment", "StatefulSet")
                and target.get("apiVersion") == _WORKLOAD_API_VERSION
                and target_key not in known
                and not _hpa_able_to_scale(hpa)
            ):
                hits.append(
                    {
                        "namespace": ns,
                        "object": f"HorizontalPodAutoscaler/{name}",
                        "excerpt": f"scaleTargetRef {target.get('kind')}/{target.get('name')} not found",
                        "severity": "minor",
                        "reconciler": reconciler,
                        "release": release_of(meta),
                    }
                )
    return hits


#: The floor `hpa-floors-at-one` asks for. Two is the smallest number at which
#: a rollout, a drain, or a preemption leaves something serving, and asking for
#: more would be a capacity opinion this check has no basis for.
HPA_FLOOR_TARGET_REPLICAS = 2


def check_hpa_floors_at_one(context: dict) -> list[dict]:
    """A Service-backed workload whose autoscaler is allowed to reach one pod.

    §3.11 asks whether a Deployment declares one replica. That is the right
    question for a workload whose replica count is a written-down number, and
    the wrong one for a workload whose replica count an HPA rewrites all day:
    `spec.replicas` in the dump is whatever the autoscaler happened to want at
    the moment the audit read it. An audit that fires at 06:50 UTC sees three
    replicas and says nothing; the rollout at 03:00 sees one and drops every
    request. The floor is the durable fact and the instantaneous count is not,
    so this check reads `minReplicas` and ignores `spec.replicas` entirely.

    The floor being one is also the case where the usual objection to §3.11's
    finding does not apply. There the answer is `manual` because going HA
    "touches leader election, session handling, and storage" -- true of a
    workload nobody has ever run two copies of. An HPA with `maxReplicas` above
    one is the owner's own written statement that a controller may run several
    copies concurrently with no human in the loop, so those three questions
    were settled before this check looked. Raising the *floor* asks strictly
    less of the workload than the ceiling it already carries, which is what
    makes this a one-line manifest change rather than guidance.

    The finding names the HPA and not the workload, so §4's declaration rule
    resolves the file the fix actually edits. Editing `spec.replicas` on the
    Deployment instead is the trap: the autoscaler writes that field, and a
    pull request setting it to two merges, reconciles, and is undone on the
    next HPA sync.
    """
    workloads = {(w["ns"], w["kind"], w["name"]): w for w in context["workloads"]}
    hits = []
    for ns, hpas in context["hpas"].items():
        if _is_system_namespace(ns):  # S1
            continue
        for hpa in hpas:
            meta = hpa.get("metadata") or {}
            if "addonmanager.kubernetes.io/mode" in (meta.get("labels") or {}):  # S2
                continue
            if _hpa_is_owned(hpa):
                continue
            spec = hpa.get("spec") or {}
            floor = spec.get("minReplicas")
            floor = DEFAULT_HPA_MIN_REPLICAS if floor is None else floor
            if floor != DEFAULT_HPA_MIN_REPLICAS:
                continue
            # `hpa-cannot-scale` owns a ceiling of one, at `major`, and calls
            # it cosmetic autoscaling. Raising the floor to two there would
            # ask for a floor above the ceiling, which the API rejects.
            if (spec.get("maxReplicas") or 0) < HPA_FLOOR_TARGET_REPLICAS:
                continue
            target = spec.get("scaleTargetRef") or {}
            workload = workloads.get((ns, target.get("kind"), target.get("name")))
            # Absent from the audited set means exempted (S4), scaled to zero
            # (S5), or genuinely gone -- and the last of those is
            # `hpa-cannot-scale`'s dangling arm, not this finding.
            if workload is None or target.get("apiVersion") != _WORKLOAD_API_VERSION:
                continue
            # §3.11's reason, and it bites harder here: `Recreate` says two
            # copies must never run at once, which an HPA above one already
            # contradicts. Reporting a floor on top of that would be the third
            # opinion about a workload whose owner needs to settle the first two.
            if (workload["spec"].get("strategy") or {}).get("type") == "Recreate":
                continue
            services = _selecting_services(workload, context)
            if not services:
                continue
            declared = "" if spec.get("minReplicas") is not None else " (unset, so the API default)"
            hits.append(
                {
                    "namespace": ns,
                    "object": f"HorizontalPodAutoscaler/{meta.get('name', '')}",
                    "excerpt": (
                        f"minReplicas {floor}{declared}, maxReplicas {spec.get('maxReplicas')}, "
                        f"scaling {workload['kind']}/{workload['name']}; at the floor that "
                        f"workload is a single pod, whatever spec.replicas reads now "
                        f"({workload['spec'].get('replicas', DEFAULT_HPA_MIN_REPLICAS)})\n"
                        f"{_exposure_line(services)}"
                    ),
                    "reconciler": reconciler_of(meta),
                    "release": release_of(meta),
                }
            )
    return hits


_HOSTNAME_KEY = "kubernetes.io/hostname"
_ZONE_KEY = "topology.kubernetes.io/zone"


NODE_SELECTOR_OP_IN = "In"


def check_rigid_scheduling(workload: dict, context: dict) -> dict | None:
    node_selector = workload["template"].get("nodeSelector") or {}
    hits = []
    if _HOSTNAME_KEY in node_selector:
        hits.append(("critical", f"nodeSelector pins {_HOSTNAME_KEY}={node_selector[_HOSTNAME_KEY]}"))
    zone = node_selector.get(_ZONE_KEY)
    zonal_storage = workload["kind"] == "StatefulSet" and bool(workload["spec"].get("volumeClaimTemplates"))
    if zone and not zonal_storage:
        hits.append(("major", f"nodeSelector pins {_ZONE_KEY}={zone}"))
    required = (
        (workload["template"].get("affinity") or {}).get("nodeAffinity") or {}
    ).get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    for term in required.get("nodeSelectorTerms") or []:
        for expr in term.get("matchExpressions") or []:
            values = expr.get("values") or []
            if expr.get("operator") != NODE_SELECTOR_OP_IN:
                continue  # `NotIn` a node or zone excludes one; it pins nothing
            if expr.get("key") == _HOSTNAME_KEY and len(values) == 1:
                hits.append(("critical", f"nodeAffinity pins {_HOSTNAME_KEY}={values[0]}"))
            elif expr.get("key") == _ZONE_KEY and len(values) == 1 and not zonal_storage:
                hits.append(("major", f"nodeAffinity pins {_ZONE_KEY}={values[0]}"))
    if not hits:
        return None
    hits.sort(key=lambda h: 0 if h[0] == "critical" else 1)  # report the worse of the two, if both fire
    severity, excerpt = hits[0]
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": excerpt, "severity": severity}


def check_no_spread(workload: dict, context: dict) -> dict | None:
    if workload["kind"] == "DaemonSet":
        return None
    replicas = workload["spec"].get("replicas", 1) or 1
    if replicas < 2:
        return None
    if workload["template"].get("topologySpreadConstraints"):
        return None
    anti_affinity = ((workload["template"].get("affinity") or {}).get("podAntiAffinity")) or {}

    def keyed_on_topology(terms, preferred):
        for entry in terms:
            term = entry.get("podAffinityTerm", entry) if preferred else entry
            if term.get("topologyKey") in (_HOSTNAME_KEY, _ZONE_KEY):
                return True
        return False

    required = anti_affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or []
    preferred = anti_affinity.get("preferredDuringSchedulingIgnoredDuringExecution") or []
    if keyed_on_topology(required, False) or keyed_on_topology(preferred, True):
        return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"replicas={replicas}, no topologySpreadConstraints or podAntiAffinity",
    }


# `whenUnsatisfiable`'s two values. The field is optional and the API server
# defaults it to `DoNotSchedule`, so an absent key is binding and only the
# literal string below is not -- reading a missing key as advisory would flag
# every correctly-written constraint on the fleet.
_SPREAD_ADVISORY = "ScheduleAnyway"
_SPREAD_BINDING = "DoNotSchedule"

# Below two endpoints there is no co-location to observe: one replica cannot
# be spread, and the finding is about replicas that are together rather than
# about a workload that has only one.
_MIN_ENDPOINTS_TO_COLOCATE = 2

_IMPACT_SPREAD_NOT_ACHIEVED = (
    "Every ready replica of this workload is on one node right now, and the "
    "manifest contains nothing that would have stopped that: the spreading it "
    "declares is a preference the scheduler is free to ignore, and it did. "
    "Losing that one node -- a pool upgrade draining it, a preemption, a "
    "hardware fault -- takes the whole workload out at once, and the replica "
    "count in the manifest reads as though it could not."
)


def _endpoint_nodes_for(workload: dict, context: dict) -> tuple[set[str], int]:
    """Nodes hosting this workload's ready endpoints, and how many were counted.

    Read off EndpointSlices rather than Pods because Pods are not in this
    audit's dump — they are the largest kind in any cluster and the dump is
    written to disk. The slice carries `nodeName` on each endpoint, which is
    the scheduler's own answer to where the pod landed.

    Only Services this workload alone backs are consulted. A Service fronting
    two workloads mixes their endpoints, and "all endpoints on one node" would
    then be a claim about the pair rather than about the object the finding
    names — so a Service another audited workload also matches is skipped, and
    a workload behind only such Services produces no evidence and no finding.
    """
    key = f"{workload['kind']}/{workload['name']}"
    # One Service's slices, not the union of several. A workload behind both a
    # headless and a ClusterIP Service appears in both, and adding them would
    # report twice as many endpoints as it has replicas. The widest single view
    # is the honest one.
    widest: list[str] = []
    for entry in context["service_endpoints"]:
        if entry["ns"] != workload["ns"]:
            continue
        selector = {"matchLabels": entry["selector"]}
        if not selector_matches(selector, workload["pod_labels"]):
            continue
        shared = any(
            f"{other['kind']}/{other['name']}" != key
            and other["ns"] == workload["ns"]
            and selector_matches(selector, other["pod_labels"])
            for other in context["workloads"]
        )
        if shared:
            continue
        found = entry.get("endpoint_nodes") or []
        if len(found) > len(widest):
            widest = found
    return set(widest), len(widest)


def check_spread_not_achieved(workload: dict, context: dict) -> dict | None:
    """§3.19 — advisory spreading that demonstrably did not spread.

    §3.8 asks whether a workload declares spreading at all and stops there, so a
    `topologySpreadConstraints` block satisfies it whatever the block says —
    and `whenUnsatisfiable: ScheduleAnyway` is scored rather than enforced.

    The obvious check on that hole, "flag every workload whose spreading is
    advisory", is one this audit must not ship: §3.8's own remediation
    prescribes `ScheduleAnyway`, and deliberately so, because `DoNotSchedule`
    on `kubernetes.io/hostname` can leave a pod Pending forever. A check
    written that way would re-flag every workload §3.8 had just fixed, which is
    a tool arguing with itself in a customer's ledger.

    So this fires on the observation instead: the spreading is advisory **and**
    every ready endpoint is on one node. That is not a prediction about what
    the scheduler might do, it is what it did, and it cannot be raised against
    a workload whose advisory constraint is in fact holding the replicas apart.

    Suppressed where the cluster's own EndpointSlices never name a second node,
    which is the only evidence in this dump that more than one node exists —
    co-location is forced there and the manifest is not the defect. That test
    is deliberately weak: it establishes that a second node exists, not that
    this workload could have been placed on it. The finding claims only what it
    can see, that the replicas are together and nothing asked otherwise.
    """
    if workload["kind"] == "DaemonSet":
        return None
    replicas = workload["spec"].get("replicas", 1) or 1
    if replicas < _MIN_ENDPOINTS_TO_COLOCATE:
        return None
    constraints = workload["template"].get("topologySpreadConstraints") or []
    anti_affinity = ((workload["template"].get("affinity") or {}).get("podAntiAffinity")) or {}
    required = anti_affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or []
    preferred = anti_affinity.get("preferredDuringSchedulingIgnoredDuringExecution") or []

    def keyed_on_topology(terms, is_preferred):
        for entry in terms:
            term = entry.get("podAffinityTerm", entry) if is_preferred else entry
            if term.get("topologyKey") in (_HOSTNAME_KEY, _ZONE_KEY):
                return True
        return False

    # A binding rule of either kind ends it. `required` podAntiAffinity is
    # enforced by definition; a constraint is enforced unless it opts out.
    if keyed_on_topology(required, False):
        return None
    if any(c.get("whenUnsatisfiable", _SPREAD_BINDING) != _SPREAD_ADVISORY for c in constraints):
        return None

    advisory = [f"topologySpreadConstraint on {c.get('topologyKey', '')} is {_SPREAD_ADVISORY}" for c in constraints]
    if keyed_on_topology(preferred, True):
        advisory.append("podAntiAffinity is preferredDuringScheduling")
    # Nothing declared at all is §3.8's finding, not this one, and reporting
    # both would put two verdicts on one object in one ledger.
    if not advisory:
        return None

    # A cluster whose slices only ever name one node has one node, and a
    # workload cannot be faulted for landing on it.
    if len(context.get("endpoint_nodes_seen") or set()) < _MIN_ENDPOINTS_TO_COLOCATE:
        return None

    nodes, counted = _endpoint_nodes_for(workload, context)
    if counted < _MIN_ENDPOINTS_TO_COLOCATE or len(nodes) != 1:
        return None
    # The shape of the topology, appended because the remediation turns on it
    # and the model cannot read it from anywhere else in this document. Three
    # edits close this finding and they are not interchangeable: raising a
    # zone-keyed constraint on a single-zone cluster is a no-op, and a
    # hostname-keyed `DoNotSchedule` pends a replica where the cluster has
    # fewer nodes than the workload has replicas. Both branches are decidable
    # from these two counts, so the SOP decides them rather than the model
    # guessing -- which is what produced a `manual` on 2026-09-07, correctly
    # hesitant and still a pull request nobody got.
    cluster_nodes = len(context.get("endpoint_nodes_seen") or set())
    cluster_zones = len(context.get("endpoint_zones_seen") or set())
    topology = f"this cluster's EndpointSlices name {cluster_nodes} nodes"
    if cluster_zones:
        topology += f" across {cluster_zones} zone{'' if cluster_zones == 1 else 's'}"
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": (
            f"replicas={replicas}, all {counted} ready endpoints on node {sorted(nodes)[0]}; "
            f"every spreading rule is advisory: {'; '.join(advisory)}; {topology}"
        ),
    }


_SELF_HEALTH_SIDECARS = {"istio-proxy", "cloud-sql-proxy", "gke-metadata-server"}


def _containers_behind_a_service(workload: dict, services: list[dict]) -> list[dict]:
    """The containers a Service actually routes traffic to.

    A container with no readiness probe counts as ready the moment it starts,
    so a probe-less log shipper cannot hold traffic off a pod. Only the
    container serving the Service's `targetPort` can, which makes it the only
    one whose missing probe is the risk this check names.

    Resolving that container means reading the native sidecars too --
    `initContainers` with `restartPolicy: Always` serve ports like any other
    container. The gateway's own Service targets 8643, which belongs to the
    `envoy-credential-proxy` sidecar and nothing under `containers`; a check
    that reads only `containers` reported the one container holding the
    Service port as having no probe when it has one.

    Declaring `ports` is optional -- kubelet routes to a port a container never
    named -- so when nothing in the pod declares a matching one there is no
    routing to infer, and every container stays in the path as before.
    """
    targets = set()
    for svc in services:
        for port in (svc.get("spec") or {}).get("ports") or []:
            targets.add(port.get("targetPort", port.get("port")))
    targets.discard(None)
    containers = _effective_containers(workload)
    behind = [
        c
        for c in containers
        if any(p.get("containerPort") in targets or p.get("name") in targets for p in c.get("ports") or [])
    ]
    return behind or containers


#: Port names the ecosystem reserves for a Prometheus scrape endpoint. The name
#: is the discriminator and the number is not: `ServiceMonitor` and
#: `PodMonitor` both select on `port` by name, so this is the convention a
#: chart author is already following, while 9402 and 8080 mean nothing on their
#: own.
_METRICS_PORT_NAMES = frozenset({"metrics", "http-metrics", "https-metrics", "telemetry"})

#: The three things "Service-backed" can mean, as the trailing `(scope)` of an
#: exposure line. §3.9 and §3.11 of the SOP key their impact claims off these
#: strings verbatim, so they are named rather than spelled inline.
_SCOPE_SERVING = "serving traffic"
_SCOPE_METRICS_ONLY = "metrics scrape only"
_SCOPE_NO_PORTS = "no ports declared"


def _metrics_only_ports(services: list[dict]) -> bool:
    """Every port every selecting Service exposes is a metrics scrape port.

    A workload whose only Service is a scrape endpoint is "Service-backed" in
    the sense 3.9 flags on, and in no other sense: nothing routes a user
    request to it. Prometheus retries a failed scrape, so a probe-less pod
    joining that Service early costs a gap in a graph, not a dropped request.

    Named ports only, and conservatively: an unnamed port is not treated as
    metrics, so a single-port Service that omits the name keeps the finding
    exactly as it read before. That is the safe direction -- the line this
    feeds suppresses an impact claim, and suppressing it wrongly is the
    expensive error.
    """
    ports = [port for svc in services for port in (svc.get("spec") or {}).get("ports") or []]
    return bool(ports) and all(port.get("name") in _METRICS_PORT_NAMES for port in ports)


def _routing_services(services: list[dict]) -> list[dict]:
    """The selecting Services that carry requests, for picking the containers behind them.

    A serving Service and a metrics Service commonly share one selector, and the
    union of their target ports names the exporter sidecar as though it were in
    the request path -- so a probe-less or hook-less exporter was published as
    dropping production traffic. Each metrics-only Service is set aside here;
    when every Service is one, they are all kept, because then the scrape port
    is the only routing there is and the exposure line already says so.
    """
    serving = [svc for svc in services if not _metrics_only_ports([svc])]
    return serving or services


def _exposure_scope(services: list[dict]) -> str:
    """Which of the three things "Service-backed" means for this workload.

    `_metrics_only_ports` answers one question and cannot answer this one: it
    is false both for a Service carrying user requests and for a Service that
    declares no ports at all, and those are not the same claim. A port-less
    Service routes nothing through its own ClusterIP; if it is headless its DNS
    records still hand out pod IPs, so a client that already knows a port can
    reach the pods anyway. Neither of the other two scopes is true of it, so it
    gets its own rather than being rounded to whichever is nearer -- rounding
    it to `serving traffic` is what made a Service with no ports publish "every
    rollout sends production traffic to pods that are not yet serving".
    """
    ports = [port for svc in services for port in (svc.get("spec") or {}).get("ports") or []]
    if not ports:
        return _SCOPE_NO_PORTS
    return _SCOPE_METRICS_ONLY if _metrics_only_ports(services) else _SCOPE_SERVING


def _exposure_line(services: list[dict]) -> str:
    """`selecting services: name[port,port] (scope)` — what "Service-backed" means here.

    Both checks that gate on a selecting Service publish an Impact line about
    production traffic, and neither can tell whether there is any. On
    2026-09-01 the live fleet had `cert-manager` and `cert-manager-cainjector`
    each selected by one Service whose only port is `http-metrics/9402`, and
    `argocd-notifications-controller` by one whose only port is `metrics/9001`;
    nothing distinguishes them from `argocd-redis` (`tcp-redis/6379`) or
    `kube-agents-controller-manager` (an unnamed 443 to a webhook) except the
    ports, so name the ports and say which case it is.
    """
    exposure = "; ".join(
        "{}[{}]".format(
            (svc.get("metadata") or {}).get("name", "?"),
            ",".join(str(port.get("name") or port.get("port")) for port in (svc.get("spec") or {}).get("ports") or []) or "no ports",
        )
        for svc in services
    )
    return f"selecting services: {exposure} ({_exposure_scope(services)})"


def check_probes_readiness(workload: dict, context: dict) -> dict | None:
    services = _selecting_services(workload, context)
    if not services:
        return None
    behind = [
        c
        for c in _containers_behind_a_service(workload, _routing_services(services))
        if c.get("name") not in _SELF_HEALTH_SIDECARS and not c.get("readinessProbe")
    ]
    if not behind:
        return None
    # All the containers or none of them. A patch that gives readiness probes
    # to two of three leaves this finding open on the third, and a finding its
    # own remediation does not close is one the audit republishes every day.
    copyable = [_readiness_from_liveness(container) for container in behind]
    derivable = (
        ""
        if any(line is None for line in copyable)
        else "\nreadiness derivable from the liveness probe: " + "; ".join(copyable)
    )
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": (
            "Service-backed, containers missing a readiness probe: "
            f"{', '.join(c.get('name', '') for c in behind)}\n"
            f"{_exposure_line(services)}{derivable}"
        ),
    }


def check_probes_liveness(workload: dict, context: dict) -> dict | None:
    missing = [
        c.get("name", "")
        for c in workload["template"].get("containers") or []
        if c.get("name") not in _SELF_HEALTH_SIDECARS and not c.get("livenessProbe")
    ]
    if not missing:
        return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"containers missing a liveness probe: {', '.join(missing)}",
    }


# The four probe handlers a container can declare. Order is irrelevant --
# `v1.Probe` is a union and admission rejects a second one -- but the tuple has
# to be exhaustive, because a handler this does not know about reads as "no
# handler" and silently exempts the container from §3.18.
_PROBE_HANDLERS = ("httpGet", "tcpSocket", "exec", "grpc")

# `v1.Probe` defaults, from the Kubernetes API reference. Only the three that
# decide *when* a probe gives up are here; `timeoutSeconds` and
# `successThreshold` do not move the deadline §3.18 compares.
_PROBE_DEFAULT_PERIOD_SECONDS = 10
_PROBE_DEFAULT_FAILURE_THRESHOLD = 3
_PROBE_DEFAULT_INITIAL_DELAY_SECONDS = 0

_IMPACT_LIVENESS_PREEMPTS = (
    "Both probes test the same endpoint, and the one that kills the container "
    "gives up first. Whatever the readiness probe was meant to protect against "
    "-- a slow dependency, a cold cache, a downstream outage -- restarts every "
    "replica at once instead of quietly taking them out of the Service, and "
    "the restarts keep the dependency from recovering. The readiness probe "
    "cannot fire first, so it never gets to do its job."
)


def _probe_handler(probe: dict | None) -> tuple[str, str] | None:
    """`(kind, canonical body)` of a probe's handler, or None if it has none.

    Canonicalised with sorted keys so two probes that differ only in field
    order compare equal -- the apiserver does not reorder, but a manifest
    written by two hands can, and the check is about what the probe *tests*.
    """
    for kind in _PROBE_HANDLERS:
        if isinstance(probe, dict) and probe.get(kind) is not None:
            return kind, json.dumps(probe[kind], sort_keys=True)
    return None


def _probe_deadline_seconds(probe: dict) -> int:
    """Seconds from container start until this probe's failure takes effect.

    `initialDelaySeconds + failureThreshold x periodSeconds`. Approximate by
    construction -- the kubelet's first check lands somewhere inside the first
    period, and a probe that times out costs `timeoutSeconds` on top -- but
    both probes are approximated the same way, and §3.18 only ever compares
    the two against each other.
    """
    return probe.get("initialDelaySeconds", _PROBE_DEFAULT_INITIAL_DELAY_SECONDS) + (
        probe.get("failureThreshold", _PROBE_DEFAULT_FAILURE_THRESHOLD)
        * probe.get("periodSeconds", _PROBE_DEFAULT_PERIOD_SECONDS)
    )


#: Timings for a readiness probe copied from a liveness probe. The handler
#: comes from the container; only the deadline is chosen here, and it has to
#: land strictly inside the liveness one -- a readiness probe that gives up at
#: the same moment is §3.18, planted by the fix that closed §3.9.
_READINESS_FROM_LIVENESS_INITIAL_DELAY_SECONDS = 0
_READINESS_FROM_LIVENESS_PERIOD_SECONDS = 5
#: Half of the liveness budget, so the pod leaves the Service well before the
#: kubelet restarts it rather than a second before.
_READINESS_FROM_LIVENESS_BUDGET_DIVISOR = 2
#: The period to fall back to where a liveness deadline is too tight to halve
#: in 5-second steps.
_READINESS_FROM_LIVENESS_TIGHT_PERIOD_SECONDS = 1


def _readiness_from_liveness(container: dict) -> str | None:
    """A readiness probe this container's own liveness probe already licenses.

    §3.9's remediation is `manual` because a probe's path, port and timings are
    application knowledge. That holds for a container declaring no probe at
    all, and not for one declaring a liveness probe: the kubelet is already
    calling that handler and the application is already answering it, or the
    container would be restarting. Copying it into a readiness probe asks
    nothing new of the workload, and turns the largest sub-case of a `manual`
    finding into one a pull request can close.

    Only the deadline is invented, and deliberately tighter than the liveness
    deadline it comes from, so the pod leaves the Service before the kubelet
    restarts it. Equal deadlines are the §3.18 antipattern -- a fix that
    produced them would close this finding by opening that one on the same
    container.

    Returns the sentence the excerpt carries, or None where there is no
    liveness handler to copy, or its deadline leaves no room beneath it.
    """
    liveness = container.get("livenessProbe")
    handler = _probe_handler(liveness)
    if handler is None:
        return None
    live_at = _probe_deadline_seconds(liveness)
    period = _READINESS_FROM_LIVENESS_PERIOD_SECONDS
    threshold = max(1, live_at // (period * _READINESS_FROM_LIVENESS_BUDGET_DIVISOR))
    if period * threshold >= live_at:
        period, threshold = _READINESS_FROM_LIVENESS_TIGHT_PERIOD_SECONDS, 1
        if period >= live_at:
            return None
    ready_at = _READINESS_FROM_LIVENESS_INITIAL_DELAY_SECONDS + period * threshold
    return (
        f"{container.get('name', '')}: copy the liveness handler {handler[0]} {handler[1]}, "
        f"with initialDelaySeconds {_READINESS_FROM_LIVENESS_INITIAL_DELAY_SECONDS}, "
        f"periodSeconds {period}, failureThreshold {threshold} -- readiness then fails "
        f"at ~{ready_at}s, inside liveness's ~{live_at}s"
    )


def check_liveness_preempts_readiness(workload: dict, context: dict) -> dict | None:
    """§3.18 -- a liveness probe that fires no later than its readiness twin.

    The pair only matters when both probes test *the same thing*: then the
    cluster has two responses to one signal, one of which (stop routing) is
    recoverable and one of which (restart) is not. Kubernetes runs them
    independently, so which response happens is decided by whichever deadline
    comes first -- and when liveness's is the shorter or equal one, the
    readiness probe is decoration. It can never remove the pod from a Service
    before the container is killed out from under it.

    That is the whole rule, and it is deliberately narrower than the
    antipattern it belongs to. A shared endpoint with real headroom on the
    liveness side is the *documented* way to use one health handler for both,
    so a fleet's worth of `livenessProbe`/`readinessProbe` pairs pointing at
    one `/healthz` are not findings; on this fleet the rule cut nine matching
    pairs to four. What is left is provable from the manifest without knowing
    anything about what the endpoint does, which is what makes it safe to open
    a pull request against.

    Deliberately not flagged: a liveness probe stricter than a readiness probe
    on a *different* handler. It fails the same way, but the two probes may
    genuinely test different things -- a local liveness endpoint and a
    dependency-aware readiness one is the correct shape -- and the collector
    cannot tell that from a shape that is wrong.
    """
    bad = []
    for container in workload["template"].get("containers") or []:
        if container.get("name") in _SELF_HEALTH_SIDECARS:
            continue
        liveness, readiness = container.get("livenessProbe"), container.get("readinessProbe")
        handler = _probe_handler(liveness)
        if handler is None or handler != _probe_handler(readiness):
            continue
        live_at, ready_at = _probe_deadline_seconds(liveness), _probe_deadline_seconds(readiness)
        if live_at > ready_at:
            continue
        # Name both deadlines and the handler they share. The remediation is a
        # choice between widening liveness, repointing it, and deleting it,
        # and a reader cannot make that choice without seeing how much room
        # there is between the two numbers -- here, none.
        bad.append(
            f"{container.get('name', '')}: {handler[0]} {handler[1]} is both probes; "
            f"liveness fails at ~{live_at}s, readiness at ~{ready_at}s"
        )
    if not bad:
        return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": "; ".join(bad),
    }


def check_single_replica(workload: dict, context: dict) -> dict | None:
    if workload["kind"] != "Deployment":
        return None
    if (workload["spec"].get("replicas", 1) or 1) != 1:
        return None
    if (workload["spec"].get("strategy") or {}).get("type") == "Recreate":
        return None
    # An HPA wrote this `1` and will write the next number too, so both halves
    # of this finding are wrong on such a workload: the count is a snapshot
    # rather than a declaration, and the remediation this check carries --
    # guidance about leader election, storage, and session handling -- answers
    # a question the owner settled when they let a controller add replicas
    # unattended. `hpa-floors-at-one` reports it off the floor instead, and
    # points the fix at the field the autoscaler does not overwrite.
    if _hpa_targeting(workload, context):
        return None
    services = _selecting_services(workload, context)
    if not services:
        return None
    # Same claim, same qualifier as `check_probes_readiness`. Giving one check
    # the exposure line and not the other left the 2026-09-01 run publishing
    # `cert-manager` and `cert-manager-cainjector` as "single replica,
    # Service-backed" -- a rollout-drops-user-traffic claim -- one section
    # below the readiness finding on those same two Deployments that had just
    # said the only Service in front of them is a scrape endpoint.
    excerpt = f"single replica, Service-backed\n{_exposure_line(services)}"
    # The one fact that bars the manifest arm, and it has to ride in the
    # excerpt: `emit` builds the published candidate from a fixed key set, so a
    # `manifest_blocked` flag on the hit would be dropped silently, while
    # `adopt_collector_evidence` forces this string onto the finding verbatim.
    # A second replica cannot schedule off the first one's node here -- it sits
    # in `ContainerCreating` with a `Multi-Attach error` until something gives,
    # which is 3.21's finding and 3.21's fix. The finding itself still stands:
    # a drain is a full outage whatever the volume does.
    claims = _exclusive_claims(workload, context)
    if claims:
        excerpt += "\nmounts an exclusively-attachable claim: " + "; ".join(claims)
    return {
        "object": f"Deployment/{workload['name']}",
        "excerpt": excerpt,
    }


# Kubernetes' own default when a pod spec omits `terminationGracePeriodSeconds`.
# Named because the excerpt has to print a number either way, and printing the
# absent case as `0` or `None` would describe a pod that is killed instantly.
DEFAULT_GRACE_PERIOD_S = 30


def check_rollout_drops_traffic(workload: dict, context: dict) -> dict | None:
    """A Service-backed workload whose containers have no `preStop` hook.

    Endpoint removal and `SIGTERM` are two independent, concurrent reactions to
    a pod being deleted: the kubelet starts terminating the container while the
    endpoints controller is still propagating the removal out to every node's
    kube-proxy and to every Ingress or mesh sidecar holding a connection. The
    container almost always wins that race, so requests keep arriving at a
    process that has already begun shutting down and are answered with a
    connection reset. A `preStop` hook is the only thing in the pod lifecycle
    that delays `SIGTERM`, which is why the fix is a hook that sleeps rather
    than anything in the application.

    That makes this the one rollout defect nobody sees in a rolling update: the
    Deployment goes green, the new pods pass their probes, and the only trace
    is a burst of 502s in whatever sits in front. It costs errors on every
    deploy, every node drain, every autoscaler scale-down, and every
    preemption -- which on Spot capacity is continuous.

    Narrowed three ways, because unnarrowed this fires on essentially every
    workload in a fleet and a check that flags everything has told you nothing.
    `_SCOPE_SERVING` drops the workloads whose only Service is a metrics scrape
    endpoint -- Prometheus retries, so a reset there costs a gap in a graph
    rather than a user's request -- and the same qualifier is what
    `check_probes_readiness` and `check_single_replica` already use to avoid
    the same false claim. More than one replica, because a single-replica
    workload has a hard outage on every rollout whatever its hooks do, and
    `check_single_replica` is the finding that says so; adding a `preStop`
    sleep there delays the outage rather than removing it. And only the
    containers actually behind the Service's `targetPort`, for the reason
    `_containers_behind_a_service` gives: a log shipper with no hook is not in
    the request path and cannot reset anything.

    DaemonSets are in scope with no replica test. One pod per node is not the
    single-replica case -- the other nodes keep serving -- so a rolling update
    of a Service-backed DaemonSet drops exactly the traffic this describes.
    """
    if workload["kind"] == "Deployment" or workload["kind"] == "StatefulSet":
        if (workload["spec"].get("replicas", 1) or 1) <= 1:
            return None
    services = _selecting_services(workload, context)
    if not services or _exposure_scope(services) != _SCOPE_SERVING:
        return None
    missing = [
        c.get("name", "")
        for c in _containers_behind_a_service(workload, _routing_services(services))
        if c.get("name") not in _SELF_HEALTH_SIDECARS and not (c.get("lifecycle") or {}).get("preStop")
    ]
    if not missing:
        return None
    grace = workload["template"].get("terminationGracePeriodSeconds")
    if grace is None:
        grace = DEFAULT_GRACE_PERIOD_S
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": (
            f"Service-backed, containers with no preStop hook: {', '.join(missing)}\n"
            f"terminationGracePeriodSeconds={grace}\n"
            f"{_exposure_line(services)}"
        ),
    }


# A bare integer `sleep` in a shell `preStop`. Only whole seconds are read:
# `sleep 0.5` and `sleep 1m` are both accepted by GNU coreutils and only one of
# them by busybox, so a duration parsed out of either would be a guess about
# which shell the image ships. An unparsed hook produces no finding, which is
# the direction that costs a report rather than inventing one.
#
# The trailing lookahead is what rejects them, and `\b` will not do it: in
# `sleep 0.5` there is a word boundary between the `0` and the `.`, so `\b`
# reads a fractional wait as a whole `0` -- which then reaches any grace period
# and reports a hook that sleeps for half a second as truncated.
_PRESTOP_SLEEP_RE = re.compile(r"\bsleep\s+(\d+)(?![\w.])")

_IMPACT_PRESTOP_TRUNCATED = (
    "This container declares a preStop hook that cannot finish. The grace "
    "period is the ceiling the hook runs inside, and it expires while the hook "
    "is still sleeping, so the kubelet sends SIGKILL: the wait never completes "
    "and the process is never given the SIGTERM it was supposed to delay. "
    "Every rollout, node drain, and preemption therefore resets the connections "
    "in flight, which is the failure the hook was added to prevent -- and 3.13 "
    "is silent on this workload because a preStop hook does exist."
)


def _prestop_wait_seconds(container: dict) -> tuple[int | None, str]:
    """The seconds a container's `preStop` hook waits, and how it spells it.

    Two spellings are readable. `sleep.seconds` is the native handler the
    kubelet runs itself, and its duration is the field. An `exec` is a shell
    command, so the duration has to be read out of the text; the largest
    integer `sleep` in it is taken, because a hook that sleeps that long
    cannot finish in less time whatever else the command does.

    `httpGet` and `tcpSocket` handlers return `None`. Their duration is a
    property of whatever answers the request, which is not in this dump.
    """
    hook = (container.get("lifecycle") or {}).get("preStop") or {}
    native = hook.get("sleep") or {}
    if isinstance(native.get("seconds"), int):
        return native["seconds"], "lifecycle.preStop.sleep.seconds"
    command = " ".join((hook.get("exec") or {}).get("command") or [])
    found = [int(m) for m in _PRESTOP_SLEEP_RE.findall(command)]
    if found:
        return max(found), f"lifecycle.preStop.exec.command ({command})"
    return None, ""


def check_prestop_outlives_grace(workload: dict, context: dict) -> dict | None:
    """3.20 -- a `preStop` hook the grace period cuts short.

    3.13 stops firing the moment a `preStop` exists, whatever it says: its own
    "do NOT flag" list ends with "any container that already declares a
    `preStop`, whatever it runs -- this check does not grade the hook's
    contents". That is the right scope for 3.13 and it leaves one shape
    unreported, the shape where the hook is present and does nothing.

    `terminationGracePeriodSeconds` is the ceiling the whole termination runs
    inside, hook included. When the hook's wait reaches that ceiling the
    kubelet sends SIGKILL with the hook still sleeping, so the delay never
    happens and the container never even receives the SIGTERM the delay was
    protecting. The workload is in exactly the state 3.13 describes while
    carrying the fix for it in its manifest -- and 3.13 is quiet, so the ledger
    says nothing at all.

    This is 3.13's own remediation getting written wrong, which is why it is
    worth a check rather than a sentence. That remediation already says to
    raise the grace period "above that wait if it is not already"; a check is
    what notices when someone did not, whether that someone was a previous run
    of this audit or the hundred blog posts that print `sleep 30` next to a
    grace period nobody mentioned.

    Only the two readable spellings of a wait are considered, and only whole
    seconds -- see `_prestop_wait_seconds`. Reaching the ceiling is the
    predicate, not merely approaching it: at `wait == grace` the hook is
    provably truncated, while "the hook leaves too little room for the
    application to shut down" needs a number for how much room an application
    needs, which this audit cannot know.

    `>=` rather than `>` is also what keeps the native handler in scope at all.
    The apiserver validates `lifecycle.preStop.sleep.seconds` against the grace
    period and refuses anything larger -- measured on GKE 1.35, `seconds: 31`
    under a 30s ceiling is rejected outright and `seconds: 30` is accepted --
    so `wait == grace` is the only truncated native shape that can exist on a
    cluster. Tightening this to `>` would silently retire the native arm
    rather than narrow it. Nothing validates the shell spelling, which is why
    the waits that run far past the ceiling are all `exec` hooks.
    """
    grace = workload["template"].get("terminationGracePeriodSeconds")
    if grace is None:
        grace = DEFAULT_GRACE_PERIOD_S
    truncated = []
    for container in _effective_containers(workload):
        wait, spelling = _prestop_wait_seconds(container)
        if wait is None or wait < grace:
            continue
        truncated.append(f"{container.get('name', '')} waits {wait}s via {spelling}")
    if not truncated:
        return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": (
            f"terminationGracePeriodSeconds={grace}, and the hook reaches it:\n" + "\n".join(truncated)
        ),
    }


# The access modes under which a volume can be held by one node at a time --
# or, for `ReadWriteOncePod`, by one pod. `ReadWriteMany` and `ReadOnlyMany`
# are the two that let a second pod attach, so a claim carrying either is out
# of §3.21's scope however the workload rolls. GKE's default StorageClass
# (`pd.csi.storage.gke.io`) provisions `ReadWriteOnce`, which is why this is
# the common case rather than an exotic one.
_EXCLUSIVE_ACCESS_MODES = frozenset({"ReadWriteOnce", "ReadWriteOncePod"})

# Kubernetes' own `maxSurge` when a RollingUpdate strategy omits it. Named
# because the default *is* the finding: a Deployment that never mentions a
# strategy still surges, and 25% of one replica rounds up to one extra pod.
DEFAULT_MAX_SURGE = "25%"

_IMPACT_RWO_ROLLOUT_DEADLOCK = (
    "Whether this workload can be updated is decided by the scheduler rather "
    "than by anything in its manifest. The default strategy starts a "
    "replacement pod before the old one is gone, and both claim the same "
    "ReadWriteOnce volume -- which is attachable by one node at a time, not by "
    "one pod. Placed beside the original the replacement starts normally and "
    "the rollout completes. Placed on any other node it sits in "
    "ContainerCreating with a Multi-Attach error until the progress deadline "
    "expires, and that is the outcome whenever the volume's node cannot take a "
    "second copy: it is full, it is cordoned for an upgrade, or an affinity "
    "rule sends the pod elsewhere. A workload given a volume of its own is "
    "usually sized so that a second copy does not fit beside it. **When it "
    "does deadlock, the failure is silent.** maxUnavailable will not let the "
    "old pod leave until the replacement is ready and the replacement never "
    "will be, so nothing goes down and no alert fires: the Deployment keeps "
    "serving the image it was already running, and every later change, "
    "security patch included, is accepted by the API server and reaches no "
    "container."
)

_IMPACT_RWO_REPLICAS_PINNED = (
    "This workload reaches the replica count it declares only for as long as "
    "every one of its pods happens to be on the same node. They all claim one "
    "ReadWriteOnce volume, which is attachable by one node at a time, and the "
    "scheduler spreads the pods of a ReplicaSet by default -- so any pod "
    "placed away from the volume stays in ContainerCreating with a "
    "Multi-Attach error. The redundancy the manifest asks for is the thing "
    "this costs: pods that did co-locate share the fate of that one node, and "
    "pods that did not never start. Either way the Deployment's replica field, "
    "and every dashboard reading it, reports capacity that is not there."
)


def _max_surge_count(strategy: dict, replicas: int) -> int:
    """`maxSurge` resolved against a replica count.

    The mirror of `_max_unavailable_count`, rounding the other way. Kubernetes
    rounds a `maxSurge` percentage **up**, so that a proportion can never
    resolve to nought and stall a rollout that `maxUnavailable: 0` has already
    forbidden to remove a pod first. That direction is the whole reason §3.21
    fires on ordinary single-replica Deployments: 25% of one replica is one
    extra pod, not none.

    Returns an `int` where `_max_unavailable_count` returns `None`, because
    unset means something different for the two fields. `maxUnavailable`
    absent is a genuinely unknown state this collector will not invent a
    number for; `maxSurge` absent is `"25%"` written by omission, and reading
    it as nought would clear every Deployment that does not mention a strategy
    -- which is nearly all of them, and all of the ones this check is for.
    """
    raw = strategy.get("maxSurge")
    if raw is None:
        raw = DEFAULT_MAX_SURGE
    text = str(raw).strip()
    if not text.endswith("%"):
        try:
            return int(text)
        except ValueError:
            text = DEFAULT_MAX_SURGE
    try:
        percent = float(text[:-1])
    except ValueError:
        percent = float(DEFAULT_MAX_SURGE[:-1])
    return math.ceil(replicas * percent / 100)


def _exclusive_claims(workload: dict, context: dict) -> list[str]:
    """The workload's PVC-backed volumes that only one node can hold.

    Reads `volumes[].persistentVolumeClaim.claimName` and nothing else, which
    excludes generic ephemeral volumes for free: those declare a
    `volumeClaimTemplate` inline and the controller creates one claim per pod,
    so two pods never contend for the same object and there is no finding.

    A claim the dump does not contain is skipped rather than assumed. It means
    the object was created after the dump or lives outside it, and guessing
    `ReadWriteOnce` because that is the common default would send a pull
    request to change a rollout strategy over a volume that may well be
    `ReadWriteMany`.
    """
    described = []
    for volume in workload["template"].get("volumes") or []:
        name = ((volume.get("persistentVolumeClaim") or {}).get("claimName") or "").strip()
        if not name:
            continue
        claim = context.get("claims", {}).get((workload["ns"], name))
        if claim is None:
            continue
        spec = claim.get("spec") or {}
        # `status.accessModes` is what the bound volume actually supports and
        # `spec.accessModes` is only what was asked for, so the status wins
        # where it exists. An unbound claim has no status yet, and there the
        # request is the best evidence available.
        modes = (claim.get("status") or {}).get("accessModes") or spec.get("accessModes") or []
        # Every mode must be exclusive, not merely one of them. A claim listing
        # `ReadWriteMany` alongside `ReadWriteOnce` can be attached to a second
        # node, so nothing here deadlocks and there is no finding to make.
        if not modes or any(mode not in _EXCLUSIVE_ACCESS_MODES for mode in modes):
            continue
        storage_class = spec.get("storageClassName") or "(cluster default)"
        described.append(f"{name} [{', '.join(modes)}, storageClass {storage_class}]")
    return described


def check_rwo_claim_contended(workload: dict, context: dict) -> dict | None:
    """3.21 -- two pods sent to one volume only one node can hold.

    A `ReadWriteOnce` claim is attached by one **node** at a time -- not by one
    pod, which is what `ReadWriteOncePod` means and why the two modes both
    exist. Put two pods that can coexist behind one claim and the outcome is
    the scheduler's to decide: co-located they both mount it and nothing is
    wrong, separated the second sits in ContainerCreating with a
    `Multi-Attach error for volume` until something gives.

    That conditionality is the reason this is worth reporting rather than a
    reason not to. Nothing in the manifest decides which way it goes, so the
    workload's ability to be updated depends on free capacity on one
    particular node -- and the moments that capacity disappears are exactly
    the moments the rollout matters: the node filled up, or it was cordoned
    for an upgrade. A workload given a volume of its own is also usually sized
    so a second copy does not fit beside it in the first place. Measured on a
    GKE Autopilot cluster, a single-replica Deployment small enough to double
    up on its node rolled cleanly twice in a row; the finding on it is latent,
    and the fix is what keeps it latent.

    Two shapes reach the contended state and this check reports both, because
    they are the same cause and take different fixes.

    The first is a rollout. `RollingUpdate` -- the default, and the strategy of
    a Deployment that never mentions one -- creates the replacement before
    removing the original, so the two overlap by design. **When it does
    deadlock, the failure is silence.** No pod goes down, because the old one
    is not permitted to leave until the new one is ready and the new one never
    will be; the Deployment reports `Available` throughout, the rollout
    eventually records `ProgressDeadlineExceeded`, and the workload carries on
    serving the image it was already running. Every subsequent change is
    accepted by the API server and lands nowhere, which is how a workload with
    a fully reconciled GitOps repository behind it can sit on a year-old image.

    The second is a replica count. Above one replica the pods coexist by
    definition rather than during a rollout, and the scheduler spreads a
    ReplicaSet's pods across nodes by default -- so what does not co-locate
    stays pending, and what does co-locate has given up the redundancy the
    replica count was asking for.

    That first shape is very common and it is the one worth a check, because
    every other reading of the object says the workload is fine. It needs no
    unusual manifest: a Deployment, a claim, and the default strategy are
    enough, which describes most of the single-instance datastores and
    dashboards that get run as Deployments rather than StatefulSets.

    3.14 comes at `spec.strategy` from the opposite side and names this
    situation without being able to detect it -- its remediation says to
    "check first whether `Recreate` was chosen on purpose", because a workload
    holding a `ReadWriteOnce` volume needs it. This is the check that knows.
    The two cannot both fire on one object: 3.14 excludes single-replica
    Deployments, and this one recommends `Recreate` only there.

    Returns `None` on `Recreate`, which is the fix already applied, and on
    StatefulSets, whose `volumeClaimTemplates` give each replica a claim of
    its own and whose update strategy replaces one member at a time rather
    than surging.
    """
    if workload["kind"] != "Deployment":
        return None
    strategy = workload["spec"].get("strategy") or {}
    if strategy.get("type") == "Recreate":
        return None
    replicas = workload["spec"].get("replicas", 1)
    if replicas is None:
        replicas = 1
    # Scaled to nought there is no pod, so there is no contention and no
    # rollout to deadlock. S5 already drops these before `normalize_workloads`
    # returns, so this is belt and braces rather than the working exclusion --
    # it is here so the two arms below never have to reason about nought, and
    # so a caller that assembles workloads some other way gets the same answer.
    if replicas < 1:
        return None
    surge = _max_surge_count((strategy.get("rollingUpdate") or {}), replicas)
    if replicas == 1 and surge < 1:
        return None
    claims = _exclusive_claims(workload, context)
    if not claims:
        return None
    joined = "; ".join(claims)
    if replicas > 1:
        return {
            "object": f"Deployment/{workload['name']}",
            "excerpt": (
                f"replicas={replicas}, all of them mounting one exclusively-attachable claim: {joined}"
            ),
            "impact": _IMPACT_RWO_REPLICAS_PINNED,
        }
    return {
        "object": f"Deployment/{workload['name']}",
        "excerpt": (
            f"replicas=1, strategy RollingUpdate with maxSurge resolving to {surge}, "
            f"mounting an exclusively-attachable claim: {joined}"
        ),
        "impact": _IMPACT_RWO_ROLLOUT_DEADLOCK,
    }


def _max_unavailable_count(strategy: dict, replicas: int) -> int | None:
    """`maxUnavailable` resolved against a replica count, or `None` if unset.

    The field is an `IntOrString`: `2` means two pods, `"25%"` means a
    proportion, and Kubernetes rounds a percentage *down* for `maxUnavailable`
    so that rounding can never take out more pods than asked. Reproducing that
    rounding direction matters here -- rounding up would make `"50%"` of 3
    resolve to 2, and this check would then call a perfectly ordinary rollout a
    total outage.
    """
    raw = strategy.get("maxUnavailable")
    if raw is None:
        return None
    text = str(raw).strip()
    if text.endswith("%"):
        try:
            return int(replicas * float(text[:-1]) / 100)
        except ValueError:
            return None
    try:
        return int(text)
    except ValueError:
        return None


def check_strategy_causes_downtime(workload: dict, context: dict) -> dict | None:
    """A Deployment whose own update strategy takes every replica down at once.

    Two spellings of the same outage. `strategy.type: Recreate` tells the
    Deployment controller to delete every existing pod and only then create the
    replacements, so a workload with three replicas serves nothing from the
    first deletion until the first new pod passes its readiness probe -- an
    image pull plus a cold start, tens of seconds at best and minutes for
    anything that warms a cache on boot. `RollingUpdate` with `maxUnavailable`
    at or above the replica count is the same thing written the long way: the
    controller is permitted to take all of them down in one step, and it does.

    This is worth its own check because it is invisible in every other reading
    of the object. The Deployment reports `Available` between rollouts, the
    replica count is whatever the author intended, the probes are correct, and
    there is no event to find afterwards -- the outage lasts only as long as
    the rollout and leaves nothing behind but errors in a client's logs. It
    surfaces only when someone reads `spec.strategy`, and until now nothing in
    this collector did except to suppress: `check_single_replica` returns
    `None` on `Recreate` (a single replica has an outage on any strategy, so
    naming the strategy there would be the wrong finding), and returns `None`
    again whenever `replicas != 1`. A `Recreate` Deployment with three replicas
    fell through both arms and was reported by nothing.

    Not a finding on a single replica, for that same reason, and not one on
    `maxUnavailable` merely being large: `maxUnavailable: 2` of 3 is a
    deliberate speed-over-capacity choice that still leaves a pod serving.
    Only reaching the replica count is unambiguous, because at that point the
    strategy permits zero surviving pods.
    """
    if workload["kind"] != "Deployment":
        return None
    replicas = workload["spec"].get("replicas", 1) or 1
    if replicas <= 1:
        return None
    strategy = workload["spec"].get("strategy") or {}
    kind = strategy.get("type") or "RollingUpdate"
    if kind == "Recreate":
        detail = f"strategy.type=Recreate, replicas={replicas}"
    else:
        unavailable = _max_unavailable_count(strategy.get("rollingUpdate") or {}, replicas)
        if unavailable is None or unavailable < replicas:
            return None
        raw = (strategy.get("rollingUpdate") or {}).get("maxUnavailable")
        detail = (
            f"strategy.rollingUpdate.maxUnavailable={raw} resolves to {unavailable} "
            f"of {replicas} replicas"
        )
    services = _selecting_services(workload, context)
    exposure = f"\n{_exposure_line(services)}" if services else ""
    return {
        "object": f"Deployment/{workload['name']}",
        "excerpt": f"{detail}{exposure}",
        # A rollout that drops every replica of something serving user traffic
        # is an outage; the same strategy on a workload nothing routes to is a
        # latent one, waiting for the first Service to be pointed at it.
        "severity": "major" if services and _exposure_scope(services) == _SCOPE_SERVING else "minor",
    }


def _rfc3339(stamp: str) -> float | None:
    """Kubernetes timestamp -> epoch seconds, or `None` if it will not parse.

    `None` rather than an exception because every caller here is deciding
    whether to *report* something: a timestamp the API served in a shape this
    does not expect is a reason to stay quiet about that object, not a reason
    to fail the cluster's whole collection and lose every other check.
    """
    if not stamp:
        return None
    try:
        return datetime.datetime.strptime(stamp, TIMESTAMP_FORMAT).replace(
            tzinfo=datetime.timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return None


def _job_finished(job: dict) -> bool:
    """Succeeded and no longer active, or carrying a terminal `Failed` condition.

    `failed` alone is not finished: between retries a Job reads `failed=1`,
    `active=0` and no terminal condition, and the controller is about to
    start another pod. §3.12 says such a Job has not finished.
    """
    status = job.get("status") or {}
    if status.get("active"):
        return False
    if status.get("succeeded"):
        return True
    return any(
        c.get("type") == "Failed" and c.get("status") == "True" for c in status.get("conditions") or []
    )


def _failure_summary(finished: list[dict]) -> str:
    """The `Failed` condition's reason on the most recent Job, as a clause.

    Empty string when there is nothing to add, so the caller can concatenate it
    unconditionally.

    Without this the finding says a schedule has stopped succeeding and stops
    there, which sends the reader to `kubectl describe` to learn the one thing
    that decides what to do about it. `BackoffLimitExceeded` means the
    container is exiting non-zero and the fix is in the image or its
    arguments; `DeadlineExceeded` means it is running out of
    `activeDeadlineSeconds`, and the fix is usually the deadline or the
    schedule's period rather than the code. Those are different remediations
    and the Job already carries which one applies.

    Only the newest Job, not all of them: the retained set is at most
    `failedJobsHistoryLimit` deep and consecutive failures of one schedule
    almost always share a reason, so listing three of them pads the excerpt
    without adding a fact. `finished` arrives sorted by `creationTimestamp`
    because `cronjobs_with_jobs` sorts it.
    """
    if not finished:
        return ""
    conditions = (finished[-1].get("status") or {}).get("conditions") or []
    for condition in conditions:
        if condition.get("type") == "Failed" and str(condition.get("status")) == "True":
            reason = str(condition.get("reason") or "").strip()
            return f" (most recent: {reason})" if reason else ""
    return ""


def check_schedule_never_succeeds(context: dict) -> list[dict]:
    """Cluster-scoped: iterates CronJobs, which are not in `workloads` at all.

    The other checks read pod templates and ask whether the workload is
    configured to survive something. This one asks whether a schedule is still
    producing anything, which is not a question a template can answer -- so it
    reads `status` on the CronJob and on the Jobs it retains, and reports the
    CronJob rather than any individual Job. A Job is one attempt; the finding
    is about the run of them.

    Both times come off the dump and neither is compared against `now`, for
    the reason `STALE_SUCCESS_GAP_HOURS` gives.
    """
    hits = []
    for entry in context["cronjobs"]:
        cronjob = entry["cronjob"]
        spec, status = cronjob.get("spec") or {}, cronjob.get("status") or {}
        if spec.get("suspend"):
            continue
        last_schedule = _rfc3339(status.get("lastScheduleTime", ""))
        if last_schedule is None:
            # Never fired. A CronJob that has not come due yet has nothing to
            # have failed at, and one whose schedule has passed without firing
            # is a scheduler problem this check would misattribute.
            continue
        finished = [job for job in entry["jobs"] if _job_finished(job)]
        if not finished or any((job.get("status") or {}).get("succeeded") for job in finished):
            # An active Job counts as neither, deliberately: an hourly schedule
            # almost always has one in flight, and letting it veto the check
            # would make the check unreachable on the schedules it most needs
            # to cover.
            continue
        last_success = status.get("lastSuccessfulTime", "")
        # Never succeeded at all -> measure from when the CronJob was created,
        # which is the earliest moment it could have. Without this arm the
        # worst case in the check's remit, a schedule that has never once
        # worked, is the one case it stays silent about.
        baseline_field = "lastSuccessfulTime" if last_success else "creationTimestamp"
        baseline = _rfc3339(last_success or (cronjob.get("metadata") or {}).get("creationTimestamp", ""))
        if baseline is None:
            continue
        gap_hours = (last_schedule - baseline) / SECONDS_PER_HOUR
        if gap_hours < STALE_SUCCESS_GAP_HOURS:
            continue
        hits.append(
            {
                "namespace": entry["ns"],
                "object": f"CronJob/{entry['name']}",
                "excerpt": (
                    f"schedule={spec.get('schedule', '')!r} suspend=false; "
                    f"last fired {status.get('lastScheduleTime', '')}, "
                    f"{baseline_field}={last_success or (cronjob.get('metadata') or {}).get('creationTimestamp', '')} "
                    f"({gap_hours:.0f}h without a successful run); "
                    f"{len(finished)} retained Job(s), all failed"
                    f"{_failure_summary(finished)}"
                ),
                "reconciler": reconciler_of(cronjob.get("metadata") or {}),
                "release": release_of(cronjob.get("metadata") or {}),
            }
        )
    return hits


# The CronJob controller's own default when `spec.concurrencyPolicy` is unset.
# It is the permissive one, which is why the absent case and the explicit case
# are the same finding.
DEFAULT_CONCURRENCY_POLICY = "Allow"

# How many retained Job creation times it takes to call a cadence observed.
# Two timestamps make one gap, and one gap is a coincidence -- a single retry
# would read as the schedule's period. Three give two gaps to take the wider
# of, which is what `_observed_period_s` does.
MIN_JOBS_FOR_PERIOD = 3


def _observed_period_s(jobs: list[dict]) -> float | None:
    """How often this schedule actually fires, read off the Jobs it retained.

    The alternative is parsing `spec.schedule`, and the comment on
    `STALE_SUCCESS_GAP_HOURS` already gives the reason not to: this collector
    has no business learning cron syntax, and a verdict that depends on a
    hand-rolled cron parser is a verdict that fails in whichever direction the
    parser is wrong. Consecutive `creationTimestamp`s are the same fact,
    already in the dump, in a form that needs no clock and no grammar.

    The *widest* gap rather than the narrowest. Retained Jobs are a sample with
    holes in it -- `successfulJobsHistoryLimit` evicts, and a run that was
    skipped or suspended leaves a gap of several periods -- so the narrowest
    gap is the closest thing to the true period and the widest is an
    overestimate of it. Overestimating is the safe direction here: the caller
    reports overlap when a run lasts *longer* than a period, so a period that
    is too long makes the check quieter, never louder.
    """
    stamps = sorted(
        stamp
        for stamp in (_rfc3339((job.get("metadata") or {}).get("creationTimestamp", "")) for job in jobs)
        if stamp is not None
    )
    if len(stamps) < MIN_JOBS_FOR_PERIOD:
        return None
    gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:]) if later > earlier]
    return max(gaps) if gaps else None


def _longest_run_s(jobs: list[dict]) -> tuple[float | None, str]:
    """The longest completed run among the retained Jobs, and which one it was.

    Only Jobs that both started and finished. A Job still running has no
    `completionTime`, and treating "started a long time ago and has not
    finished" as a duration would report the one case this check must not
    guess at: a Job that is wedged rather than slow.
    """
    longest, name = None, ""
    for job in jobs:
        status = job.get("status") or {}
        start = _rfc3339(status.get("startTime", ""))
        end = _rfc3339(status.get("completionTime", ""))
        if start is None or end is None or end < start:
            continue
        if longest is None or (end - start) > longest:
            longest, name = end - start, (job.get("metadata") or {}).get("name", "")
    return longest, name


def check_cronjob_runs_overlap(context: dict) -> list[dict]:
    """Cluster-scoped: a CronJob that takes longer to run than it waits to rerun.

    `concurrencyPolicy: Allow` -- the default, so almost always unstated -- lets
    the controller start the next run whether or not the last one finished. That
    is harmless until a run outlasts its own period, and then it is a pile-up:
    every period adds a worker without removing one, so a schedule that fires
    every five minutes and takes six accumulates runs until something in the
    cluster runs out. The failure is not the CronJob's. It lands on whatever
    the job talks to -- connection-pool exhaustion on a database, duplicate rows
    from two copies of the same import, a node's memory going to N copies of a
    batch process -- which is why the CronJob itself looks healthy the whole
    time and every retained Job reports `succeeded`.

    Measured, not predicted. The check does not read `spec.schedule` and does
    not guess at how long a run should take: it compares the longest run the
    cluster actually recorded against the widest gap between the runs it
    actually started, both off timestamps already in the dump. So a hit says
    this schedule has already overlapped, not that its policy would allow it
    to -- which is the difference between a finding and a lint rule, given that
    the permissive policy is the default and therefore describes most CronJobs
    in any fleet.

    `Forbid` and `Replace` are both excluded because both bound the pile-up:
    `Forbid` skips the new run, `Replace` kills the old one. Which of the two
    to move to is a real decision -- skipping a run loses that period's work,
    killing one loses the work in flight -- so this reports the collision and
    leaves the choice to whoever knows what the job does.
    """
    hits = []
    for entry in context["cronjobs"]:
        cronjob = entry["cronjob"]
        spec = cronjob.get("spec") or {}
        if spec.get("suspend"):
            continue
        policy = spec.get("concurrencyPolicy") or DEFAULT_CONCURRENCY_POLICY
        if policy != DEFAULT_CONCURRENCY_POLICY:
            continue
        period = _observed_period_s(entry["jobs"])
        if period is None:
            continue
        longest, slowest = _longest_run_s(entry["jobs"])
        if longest is None or longest < period:
            continue
        stated = "concurrencyPolicy=Allow" if spec.get("concurrencyPolicy") else "concurrencyPolicy unset (defaults to Allow)"
        hits.append(
            {
                "namespace": entry["ns"],
                "object": f"CronJob/{entry['name']}",
                "excerpt": (
                    f"schedule={spec.get('schedule', '')!r} {stated}; "
                    f"slowest retained run {slowest} took {longest / SECONDS_PER_MINUTE:.1f}m "
                    f"against an observed period of {period / SECONDS_PER_MINUTE:.1f}m "
                    f"across {len(entry['jobs'])} retained Job(s), "
                    "so a run is still going when the next one starts"
                ),
                "reconciler": reconciler_of(cronjob.get("metadata") or {}),
                "release": release_of(cronjob.get("metadata") or {}),
            }
        )
    return hits


def check_service_selects_nothing(context: dict) -> list[dict]:
    """Cluster-scoped: a Service whose selector matches no running pod.

    A Service is a label query, and nothing validates that the query matches
    anything. Rename a Deployment's pod labels, move a workload to another
    namespace, typo a value in a values file -- the Service still admits, still
    gets a ClusterIP, still resolves in DNS, and every request to it fails with
    a connection refused that names the Service rather than the workload that
    is no longer behind it. There is no event, no condition, and no status
    field that says so; the object looks identical to a healthy one.

    Read off EndpointSlices, which is the controller's own answer to "what does
    this selector currently match" -- so a hit is what the cluster resolved,
    not what this script thinks the selector should match. Terminating
    endpoints do not count, per `services_with_endpoints`.

    A Service pointing at a workload deliberately parked at `replicas: 0` has
    no endpoints and is not broken, so it is excluded by name rather than by
    guess: `zeroed_by` carries the workload that explains it.

    Severity splits on whether the address is published outside the cluster. A
    LoadBalancer or NodePort with no backends is a black hole with a public
    address on it, and whatever is pointed at that address -- DNS, a CDN
    origin, a partner's allowlist -- is already failing. A ClusterIP with no
    backends breaks its in-cluster callers, which is bad but bounded by who
    knows the name.
    """
    hits = []
    for entry in context["service_endpoints"]:
        if entry["endpoints"] or entry["zeroed_by"]:
            continue
        service = entry["service"]
        spec = service.get("spec") or {}
        svc_type = spec.get("type") or DEFAULT_SERVICE_TYPE
        selector = ",".join(f"{k}={v}" for k, v in sorted(entry["selector"].items()))
        ports = ",".join(
            str(p.get("port", "")) for p in spec.get("ports") or []
        )
        hits.append(
            {
                "namespace": entry["ns"],
                "object": f"Service/{entry['name']}",
                "severity": "critical" if svc_type in EXTERNAL_SERVICE_TYPES else "major",
                "excerpt": (
                    f"type={svc_type} port(s)={ports or 'none'} selector={selector!r}; "
                    "the EndpointSlices for this Service list no non-terminating "
                    "address, so every request to it is refused"
                ),
                "reconciler": reconciler_of(service.get("metadata") or {}),
                "release": release_of(service.get("metadata") or {}),
            }
        )
    return hits


def check_service_port_unresolved(context: dict) -> list[dict]:
    """Cluster-scoped: a Service whose `targetPort` names a port no container declares.

    The near miss of §3.16, and the one that survives review more easily. The
    selector is right, the pods are up, the EndpointSlices list them, and
    `kubectl get endpoints` prints addresses — so every check anyone would
    think to run says the Service is healthy. But a `targetPort` given as a
    name is resolved against the pod's `containerPort` names, and when no
    container declares that name the controller drops the port from the slice
    rather than guessing. kube-proxy programs nothing, and the Service refuses
    on that port while looking entirely fine.

    It is a rename that does it: a container port renamed `web` to `http`, a
    chart whose Service and Deployment templates take the name from two
    different values, a port declaration deleted as tidying. The Service is
    usually not the file that changed.

    Numeric `targetPort`s cannot fail this way — `containerPort` is
    informational and traffic goes to the number regardless — so only named
    ones are collected, and a Service with no ports at all has nothing to
    resolve.

    Endpoints are required, which is what keeps this from double-reporting
    §3.16: a Service with no backends is that finding, not this one.

    Under-reports one case on purpose. The controller groups pods by the ports
    they resolve, so a Deployment mid-rename with old and new pods both up
    produces two slices, one carrying the port and one not; the union reads as
    resolved and this stays quiet. Half the callers are failing, but a check
    that fires during every rolling update of every renamed port would be
    turned off within a week.
    """
    hits = []
    for entry in context["service_endpoints"]:
        if not entry["endpoints"]:
            continue
        broken = [
            (port, target)
            for port, target in entry["named_targets"]
            if port not in entry["resolved_ports"]
        ]
        if not broken:
            continue
        service = entry["service"]
        svc_type = (service.get("spec") or {}).get("type") or DEFAULT_SERVICE_TYPE
        asked = "; ".join(
            f"port {port or '(unnamed)'} wants targetPort {target!r}" for port, target in broken
        )
        declared = ", ".join(entry["declared_ports"]) or "no ports at all"
        backend = entry["backend"] or "the workload behind it"
        hits.append(
            {
                "namespace": entry["ns"],
                "object": f"Service/{entry['name']}",
                "severity": "critical" if svc_type in EXTERNAL_SERVICE_TYPES else "major",
                "excerpt": (
                    f"type={svc_type} {asked}; the EndpointSlices carry "
                    f"{entry['endpoints']} non-terminating address(es) and no entry for that "
                    f"port, because {backend} declares {declared}"
                ),
                "reconciler": reconciler_of(service.get("metadata") or {}),
                "release": release_of(service.get("metadata") or {}),
            }
        )
    return hits


# --------------------------------------------------------------------------- #
# compliance-audit: all sixteen checks of §2.
#
# Unlike obtainability's Deployment/StatefulSet/DaemonSet templates, this
# stream's workload dump includes bare Pods (owned ones excluded — audit the
# owning controller, per the SOP's own reasoning) and reads `.spec` directly,
# resolved per kind: a Pod's `.spec` *is* the pod spec; a CronJob's is nested
# two objects deeper. `_pod_spec_of` is that resolution, done once instead of
# three times.
# --------------------------------------------------------------------------- #

COMPLIANCE_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Pod")
# The subset carrying `spec.replicas`, so a zero there means "no pod". A
# DaemonSet has no such field (its count is the node count), and a bare Pod
# either exists or does not.
_SCALABLE_WORKLOAD_KINDS = ("Deployment", "StatefulSet")
COMPLIANCE_DUMP_KINDS = "deploy,sts,ds,cronjob,pod"
_SYSTEM_SA_NAMESPACE_RE_PARTS = (
    "kube-system", "gmp-system", "cnrm-system", "configconnector-operator-system", "krmapihosting-system",
)


def _pod_spec_of(item: dict) -> dict:
    kind, spec = item.get("kind"), item.get("spec") or {}
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    return (spec.get("template") or {}).get("spec") or {}


def _pod_annotations_of(item: dict) -> dict:
    """The annotations on the pod `_pod_spec_of` resolves, from the same place."""
    kind, spec = item.get("kind"), item.get("spec") or {}
    if kind == "Pod":
        meta = item.get("metadata") or {}
    elif kind == "CronJob":
        meta = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("metadata") or {}
    else:
        meta = (spec.get("template") or {}).get("metadata") or {}
    return meta.get("annotations") or {}


# What every excerpt on a suspended CronJob gains, and why one is not enough to
# drop the finding. `spec.suspend: true` stops the controller creating Jobs, so
# nothing in the template is running now and nothing will start on a schedule --
# but the template is still what runs the moment someone unsuspends it, which is
# the shape both compliance and ai-security exist to audit, and suspension is a
# field one `kubectl patch` undoes.
#
# What it does change is the tense a reader should give the impact. The
# 2026-09-05 ai-security report told an operator that `CronJob/ai-batch-finetune`
# pulls an unpinned model and that "the bytes that arrive at the next pod restart
# are whatever the source serves then" -- about a CronJob suspended since it was
# created, whose `status.lastScheduleTime` is empty and which has never produced
# a Job. Every word of the detection was right and the sentence built on it
# described something that cannot currently happen.
#
# Stated here rather than left to the model because `adopt_collector_evidence`
# overwrites the model's excerpt with this one: prose in an SOP would not reach
# the reader, and the excerpt is what the impact gets written from.
_SUSPENDED_CRONJOB_NOTE = (
    " (CronJob is suspended: spec.suspend=true, so no Job runs on the schedule "
    "until it is resumed -- the template below is still what runs when it is)"
)


def _is_suspended_cronjob(item: dict) -> bool:
    return item.get("kind") == "CronJob" and (item.get("spec") or {}).get("suspend") is True


# The same defect in the other direction, for the kinds that cannot suspend. A
# Deployment or StatefulSet at `spec.replicas: 0` has no pod, so every present
# tense in an impact is wrong for the same reason, and it is undone by the same
# one-field patch -- audit the template, correct the tense.
#
# The instance: the 2026-09-06 08:55Z ai-security report told an operator that
# `Deployment/ai-embeddings-tei` "executes arbitrary code shipped inside the
# model repository, with this pod's ServiceAccount, network access, and mounted
# volumes". That Deployment has sat at `replicas: 0` throughout -- there is no
# pod, so there is no ServiceAccount in use and nothing is mounted. The same
# finding then told them to confirm the fix with `kubectl logs
# deploy/ai-embeddings-tei --tail=50`, which returns nothing at zero replicas.
# That is the sharper half of the cost: a wrong tense misleads, but a
# verification step that cannot produce output sends the operator looking for a
# broken cluster.
#
# Not folded into `_SUSPENDED_CRONJOB_NOTE`: the two are disjoint by kind, and
# the field a reader has to go look at differs. The note names the kind because,
# unlike suspension, this reaches two of them.
def _scaled_to_zero_note(kind: str) -> str:
    return (
        f" ({kind} is scaled to zero: spec.replicas=0, so no pod is running from "
        "this template and commands that read one return nothing until it is "
        "scaled back up -- the template below is still what runs when it is)"
    )


# Explicit `0` only. An absent `replicas` means one, not none, and a `None` from
# a partial object is unknown rather than zero -- both are the running case.
def _is_scaled_to_zero(item: dict) -> bool:
    return (
        item.get("kind") in _SCALABLE_WORKLOAD_KINDS
        and (item.get("spec") or {}).get("replicas") == 0
    )


def normalize_compliance_workloads(dump: dict) -> list[dict]:
    """Every object surviving compliance's universal suppressions, as
    `{kind, ns, name, spec}` where `spec` is the resolved **pod** spec —
    compliance's checks read `securityContext`, `hostNetwork`, `volumes`,
    never the owning object's own fields. Bare Pods are included and owned
    ones excluded (audit the controller, never the pod, whose name carries a
    random suffix) — the opposite inclusion rule from `normalize_workloads`,
    which drops Pods from its kind list entirely because obtainability reads
    templates, not live objects.
    """
    out = []
    for item in dump.get("items", []) or []:
        if item.get("kind") not in COMPLIANCE_WORKLOAD_KINDS:
            continue
        meta = item.get("metadata") or {}
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):
            continue
        labels = meta.get("labels") or {}
        if "addonmanager.kubernetes.io/mode" in labels:
            continue
        if (meta.get("annotations") or {}).get("components.gke.io/component-name"):
            continue
        if item.get("kind") == "Pod" and meta.get("ownerReferences"):
            continue
        out.append({
            "kind": item["kind"], "ns": ns, "name": meta.get("name", ""),
            "spec": _pod_spec_of(item), "pod_annotations": _pod_annotations_of(item),
            "suspended": _is_suspended_cronjob(item),
            "scaled_to_zero": _is_scaled_to_zero(item),
            "reconciler": reconciler_of(meta),
            "release": release_of(meta),
        })
    return out


def check_privileged_container(workload: dict, context: dict) -> dict | None:
    bad = [
        c.get("name", "")
        for c in (workload["spec"].get("containers") or []) + (workload["spec"].get("initContainers") or [])
        if (c.get("securityContext") or {}).get("privileged") is True
        or "SYS_ADMIN" in ((c.get("securityContext") or {}).get("capabilities") or {}).get("add", [])
    ]
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": f"privileged/SYS_ADMIN: {', '.join(bad)}"}


# §2.2's flag-when is an **or** over three independent namespaces, so one
# sentence covering all three is false on most of what the check catches: a
# hostNetwork-only pod crosses no process boundary, and a hostPID/hostIPC-only
# pod is still fully inside NetworkPolicy. Each flag contributes its own clause
# and only the ones actually set are published.
_IMPACT_HOST_PID = (
    "hostPID puts every other pod's process table in this workload's /proc, so "
    "the command lines and argv-borne configuration of every tenant on the node "
    "are readable from here; where the container runs as root, which is the "
    "default, /proc/<pid>/root also reaches into those containers' filesystems."
)
_IMPACT_HOST_IPC = (
    "hostIPC shares the node's System V IPC and POSIX shared-memory segments, "
    "so this workload can read and write memory that other tenants' processes "
    "expect to be private to their own pod."
)
# Not "bypasses enforcement", which reads as a policy that is merely weaker.
# Neither GKE Dataplane V2 nor Calico enforces NetworkPolicy on a host-networked
# pod at all -- upstream leaves the behaviour undefined, and Calico's
# `IsValidCalicoWorkloadEndpoint` rejects such pods outright, which is why they
# vanish as rule peers as well as targets (projectcalico#1987, closed
# not_planned).
_IMPACT_HOST_NETWORK = (
    "hostNetwork takes this pod out of NetworkPolicy rather than loosening it: "
    "neither GKE Dataplane V2 nor Calico enforces policy on a host-networked "
    "pod, and it disappears as a rule peer as well, so a podSelector elsewhere "
    "written to admit it never matches. From another node its traffic arrives "
    "as the node IP, which only an ipBlock over the node CIDR can describe; "
    "from the same node it is allowed unconditionally, with no ipBlock "
    "recourse. It also reaches every node-local listener bound to 127.0.0.1, "
    "including ones whose owners took loopback for an isolation boundary."
)


def check_host_namespace(workload: dict, context: dict) -> dict | None:
    spec = workload["spec"]
    host_pid, host_ipc, host_net = bool(spec.get("hostPID")), bool(spec.get("hostIPC")), bool(spec.get("hostNetwork"))
    if not (host_pid or host_ipc or host_net):
        return None
    severity = "critical" if (host_pid or host_ipc) else "major"
    has_host_port = any(p.get("hostPort") for c in spec.get("containers") or [] for p in c.get("ports") or [])
    if severity == "major" and workload["kind"] == "DaemonSet" and has_host_port:
        # §2.2's ingress/gateway data-plane downgrade: hostNetwork is the
        # only flag set and a hostPort is declared -- record it rather than
        # suppressing silently.
        severity = "minor"
    clauses = [
        clause
        for flag, clause in (
            (host_pid, _IMPACT_HOST_PID),
            (host_ipc, _IMPACT_HOST_IPC),
            (host_net, _IMPACT_HOST_NETWORK),
        )
        if flag
    ]
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"hostNetwork={host_net} hostPID={host_pid} hostIPC={host_ipc}",
        "severity": severity,
        "impact": " ".join(clauses),
    }


_SENSITIVE_HOSTPATHS = ("/", "/etc", "/proc", "/var/run/docker.sock", "/run/containerd/containerd.sock")
# §2.3's log-shipper pattern, `minor` when every mount of it is read-only.
_LOG_SHIPPER_HOSTPATHS = ("/var/log", "/var/lib/docker/containers")


def check_hostpath_mount(workload: dict, context: dict) -> dict | None:
    host_volumes = {
        v["name"]: v["hostPath"]["path"]
        for v in workload["spec"].get("volumes") or []
        if v.get("hostPath", {}).get("path")
    }
    if not host_volumes:
        return None
    mounted = []
    for container in (workload["spec"].get("containers") or []) + (workload["spec"].get("initContainers") or []):
        for vm in container.get("volumeMounts") or []:
            if vm.get("name") in host_volumes:
                mounted.append((host_volumes[vm["name"]], bool(vm.get("readOnly"))))
    if not mounted:
        return None

    def sensitive(path: str) -> bool:
        return path in _SENSITIVE_HOSTPATHS or path.startswith("/var/lib/kubelet")

    critical = any(sensitive(p) or not ro for p, ro in mounted)
    log_shipper = all(ro and p.startswith(_LOG_SHIPPER_HOSTPATHS) for p, ro in mounted)
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": "; ".join(f"{p} readOnly={ro}" for p, ro in mounted),
        "severity": "critical" if critical else "minor" if log_shipper else "major",
    }


_SYSTEM_PRINCIPAL_RE = re.compile(r"^system:")
# Kubernetes' own groups: dotless (`apps`, `batch`, `policy`) or under
# `k8s.io` (`rbac.authorization.k8s.io`). `x-k8s.io` is the SIG-project
# namespace for CRDs and stays a vendor group.
_BUILTIN_API_GROUP_SUFFIX = ".k8s.io"


def _is_builtin_api_group(group: str) -> bool:
    """A `*`/`*` over one of these is not an operator owning its own CRDs:
    over `rbac.authorization.k8s.io` it can grant itself cluster-admin."""
    return "." not in group or group.endswith(_BUILTIN_API_GROUP_SUFFIX)
_MANAGED_IDENTITY_RE = re.compile(r"^gke-|^service-\d+@|\.gserviceaccount\.com$")
_ORG_EMAIL_GROUP_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _is_non_system_subject(subject: dict) -> tuple[bool, bool]:
    """Returns (flagged, is_org_email_group) for one binding subject."""
    kind, name, ns = subject.get("kind"), subject.get("name", ""), subject.get("namespace", "")
    if kind == "ServiceAccount":
        return ns not in _SYSTEM_SA_NAMESPACE_RE_PARTS and not ns.startswith("gke-") and not ns.startswith("config-management-"), False
    if kind in ("User", "Group"):
        if _SYSTEM_PRINCIPAL_RE.match(name) or _MANAGED_IDENTITY_RE.search(name):
            return False, False
        is_org_group = kind == "Group" and bool(_ORG_EMAIL_GROUP_RE.match(name))
        return True, is_org_group
    return False, False


def check_cluster_admin_binding(context: dict) -> list[dict]:
    hits = []
    for crb in context.get("clusterrolebindings") or []:
        role_ref = crb.get("roleRef") or {}
        if role_ref.get("name") != "cluster-admin":
            continue
        crb_meta = crb.get("metadata") or {}
        name = crb_meta.get("name", "")
        # One candidate per binding, naming every flagged subject: the finding
        # id is (check, cluster, namespace, object), so a hit per subject
        # would hand `finish` two candidates with one identity -- a document
        # carrying both is refused, and one carrying either loses the other
        # subject from the excerpt. The worst subject sets the severity: an
        # org group beside a ServiceAccount does not make the SA's grant minor.
        targets, all_org_groups = [], True
        for subject in crb.get("subjects") or []:
            flagged, is_org_group = _is_non_system_subject(subject)
            if not flagged:
                continue
            targets.append(f"{subject.get('kind')}/{subject.get('namespace', '-')}/{subject.get('name')}")
            all_org_groups = all_org_groups and is_org_group
        if not targets:
            continue
        hits.append(
            {
                "namespace": "",
                "object": f"ClusterRoleBinding/{name}",
                "excerpt": f"{name} -> {', '.join(targets)}",
                "severity": "minor" if all_org_groups else "critical",
                "reconciler": reconciler_of(crb_meta),
                "release": release_of(crb_meta),
            }
        )
    return hits


_WILDCARD_BOOTSTRAP_LABEL = "kubernetes.io/bootstrapping"

# Verbs that make a wildcard *scope* an escalation rather than a broad read.
# Used only by the second stage-1 branch below, the one that fires without a
# `*` in `verbs`; a rule that already carries the verb wildcard is caught
# whatever it lists.
#
# Deliberately excludes get/list/watch. A ClusterRole of `apiGroups: ["*"],
# resources: ["*"], verbs: ["get","list","watch"]` is the ordinary
# cluster-monitoring shape -- Prometheus, a backup agent, the `view` role's
# cousins -- and reporting every one of those as critical is the
# false-positive flood this audit already learned to avoid once. Reading every
# Secret in the fleet is a real concern and it is not this check's; it needs a
# rule that can tell a scraper from an escalation, which this one cannot.
_ESCALATING_VERBS = frozenset(
    {"create", "update", "patch", "delete", "deletecollection", "impersonate", "escalate", "bind"}
)

# The three verbs RBAC escalation prevention keys on. A subject holding one of
# them reaches the ceiling directly; every other enumerated verb reaches it the
# long way, and §2.5 forbids describing the two the same way.
_RBAC_DIRECT_VERBS = ("bind", "escalate", "impersonate")
_IMPACT_RBAC_ANY_VERB = (
    "Subject can perform any verb on any resource in this scope, including "
    "reading Secrets and creating privileged pods -- an unbounded escalation path."
)
# §2.5 branch 3, one clause per verb family actually present and no others.
# Ordered by what an owner acts on first, not alphabetically.
_RBAC_VERB_CLAUSES = (
    (("get", "list"), "read every Secret in every namespace"),
    (("create",), "create privileged pods directly"),
    (
        ("patch", "update"),
        "rewrite an existing privileged workload into one it controls and take the node that runs it",
    ),
    (("delete", "deletecollection"), "destroy any object in the cluster"),
)
# §2.5 says `delete` *alone* is "data loss and denial of service rather than
# credential theft". Alone is load-bearing: appended to a list that also grants
# `get`, it would deny the credential theft the sentence just described.
_RBAC_DELETE_ONLY_TAIL = " That is data loss and denial of service rather than credential theft."
_IMPACT_RBAC_INDIRECT_TAIL = (
    " RBAC escalation prevention refuses a subject holding none of `bind`, "
    "`escalate` or `impersonate` a binding granting more than it already has, "
    "so the route to cluster-admin here is real but indirect."
)


def _wildcard_rbac_impact(verbs: set[str]) -> str:
    """§2.5's Impact, chosen by the verbs the matched rules actually grant.

    The model was asked to do this from the excerpt and did it for one finding
    out of two: `ClusterRole/argocd-server` holds `delete`, `get`, `patch` on
    every resource in every group and published the wildcard sentence, which
    collapses exactly the direct/indirect distinction the branch exists to
    keep. The verbs are already parsed here, so the branch is arithmetic rather
    than a reading comprehension task.
    """
    if "*" in verbs:
        return _IMPACT_RBAC_ANY_VERB
    direct = [v for v in _RBAC_DIRECT_VERBS if v in verbs]
    if direct:
        # Same ceiling as the wildcard, so the sentence holds -- but say which
        # verb reaches it: that one verb is the whole finding, and an owner
        # trimming the list needs to know it cannot stay.
        return f"{_IMPACT_RBAC_ANY_VERB} `{direct[0]}` is the verb that reaches it."
    clauses = [text for family, text in _RBAC_VERB_CLAUSES if verbs.intersection(family)]
    if not clauses:
        # A verb list matching none of the shapes above. No hit reaches here
        # today -- stage 1 admits a rule only for a `*` verb or a member of
        # `_ESCALATING_VERBS`, and every one of those is a direct verb or a
        # clause family. It stays because the two lists are edited separately:
        # widening `_ESCALATING_VERBS` alone must not silently promote a new
        # verb to the wildcard sentence. §2.5 picks this direction on purpose --
        # understating an escalation costs a reader one follow-up, overstating
        # one costs the audit its standing.
        return (
            "The rule's scope is every resource in every API group. Read the verbs in the "
            "excerpt for what that grants; it is not a grant of every verb."
            + _IMPACT_RBAC_INDIRECT_TAIL
        )
    if len(clauses) > 1:
        listed = f"{', '.join(clauses[:-1])}, and {clauses[-1]}"
        tail = ""
    else:
        listed = clauses[0]
        tail = _RBAC_DELETE_ONLY_TAIL if verbs & set(_RBAC_VERB_CLAUSES[-1][0]) else ""
    return (
        f"The verbs are enumerated rather than wildcarded, so this is not a grant of every "
        f"verb. With what it does grant, the subject can {listed}."
        + tail
        + _IMPACT_RBAC_INDIRECT_TAIL
    )


def _binding_principal(subject: dict) -> str:
    """How a subject is spelled to `kubectl auth can-i --as`.

    The remediation §2.5 mandates is `kubectl auth can-i --list --as=<subject>`,
    and until this was carried on the hit the model had to invent the subject:
    `ClusterRole/argocd-server`'s finding told the operator to enumerate
    `argocd-application-controller` instead -- the *other* finding's subject,
    holding `verbs: ["*"]` on `["*"]`, a strict superset. Diffing a proposed
    replacement against a superset passes whatever the replacement says.
    """
    kind, name = subject.get("kind"), str(subject.get("name") or "")
    if kind == "ServiceAccount":
        return f"system:serviceaccount:{subject.get('namespace', '')}:{name}"
    return name


def _role_ref_key(ref: dict, namespace: str, name: str | None = None) -> tuple:
    """(kind, name, namespace) for a Role, (kind, name, "") for a ClusterRole.

    A RoleBinding's `roleRef` to a Role names one in the binding's own
    namespace; keyed on name alone, team-b's binding of its own `manager`
    Role would mark team-a's unbound `manager` as bound.
    """
    kind = ref.get("kind")
    return (kind, ref.get("name") if name is None else name, namespace if kind == "Role" else "")


def check_wildcard_rbac(context: dict) -> list[dict]:
    """The universal suppressions in §2 of the SOP say "every check in
    this section", and this is the check that never applied them: it has two
    suppressions of its own -- the `rbac-defaults` label and the `system:` name
    prefix -- and neither covers a GKE add-on.

    `kubelet-api-admin` is the one that costs. GKE ships it on every cluster
    with `verbs: ["*"]` over five `nodes/*` subresources in the core group,
    bound to `User/kube-apiserver` so the API server can reach kubelets for
    `kubectl logs` and `kubectl exec`. The subject test reads that user as
    non-system because the name carries no `system:` prefix, so the role
    counted as bound, and every cluster in the fleet reported one `critical`.
    Four of the eight stored compliance runs rendered it and four dropped it,
    which is worse than either -- the ledger is keyed on (check, cluster,
    namespace, object), so the same object churned as new and resolved run to
    run. The run that rendered it per cluster made it 16 of 34 findings. It is
    also unfixable: the label is `Reconcile`, so the add-on manager reverts an
    edit, and a server-side dry-run patch is refused outright.

    The label test is the same S2 rung the workload checks use, and the SOP
    names this exact failure -- "flagging these is the fastest way to get this
    audit switched off".
    """
    # Keyed the same as before, but keeping the subjects rather than throwing
    # them away: the remediation names a principal, and the only place that
    # principal exists is the binding.
    bound_non_system: dict[tuple, list[str]] = {}
    for kind_key in ("clusterrolebindings", "rolebindings"):
        for binding in context.get(kind_key) or []:
            role_ref = binding.get("roleRef") or {}
            for subject in binding.get("subjects") or []:
                flagged, _ = _is_non_system_subject(subject)
                if flagged:
                    key = _role_ref_key(role_ref, (binding.get("metadata") or {}).get("namespace", ""))
                    principal = _binding_principal(subject)
                    if principal not in bound_non_system.setdefault(key, []):
                        bound_non_system[key].append(principal)

    hits = []
    for role in (context.get("roles") or []):
        meta = role.get("metadata") or {}
        if (meta.get("labels") or {}).get(_WILDCARD_BOOTSTRAP_LABEL) == "rbac-defaults":
            continue
        if meta.get("name", "").startswith("system:"):
            continue
        if "addonmanager.kubernetes.io/mode" in (meta.get("labels") or {}):  # S2
            continue
        wildcard_rules = [
            rule
            for rule in role.get("rules") or []
            if (
                "*" in (rule.get("verbs") or [])
                and (
                    (rule.get("apiGroups") or []) == [""]
                    or "*" in (rule.get("resources") or [])
                    or "*" in (rule.get("apiGroups") or [])
                )
            )
            # Second branch: the scope is every resource in every apiGroup and
            # the verbs are written out instead of wildcarded. Requiring a `*`
            # in `verbs` missed those, and the miss is not academic -- on the
            # reference fleet `ClusterRole/argocd-server` holds
            # `apiGroups: ["*"], resources: ["*"], verbs: ["delete","get",
            # "patch"]`, bound to `ServiceAccount/argocd/argocd-server`, and
            # graded clean. `get` on every resource in every group is every
            # Secret in every namespace; `patch` on every resource is enough to
            # rewrite a Deployment into a privileged pod. Enumerating three
            # verbs rather than typing `*` is a spelling, not a boundary.
            or (
                (rule.get("apiGroups") or []) == ["*"]
                and "*" in (rule.get("resources") or [])
                and _ESCALATING_VERBS & {str(v).lower() for v in (rule.get("verbs") or [])}
            )
        ]
        # Vendor-apiGroup exception: a wildcard confined to one non-core
        # apiGroup that is not "*" itself is the operator-owns-its-own-CRDs
        # pattern, not an escalation. A wildcard over the core group ("")
        # is never suppressed, which the list-equality check above already
        # requires explicitly rather than falling out of the "*" membership
        # test (an apiGroups list of [""] contains no "*" at all).
        wildcard_rules = [
            r
            for r in wildcard_rules
            if (r.get("apiGroups") or []) == [""]
            or "*" in (r.get("apiGroups") or [])
            or len(set(r.get("apiGroups") or [])) != 1
            or _is_builtin_api_group((r.get("apiGroups") or [""])[0])
        ]
        if not wildcard_rules:
            continue
        key = _role_ref_key(role, meta.get("namespace", ""), name=meta.get("name"))
        if key not in bound_non_system:
            continue
        ns = meta.get("namespace", "")
        verbs = {
            str(v).lower() for rule in wildcard_rules for v in (rule.get("verbs") or [])
        }
        # The subjects go in the excerpt rather than the recommendation because
        # `adopt_collector_evidence` restores the excerpt over whatever the run
        # published: a principal written anywhere else is a principal the model
        # is free to replace with a plausible-looking wrong one.
        principals = ", ".join(bound_non_system[key])
        hits.append(
            {
                "namespace": ns,
                "object": f"{role['kind']}/{meta.get('name')}",
                "excerpt": f"{json.dumps(wildcard_rules)}; bound to {principals}",
                "severity": "critical" if role.get("kind") == "ClusterRole" else "major",
                "impact": _wildcard_rbac_impact(verbs),
                # The role's, not the binding's. Both appear in this check --
                # the principals in the excerpt come from `bound_non_system` --
                # but the remediation narrows the rule, so the reader needs
                # whatever holds the role.
                "reconciler": reconciler_of(meta),
                "release": release_of(meta),
            }
        )
    return hits


# The two subjects that name a caller which presented no credential at all.
# `system:anonymous` is the user the API server assigns such a request;
# `system:unauthenticated` is the group it puts that user in. They are
# interchangeable in a binding, so a check reading one and not the other
# reports half of the shape.
_ANONYMOUS_SUBJECTS = frozenset({"system:anonymous", "system:unauthenticated"})

# Every principal that did present a credential. On GKE that is every Google
# account with `container.clusters.get` on the project, which is a set nobody
# enumerated and one that grows with the project's IAM rather than with any
# decision about this cluster.
_AUTHENTICATED_SUBJECT = "system:authenticated"

_IMPACT_ANONYMOUS_RBAC_ANONYMOUS = (
    "This binding grants a caller that presented no credential at all. Every "
    "other control in the cluster -- Workload Identity, ServiceAccount tokens, "
    "authorized networks, the whole of RBAC -- is written on the assumption "
    "that reaching the API server means holding an identity, and this binding "
    "removes that assumption for the resources it names. If the control plane "
    "has a public endpoint, the caller is the internet."
)

_IMPACT_ANONYMOUS_RBAC_AUTHENTICATED = (
    "This binding grants write access to `system:authenticated`, which is not "
    "a team -- it is every principal the API server accepts a credential from. "
    "On GKE that is every Google account holding `container.clusters.get` on "
    "the project, so the set of people who can write to this cluster is "
    "whatever the project's IAM happens to say today, and it widens every time "
    "someone is added to the project for an unrelated reason."
)


def _rules_of_role_ref(context: dict, role_ref: dict, namespace: str) -> list[dict] | None:
    """The rules of the Role or ClusterRole a binding names, or `None` if absent.

    `None` and `[]` are different answers and the caller has to tell them
    apart: an empty rule list is a role that grants nothing, while a missing
    role is a binding pointing at an object that does not exist, which grants
    nothing today and everything the role is written to grant on the day
    someone creates it.
    """
    kind, name = role_ref.get("kind"), role_ref.get("name")
    for role in context.get("roles") or []:
        meta = role.get("metadata") or {}
        if role.get("kind") != kind or meta.get("name") != name:
            continue
        # A ClusterRole is cluster-scoped, so the binding's namespace does not
        # narrow which one it means; a Role only resolves inside its own.
        if kind == "Role" and meta.get("namespace", "") != namespace:
            continue
        return role.get("rules") or []
    return None


def _rules_write(rules: list[dict]) -> list[str]:
    """The escalating verbs these rules carry, sorted, or `[]` for read-only."""
    return sorted({verb for rule in rules for verb in (rule.get("verbs") or []) if verb in _ESCALATING_VERBS or verb == "*"})


def check_anonymous_rbac_binding(context: dict) -> list[dict]:
    """§2.16 -- a binding whose subject is everyone rather than someone.

    §2.4 and §2.5 both run every subject through `_is_non_system_subject`,
    which returns `False` for any `User` or `Group` whose name starts
    `system:`. That exclusion is load-bearing and correct for what it was
    written for: GKE ships dozens of `system:`-prefixed bootstrap bindings on
    every cluster, and reporting them was the flood §1 exists to avoid. What
    it also swallows is the three `system:` subjects that are not a component
    identity but a wildcard over callers -- `system:anonymous`,
    `system:unauthenticated`, and `system:authenticated`. So the single worst
    RBAC mistake anyone makes is the one shape those two checks are
    structurally unable to report, and it stays invisible however severe the
    role behind it is: a ClusterRoleBinding of `cluster-admin` to
    `system:anonymous` produces nothing from either.

    It is a mistake people make on purpose. `kubectl create clusterrolebinding
    <x> --clusterrole=cluster-admin --user=system:anonymous` is the accepted
    answer to a dozen "my dashboard says forbidden" questions, and
    `--group=system:authenticated` is how a team gets `edit` without anyone
    writing down who is on it.

    The two arms are graded differently because they fail differently. For an
    anonymous subject any role at all is the finding, resolved or not: `view`
    bound to `system:anonymous` hands every ConfigMap and every pod spec in
    the cluster to whoever can reach the endpoint. For
    `system:authenticated` the role decides, and a read-only one is not
    reported at all -- `view` bound to that group is how a great many
    organisations make a cluster browsable, `_sa_groups_granted` already
    treats it as a legitimate posture rather than a defect, and a check that
    called it critical here would be arguing with the same file.

    The baseline exclusions differ by arm, because Kubernetes binds different
    roles to each. `_BASELINE_AUTHENTICATED_ROLES` is the four it binds to
    `system:authenticated` (or to the ServiceAccount groups) on every cluster;
    `_BASELINE_ANONYMOUS_ROLES` is the one it binds to
    `system:unauthenticated`. `system:discovery` and `system:basic-user` lost
    that subject in Kubernetes 1.14, so binding either to an anonymous subject
    is how anonymous discovery is re-enabled -- a choice someone made, and one
    this check reports.

    One candidate per binding, naming every universal subject it matched: the
    finding id is (check, cluster, namespace, object), and a hit per subject
    would hand `finish` two candidates with one identity. An anonymous subject
    sets the impact whenever one is present, since it is the wider grant.

    `system:serviceaccounts` is deliberately absent. It is universal in the
    same way, but it names workloads rather than people, the remediation is a
    different one, and §2.14 already reasons about it where it matters.
    """
    hits = []
    for binding in (context.get("clusterrolebindings") or []) + (context.get("rolebindings") or []):
        role_ref = binding.get("roleRef") or {}
        role_name = role_ref.get("name")
        meta = binding.get("metadata") or {}
        namespace = meta.get("namespace", "")
        kind = binding.get("kind") or ""
        ref = f"{role_ref.get('kind', '')}/{role_ref.get('name', '')}"
        anonymous, authenticated = [], []
        for subject in binding.get("subjects") or []:
            if subject.get("kind") not in ("User", "Group"):
                continue
            name = subject.get("name") or ""
            if name in _ANONYMOUS_SUBJECTS and role_name not in _BASELINE_ANONYMOUS_ROLES:
                anonymous.append(f"{subject.get('kind')}/{name}")
            elif name == _AUTHENTICATED_SUBJECT and role_name not in _BASELINE_AUTHENTICATED_ROLES:
                authenticated.append(f"Group/{name}")
        writes: list[str] = []
        if authenticated:
            rules = _rules_of_role_ref(context, role_ref, namespace)
            # A role absent from the dump grants nothing and has no verb list
            # to build the sentence from, and a read-only one is a legitimate
            # posture (§2.16); either way the authenticated subject drops out.
            writes = _rules_write(rules) if rules is not None else []
            if not writes:
                authenticated = []
        if not anonymous and not authenticated:
            continue
        excerpt = f"{kind}/{meta.get('name', '')} -> {ref} for {', '.join(anonymous + authenticated)}"
        if authenticated:
            excerpt += f"; {ref} grants {', '.join(writes)}"
        hits.append(
            {
                "namespace": namespace,
                "object": f"{kind}/{meta.get('name', '')}",
                "excerpt": excerpt,
                "impact": _IMPACT_ANONYMOUS_RBAC_ANONYMOUS if anonymous else _IMPACT_ANONYMOUS_RBAC_AUTHENTICATED,
                "reconciler": reconciler_of(meta),
                "release": release_of(meta),
            }
        )
    return hits


# Cilium writes the pod's namespace into the endpoint's label set under this
# key, and Dataplane V2's ClusterNetworkPolicy selects on it. `k8s:` is the
# source prefix Cilium adds to labels it learned from Kubernetes; a policy may
# be written with or without it.
_CILIUM_NAMESPACE_LABELS = ("k8s:io.kubernetes.pod.namespace", "io.kubernetes.pod.namespace")


def _ccnp_coverage(policies: list[dict]) -> tuple[bool, set[str]]:
    """Which namespaces the cluster-wide policies actually put behind ingress
    enforcement: `(covers_every_namespace, the_named_ones)`.

    This used to be `bool(policies)` — one ClusterNetworkPolicy anywhere in the
    cluster suppressed §2.6 in every namespace. GKE installs Dataplane V2
    policies of its own, and a single one selecting one workload's labels was
    enough to make the whole cluster report no default-allow namespaces, which
    is the same silence a cluster with real coverage produces.

    Two independent questions, and a policy has to answer both to suppress a
    namespace. *Which endpoints* — an empty `endpointSelector` matches every
    endpoint in the cluster, and a selector naming the namespace label covers
    that namespace; any other selector picks out particular pods and leaves the
    namespace's posture unchanged. *Enforcing what* — Cilium isolates ingress
    only for a policy that carries an `ingress` section, so an egress-only
    cluster policy suppresses nothing here: §2.6 is about who can reach these
    pods.
    """
    covers_all = False
    covered: set[str] = set()
    for policy in policies:
        spec = policy.get("spec") or {}
        specs = [spec] + [s for s in (policy.get("specs") or []) if isinstance(s, dict)]
        for one in specs:
            if not one.get("ingress") and not one.get("ingressDeny"):
                continue
            selector = one.get("endpointSelector")
            if not selector:
                covers_all = True
                continue
            labels = (selector.get("matchLabels") or {}) if isinstance(selector, dict) else {}
            for key in _CILIUM_NAMESPACE_LABELS:
                if labels.get(key):
                    covered.add(labels[key])
    return covers_all, covered


# §2.6's partial-coverage arm. The table's sentence -- "every pod in this
# namespace accepts traffic from every pod in the cluster" -- is true of the
# other two arms and flatly false of this one, where the policies that exist
# are working and cover everything except the pods named in the excerpt. The
# SOP forbids that sentence here by name, and the run published it anyway over
# `kubeagents-system`, whose four policies police five of six pods; the title
# took the right branch while the Impact told the operator their policies do
# nothing. Written on the hit so `adopt_arm_impact` can hold it.
_IMPACT_NETPOL_PARTIAL = (
    "The named workloads accept traffic from every pod in the cluster, while "
    "the rest of the namespace is policed -- so the gap is invisible in a "
    "policy review that only asks whether this namespace has NetworkPolicies."
)


def check_netpol_missing(context: dict) -> list[dict]:
    """§2.6's exposure test is `kubectl get pods -n <ns> | wc -l`, so it reads
    `pod_namespaces` — every namespace holding a live Pod — and not the audited
    workload set.

    Those are different sets, and the difference swallowed the check.
    `normalize_compliance_workloads` drops any Pod carrying `ownerReferences`,
    because compliance audits the controller and never the pod, whose name
    carries a random suffix. Every pod a Deployment, StatefulSet, DaemonSet or
    Job creates carries one — so counting Pods in that set returned zero for
    the ordinary namespace, which then read as "zero workloads, no exposure,
    pure churn" and was skipped. The namespace the check exists to find, one
    running a Deployment with no NetworkPolicy, was the exact case it could not
    report: on this 16-cluster fleet `cert-manager` had three pods and no
    policy across every run, and the stream flagged nothing.
    """
    hits = []
    netpols_by_ns: dict[str, list[dict]] = {}
    for netpol in context.get("networkpolicies") or []:
        ns = (netpol.get("metadata") or {}).get("namespace", "")
        netpols_by_ns.setdefault(ns, []).append(netpol)
    pod_namespaces = context["pod_namespaces"]
    # §2.6's Do-NOT-flag case: a namespace already covered by a Dataplane V2
    # ClusterNetworkPolicy is not a default-allow posture just because it has
    # no *namespaced* NetworkPolicy of its own.
    ccnp_all, ccnp_covered = _ccnp_coverage(context.get("cluster_network_policies") or [])

    for ns_item in context.get("namespaces") or []:
        ns = (ns_item.get("metadata") or {}).get("name", "")
        if _is_system_namespace(ns):
            continue
        policies = netpols_by_ns.get(ns, [])
        if not policies:
            if ns not in pod_namespaces:
                continue  # no pods, no exposure, pure churn
            if ccnp_all or ns in ccnp_covered:
                continue
            # Namespace-scoped, because `adopt_collector_evidence` puts this
            # string under a cluster-wide `kubectl get netpol -A` command. Bare
            # "zero NetworkPolicies" then reads as a claim about the cluster,
            # and on kube-agents-host -- which has eleven, none of them in
            # cert-manager -- the one false line on an otherwise accurate
            # finding is what gets the audit switched off.
            # A plain count, with no verdict attached to it. The tally includes
            # policies in system namespaces and allow-all ones, so "and this
            # namespace is the gap" would be reading more into the number than
            # it carries; the reader needs to know the cluster is not
            # uniformly unpoliced, which the count alone says.
            elsewhere = sum(len(v) for k, v in netpols_by_ns.items() if k != ns)
            excerpt = "no NetworkPolicy in this namespace"
            if elsewhere:
                excerpt += f"; {elsewhere} in other namespaces of this cluster"
            hits.append({"namespace": ns, "object": f"Namespace/{ns}", "excerpt": excerpt, "severity": "major"})
            continue
        # An empty ingress rule on a policy that enforces Ingress, and nothing
        # else. `podSelector: {}` with no `policyTypes` and no rules derives to
        # `[Ingress]` with nothing admitted -- the deny-all §2.6 remediates
        # *to* -- so reading an absent `policyTypes` as allow-all flagged the fix.
        allow_all = [
            p
            for p in policies
            if (p.get("spec") or {}).get("podSelector") == {}
            and _enforces_ingress(p)
            and any(rule == {} for rule in (p.get("spec") or {}).get("ingress") or [])
        ]
        # Any allow-all, not only an all-allow-all namespace: policies are
        # additive, so one `ingress: [{}]` over every pod admits all traffic
        # to every pod whatever the narrower policies beside it say.
        if allow_all:
            for p in allow_all:
                hits.append(
                    {
                        "namespace": ns,
                        "object": f"NetworkPolicy/{(p.get('metadata') or {}).get('name', '')}",
                        "excerpt": "allow-all (podSelector: {} with an empty ingress rule)",
                        "severity": "minor",
                    }
                )
            continue
        # A namespace holding policies is not therefore a covered namespace.
        # NetworkPolicy is additive and pod-scoped: a pod no policy selects has
        # no policy applied to it and stays reachable from anywhere, exactly as
        # if the namespace had none. The branch above answers "does this
        # namespace have a policy"; only the pod labels answer "does this pod
        # have one", and the second is the question the exposure turns on.
        # On the reference fleet that is `kubeagents-system`, whose four
        # policies name four workloads by label and leave the operator's own
        # manager pod -- 8081 and a 10250 webhook -- selected by none.
        if ccnp_all or ns in ccnp_covered:
            continue
        uncovered = _pods_no_policy_selects(context.get("pods") or [], ns, policies)
        if uncovered:
            # Namespace-scoped, not pod-scoped: a pod name carries a
            # ReplicaSet hash and a random suffix, so keying the ledger on one
            # would resolve and re-raise this finding on every rollout. The
            # workload names go in the excerpt, where churn costs nothing.
            live = sum(1 for p in context.get("pods") or [] if p.get("ns") == ns and _is_live_pod(p))
            # Only the policies that police ingress cover anything here; an
            # Egress-only policy selects pods without restricting who reaches them.
            enforcing = sum(1 for p in policies if _enforces_ingress(p))
            names = sorted({_pod_workload_ref(p, context.get("workloads") or []) for p in uncovered})
            hits.append(
                {
                    "namespace": ns,
                    "object": f"Namespace/{ns}",
                    "excerpt": (
                        f"{len(uncovered)} of {live} pods here are selected by no NetworkPolicy that "
                        f"enforces Ingress: {', '.join(names)}; {enforcing} Ingress "
                        f"{'policy' if enforcing == 1 else 'policies'} in this namespace cover the rest"
                    ),
                    "severity": "major",
                    "impact": _IMPACT_NETPOL_PARTIAL,
                }
            )
    return hits


def _is_live_pod(pod: dict) -> bool:
    """A finished Job pod is not an exposure and its name is pure churn."""
    return str(pod.get("phase") or "") not in ("Succeeded", "Failed")


# A ReplicaSet is always named `<deployment>-<pod-template-hash>` and a
# CronJob's Job `<cronjob>-<timestamp>`. Neither intermediate object is in
# `COMPLIANCE_DUMP_KINDS`, so the parent is derived by dropping that last
# segment and then confirmed against the dump rather than assumed.
_OWNER_PARENT_KIND = {"ReplicaSet": "Deployment", "Job": "CronJob"}


def _controller_refs(meta: dict) -> list[dict]:
    """Owner references, the controlling one first.

    A pod may carry several; at most one is the controller, and that is the
    object that recreates it and the object whose template a fix has to edit.
    The rest are kept behind it rather than dropped, because an owner set with
    no `controller: true` at all is legal and still names something better than
    the pod.
    """
    refs = (meta.get("ownerReferences") or [])
    return [
        {"kind": o.get("kind", ""), "name": o.get("name", "")}
        for o in sorted(refs, key=lambda o: not o.get("controller"))
    ]


def _pod_workload_ref(pod: dict, workloads: list[dict]) -> str:
    """`Kind/name` of an object the reader can actually `kubectl get`.

    Not the `app.kubernetes.io/name` label, which this used to return. A label
    value is not a name and nothing requires it to match one: on this fleet
    `Deployment/kube-agents-controller-manager` carries
    `app.kubernetes.io/name: kube-agents-operator`, and the 2026-09-05
    compliance report named `kube-agents-operator` as the uncovered workload in
    `kubeagents-system`. No object anywhere in the cluster has that name, so
    the one actionable string in the finding resolved to `NotFound`.

    Nor the pod's own name, which the label was reached for to avoid -- it
    carries a ReplicaSet hash and a random suffix, and this excerpt is
    rewritten on every rollout if it moves. `ownerReferences` gives both
    properties at once: the controller behind the pod is a real object and its
    name is stable across restarts. Where the walk cannot land on something the
    dump confirms, it degrades to the pod, which is at least a thing that
    exists.
    """
    ns = pod.get("ns", "")
    declared = {(w.get("kind"), w.get("ns"), w.get("name")) for w in workloads}
    for owner in pod.get("owners") or []:
        kind, name = owner.get("kind", ""), owner.get("name", "")
        if not kind or not name:
            continue
        parent = _OWNER_PARENT_KIND.get(kind)
        if parent and (parent, ns, name.rpartition("-")[0]) in declared:
            return f"{parent}/{name.rpartition('-')[0]}"
        return f"{kind}/{name}"
    return f"Pod/{pod.get('name') or ''}"


def _enforces_ingress(policy: dict) -> bool:
    """`policyTypes` is optional. Kubernetes derives it from which rule blocks
    are present, and an empty spec derives to `["Ingress"]` -- a deny-all. So
    absent means ingress is enforced unless the policy is egress-only.
    """
    spec = policy.get("spec") or {}
    declared = spec.get("policyTypes")
    if declared:
        return "Ingress" in declared
    return not (spec.get("egress") and "ingress" not in spec)


def _pods_no_policy_selects(pods: list[dict], ns: str, policies: list[dict]) -> list[dict]:
    ingress_policies = [p for p in policies if _enforces_ingress(p)]
    out = []
    for pod in pods:
        if pod.get("ns") != ns or not _is_live_pod(pod):
            continue
        labels = pod.get("labels") or {}
        if not any(selector_matches((p.get("spec") or {}).get("podSelector") or {}, labels) for p in ingress_policies):
            out.append(pod)
    return out


def check_default_sa_automount(context: dict) -> list[dict]:
    unsafe_sa_namespaces = set()
    for sa in context.get("serviceaccounts") or []:
        meta = sa.get("metadata") or {}
        if meta.get("name") != DEFAULT_SERVICE_ACCOUNT:
            continue
        if sa.get("automountServiceAccountToken") is not False:
            unsafe_sa_namespaces.add(meta.get("namespace", ""))

    hits = []
    for wl in context.get("workloads") or []:
        sa_name = wl["spec"].get("serviceAccountName") or wl["spec"].get("serviceAccount") or "default"
        if sa_name != "default":
            continue
        if wl["ns"] not in unsafe_sa_namespaces:
            continue
        if wl["spec"].get("automountServiceAccountToken") is False:
            continue
        hits.append(
            {
                "namespace": wl["ns"],
                "object": f"{wl['kind']}/{wl['name']}",
                "excerpt": "resolves to the default ServiceAccount with automount not disabled",
                # A cluster check that nonetheless names a workload: it walks
                # `context["workloads"]` itself rather than being called once
                # per workload, because the ServiceAccount half of the test is
                # a namespace fact. The record carries the same field the
                # workload checks read.
                "reconciler": wl.get("reconciler"),
                "release": wl.get("release"),
            }
        )
    return hits


# The four `system:` ClusterRoles Kubernetes binds to a group every principal
# is in, on every cluster, out of the box. They are what "this token grants
# nothing" means: API discovery, `/version`, `/healthz`, a self-review of one's
# own permissions, and the OIDC issuer document. §2.14 has to know them by name
# because it reads group-subject bindings to decide whether an unbound
# ServiceAccount is really unbound -- and if these four counted, no
# ServiceAccount on any cluster would ever qualify.
_UNIVERSAL_SA_GROUPS = frozenset({"system:authenticated", "system:serviceaccounts"})
_SA_NAMESPACE_GROUP_PREFIX = "system:serviceaccounts:"
# Vault Agent's injector adds its sidecar on this pod annotation, and the
# sidecar logs in to Vault's `kubernetes` auth method with the default mounted
# token. Vault reviews that token with its own identity, so the ServiceAccount
# needs no binding in the cluster and §2.14's unbound test says nothing about
# whether the token is used. The values are exactly the spellings of true the
# injector's `strconv.ParseBool` accepts; anything else gets no sidecar there,
# so it is not skipped here either.
_VAULT_AGENT_INJECT_ANNOTATION = "vault.hashicorp.com/agent-inject"
_VAULT_AGENT_INJECT_TRUE = frozenset({"1", "t", "T", "true", "True", "TRUE"})
_BASELINE_AUTHENTICATED_ROLES = frozenset({
    "system:basic-user",
    "system:discovery",
    "system:public-info-viewer",
    "system:service-account-issuer-discovery",
})
# The one role Kubernetes binds to `system:unauthenticated` out of the box.
# The other three above are bound to authenticated callers or ServiceAccounts
# only, so an anonymous binding of any of them was written by someone.
_BASELINE_ANONYMOUS_ROLES = frozenset({"system:public-info-viewer"})

_IMPACT_UNBOUND_SA_AUTOMOUNT = (
    "This workload mounts a live API-server credential into every container, "
    "and no RoleBinding or ClusterRoleBinding grants the ServiceAccount it "
    "belongs to anything. An attacker reaching the filesystem -- through a "
    "path-traversal read, an SSRF the container proxies, a leaked log of the "
    "token, or code execution -- gets an authenticated identity on the API "
    "server for free, and authenticated is the boundary most of a cluster's "
    "defences are written against. No grant in this cluster stands behind the "
    "token, so the workload loses nothing here by turning the mount off."
)


def _sa_groups_granted(context: dict) -> tuple[bool, set[str]]:
    """Which ServiceAccounts this cluster grants something by group.

    §2.14's whole claim is "no binding grants this ServiceAccount anything",
    and a binding whose subject is a *group* covering service accounts breaks
    it without naming any of them. Some organisations do exactly that --
    `view` bound to `system:authenticated` is a common way to make a cluster
    browsable -- and on such a cluster every finding this check could emit
    would be false.

    Returns `(universal, namespaces)`: `universal` when a group in
    `_UNIVERSAL_SA_GROUPS` is granted anything, which covers every
    ServiceAccount; `namespaces` for each `system:serviceaccounts:<ns>` group
    granted anything, which covers only the ServiceAccounts in `<ns>` -- one
    namespace's grant must not silence the check for the rest of the cluster.

    The four roles in `_BASELINE_AUTHENTICATED_ROLES` are excluded because
    Kubernetes ships them bound this way on every cluster.
    """
    universal = False
    namespaces: set[str] = set()
    for binding in (context.get("clusterrolebindings") or []) + (context.get("rolebindings") or []):
        if (binding.get("roleRef") or {}).get("name") in _BASELINE_AUTHENTICATED_ROLES:
            continue
        for subject in binding.get("subjects") or []:
            if subject.get("kind") != "Group":
                continue
            name = subject.get("name") or ""
            if name in _UNIVERSAL_SA_GROUPS:
                universal = True
            elif name.startswith(_SA_NAMESPACE_GROUP_PREFIX):
                namespaces.add(name[len(_SA_NAMESPACE_GROUP_PREFIX):])
    return universal, namespaces


def check_unbound_sa_automount(context: dict) -> list[dict]:
    """§2.14 -- a token mounted for a ServiceAccount nothing has granted.

    §2.7 covers the `default` ServiceAccount and stops there, which was the
    right scope when it was written and is no longer where the shape lives. A
    chart that follows current practice creates a ServiceAccount of its own,
    so `serviceAccountName` is set and §2.7 goes quiet -- and then most charts
    bind that ServiceAccount to nothing, because the workload never calls the
    API server, and leave `automountServiceAccountToken` alone, because it
    defaults to on. The result is a credential in the container filesystem
    that exists for no reason, on a workload §2.7 by construction cannot see.

    The unbound test is what makes this safe to open a pull request against.
    "This workload does not use its token" is not readable from a manifest and
    a finding asserting it is one an owner can refute from memory, which is
    why §2.7's impact sentence deliberately does not claim it. "Nothing has
    granted this ServiceAccount anything" is a different statement: it is
    provable from the RBAC dump, and it means the API server gives the token
    nothing. It does not prove that nothing else reads the token: an external
    verifier that reviews it with its own identity -- Vault's `kubernetes`
    auth method, fed by the Vault Agent injector's sidecar -- needs no binding
    here. The injector announces itself on the pod template, so that case is
    skipped; any other such consumer is what the remediation's `risk` tells
    the reviewer to rule out.

    An explicit projected `serviceAccountToken` volume is unaffected by
    `automountServiceAccountToken` and is therefore not an exclusion: a
    workload federating to an external audience through its own declared
    volume keeps working across this fix (the Vault Agent sidecar reads the
    default mount, hence the skip above). GKE Workload Identity likewise reaches Google Cloud
    through the metadata server rather than this token.
    """
    universal, granted_namespaces = _sa_groups_granted(context)
    if universal:
        return []
    # A RoleBinding may leave a ServiceAccount subject's `namespace` out, and
    # the API server then reads it as the binding's own. Keyed on "" instead,
    # that grant matched no workload and the ServiceAccount it binds was
    # reported as bound to nothing.
    bound = set()
    for binding in (context.get("clusterrolebindings") or []) + (context.get("rolebindings") or []):
        binding_ns = (binding.get("metadata") or {}).get("namespace") or ""
        for subject in binding.get("subjects") or []:
            if subject.get("kind") == "ServiceAccount":
                bound.add((subject.get("namespace") or binding_ns, subject.get("name") or ""))
    # Only the SAs that exist. A `serviceAccountName` naming an absent object
    # mounts no token at all -- the pod does not start -- so it is not this
    # check's finding, and reporting it as one would send a pull request to
    # turn off a mount that is not happening.
    declared = {
        ((sa.get("metadata") or {}).get("namespace", ""), (sa.get("metadata") or {}).get("name", "")): sa
        for sa in context.get("serviceaccounts") or []
    }
    hits = []
    for wl in context.get("workloads") or []:
        sa_name = wl["spec"].get("serviceAccountName") or wl["spec"].get("serviceAccount") or "default"
        if sa_name == "default":
            continue  # §2.7's finding; never emit both on one object
        key = (wl["ns"], sa_name)
        sa = declared.get(key)
        if sa is None:
            continue
        if wl["spec"].get("automountServiceAccountToken") is False:
            continue
        if sa.get("automountServiceAccountToken") is False:
            continue
        if key in bound or wl["ns"] in granted_namespaces:
            continue
        inject = str((wl.get("pod_annotations") or {}).get(_VAULT_AGENT_INJECT_ANNOTATION, ""))
        if inject in _VAULT_AGENT_INJECT_TRUE:
            continue
        hits.append(
            {
                "namespace": wl["ns"],
                "object": f"{wl['kind']}/{wl['name']}",
                # Name the ServiceAccount. The fix goes on the workload, but a
                # reviewer's first question is which identity is unbound, and
                # the object name does not answer it -- charts routinely name
                # the ServiceAccount something other than the Deployment.
                "excerpt": (
                    f"serviceAccountName={sa_name}, automountServiceAccountToken unset on both "
                    f"the pod and the ServiceAccount; no RoleBinding or ClusterRoleBinding "
                    f"names ServiceAccount {wl['ns']}/{sa_name} as a subject"
                ),
                # Same reason as §2.7's: a cluster-kind check that walks the
                # workload list itself still owes each hit the workload's
                # reconciler, or the finding cannot say a hand-applied fix
                # would be reverted.
                "reconciler": wl.get("reconciler"),
                "release": wl.get("release"),
            }
        )
    return hits


def _cluster_object(context: dict) -> str:
    """`Cluster/<name>` for a finding whose object is the cluster itself.

    A bare `Cluster` was what these two checks emitted until 2026-08-29, and it
    is not a name: the finding id derives from `object`, so the compliance
    stream reported the same four public control planes as resolved and
    re-opened them as new the day the collector started supplying the string.
    `audit_report.validate_findings` now refuses a bare kind outright; this is
    the other half, so the collector never asks for the refusal.
    """
    name = str(context.get("cluster_name") or "").strip()
    if not name:
        raise GateFailure(
            "the collector context carries no cluster_name, so a cluster-scoped "
            "finding cannot name its object"
        )
    return f"Cluster/{name}"


def check_workload_identity_off(context: dict) -> list[dict]:
    describe = context.get("cluster_describe") or {}
    pool = ((describe.get("workloadIdentityConfig") or {}).get("workloadPool") or "").strip()
    if pool:
        return []
    return [{"namespace": "", "object": _cluster_object(context), "excerpt": "workloadIdentityConfig.workloadPool is empty"}]


def check_legacy_metadata(context: dict) -> list[dict]:
    hits = []
    for pool in context.get("node_pools") or []:
        mode = ((pool.get("config") or {}).get("workloadMetadataConfig") or {}).get("mode") or ""
        if mode == "GKE_METADATA":
            continue
        hits.append(
            {
                "namespace": "",
                "object": f"NodePool/{pool.get('name', '')}",
                "excerpt": f"workloadMetadataConfig.mode={mode or '(empty)'}",
            }
        )
    return hits


def _is_default_route(cidr: str) -> bool:
    """Whether this CIDR admits every address of its family.

    A prefix length of zero rather than a string match, so `::/0` is caught
    beside `0.0.0.0/0`. An allowlist is only worth the name if something is
    outside it, and a dual-stack cluster can write the v6 default route into a
    field where only the v4 one was ever recognised.
    """
    try:
        return ipaddress.ip_network(cidr.strip(), strict=False).prefixlen == 0
    except ValueError:
        return False


def _grants_google_cloud_access(cfg: dict) -> bool:
    """Whether this authorized-networks config excepts Google Cloud's own public IPs.

    `gcpPublicCidrsAccessEnabled` (the `--enable-google-cloud-access` flag)
    admits every external address Google Cloud owns, on top of whatever
    `cidrBlocks` says. Absent reads as granted, because the API default is on:
    `gcloud container clusters update --enable-master-authorized-networks
    --master-authorized-networks=<CIDR>` leaves the field `true` on a cluster
    that never mentioned it, which was confirmed against a live cluster rather
    than inferred. Reading absent as `false` would call that cluster narrowed.
    """
    return cfg.get("gcpPublicCidrsAccessEnabled") is not False


def _has_restrictive_authorized_networks(describe: dict) -> bool:
    """Whether some authorized-networks surface narrows control-plane access.

    GKE carries the config on either `masterAuthorizedNetworksConfig` or
    `controlPlaneEndpointsConfig.ipEndpointsConfig.authorizedNetworksConfig` and
    rejects a cluster that sets both, so reading one field alone calls a cluster
    restricted through the other wide open. `cidrBlocks` holds CidrBlock objects
    (`{displayName, cidrBlock}`), never bare strings, so the allow-all entry is
    matched on the field rather than by membership in the list.

    Google Cloud access disqualifies a config however narrow its `cidrBlocks`
    are. The addresses it admits are not Google's own infrastructure -- they are
    the external IPs of every Compute Engine VM, Cloud Run service and Cloud
    Function in every Google Cloud project, so an attacker rents one and is
    inside the allowlist. That is not a narrowing worth suppressing a
    control-plane-exposure finding over, and suppressing on it was the worse
    half of a pair: the published remediation for that finding did not turn the
    grant off, so acting on the finding cleared it while leaving the endpoint
    reachable from anywhere a VM can be started.

    An enabled config with no blocks at all is caught by the same rule rather
    than by one of its own. It used to be read as the strict end of the setting
    -- shut to everything but "Google's own access" -- which had the sense of
    the field backwards.
    """
    ip_cfg = (describe.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}
    for cfg in (describe.get("masterAuthorizedNetworksConfig"), ip_cfg.get("authorizedNetworksConfig")):
        cfg = cfg or {}
        if cfg.get("enabled") is not True or _grants_google_cloud_access(cfg):
            continue
        blocks = [b.get("cidrBlock") if isinstance(b, dict) else b for b in (cfg.get("cidrBlocks") or [])]
        if not any(_is_default_route(str(b or "")) for b in blocks):
            return True
    return False


def _json_scalar(value: object) -> str:
    """A field's value the way the `gcloud … --format=json` output spelled it.

    An excerpt quotes a JSON read, so `true` belongs there rather than Python's
    `True`, and a field GKE omitted has to read as omitted rather than as the
    `False` a `.get()` default would put in its place -- absent and `false` are
    different states on every field this renders.
    """
    if value is None:
        return "absent"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _authorized_networks_excerpt(describe: dict) -> str:
    """Both authorized-networks surfaces, as read, for the finding's excerpt.

    Named even when empty. GKE carries the config on one surface or the other
    and returns the unused one as `{}`, so a reader who sees only the populated
    field cannot tell a cluster that left the feature off from one this check
    forgot to look at. `gcpPublicCidrsAccessEnabled` is rendered where GKE set
    it because it is the field that distinguishes clusters this check otherwise
    grades identically -- it is what GKE writes into an otherwise-empty
    `masterAuthorizedNetworksConfig` on a cluster that never enabled the
    feature.

    Which way that field cuts depends on `enabled`, and the excerpt says which,
    because the reader's next move differs. With `enabled` off the grant is
    inert: every address already reaches the endpoint, and excepting some of
    them from an allowlist nobody is applying adds nothing. With `enabled` on
    it is the finding -- the `cidrBlocks` beside it can be a single `/32` and
    the endpoint still answers every Compute Engine VM on Google Cloud. That
    case is annotated even where GKE returned no field at all, because the
    default is on and a reader who sees a tight `cidrBlocks` with no mention of
    the grant concludes the opposite of the truth.
    """
    ip_cfg = (describe.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}
    parts: list[str] = []
    for label, cfg in (
        ("masterAuthorizedNetworksConfig", describe.get("masterAuthorizedNetworksConfig")),
        ("ipEndpointsConfig.authorizedNetworksConfig", ip_cfg.get("authorizedNetworksConfig")),
    ):
        cfg = cfg or {}
        blocks = [str(b.get("cidrBlock") if isinstance(b, dict) else b) for b in (cfg.get("cidrBlocks") or [])]
        part = f"{label}.enabled={_json_scalar(cfg.get('enabled'))}, cidrBlocks=[{','.join(blocks)}]"
        live = cfg.get("enabled") is True and _grants_google_cloud_access(cfg)
        if cfg.get("gcpPublicCidrsAccessEnabled") is not None or live:
            part += f", gcpPublicCidrsAccessEnabled={_json_scalar(cfg.get('gcpPublicCidrsAccessEnabled'))}"
            if live:
                part += " (admits every Google Cloud external IP on top of cidrBlocks)"
            elif cfg.get("enabled") is not True:
                part += " (inert: authorized networks not enabled)"
        parts.append(part)
    return "; ".join(parts)


def _external_control_plane_paths(describe: dict) -> list[str]:
    """Every way the control plane answers from outside the VPC, as read.

    Two independent endpoints, and authorized networks gates only one of them.
    The IP endpoint is the one this check was written for. The DNS endpoint is
    a separate address (`gke-<hash>.<region>.gke.goog`) that GKE serves when
    `dnsEndpointConfig.allowExternalTraffic` is set, and no IP allowlist
    applies to it at all -- it is gated by IAM alone, so enabling authorized
    networks does not close it and a reader who acts on this finding would be
    left with the cluster still reachable.

    `ipEndpointsConfig.enabled` is the master switch under which
    `enablePublicEndpoint` sits. A cluster created with `--no-enable-ip-access`
    serves no IP endpoint at all, and reading `enablePublicEndpoint` alone
    calls it internet-reachable over an address it does not have. That
    combination does not exist on this fleet, so this is a false positive the
    check has not made yet rather than one it made.
    """
    endpoints = describe.get("controlPlaneEndpointsConfig") or {}
    ip_cfg = endpoints.get("ipEndpointsConfig") or {}
    private_cfg = describe.get("privateClusterConfig") or {}
    paths = []
    if ip_cfg.get("enabled") is False:
        pass  # No IP endpoint at all; `enablePublicEndpoint` below it is moot.
    elif ip_cfg.get("enablePublicEndpoint") is not None:
        if ip_cfg.get("enablePublicEndpoint") is True:
            paths.append(
                "controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint="
                f"{_json_scalar(ip_cfg.get('enablePublicEndpoint'))}"
            )
    elif private_cfg.get("enablePrivateEndpoint") is not True:
        # The legacy inversion, read only where GKE returns no current field.
        paths.append(
            "privateClusterConfig.enablePrivateEndpoint="
            f"{_json_scalar(private_cfg.get('enablePrivateEndpoint'))}"
        )
    dns_cfg = endpoints.get("dnsEndpointConfig") or {}
    if dns_cfg.get("allowExternalTraffic") is True:
        paths.append(
            "controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic=true "
            "(DNS endpoint, not gated by authorized networks)"
        )
    return paths


def _allowlisted_but_for_google_cloud(describe: dict) -> bool:
    """Whether an allowlist is configured and Google Cloud access is what defeats it.

    Distinguishes the cluster that never enabled authorized networks from the
    one that enabled them and left the grant on. Both reach the internet, but
    only the second has an operator who believes the endpoint is closed, and
    the sentence each needs is different.
    """
    ip_cfg = (describe.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}
    for cfg in (describe.get("masterAuthorizedNetworksConfig"), ip_cfg.get("authorizedNetworksConfig")):
        cfg = cfg or {}
        if cfg.get("enabled") is not True or not _grants_google_cloud_access(cfg):
            continue
        blocks = [b.get("cidrBlock") if isinstance(b, dict) else b for b in (cfg.get("cidrBlocks") or [])]
        if not any(_is_default_route(str(b or "")) for b in blocks):
            return True
    return False


_IMPACT_PUBLIC_IP_ENDPOINT = (
    "The cluster's API server accepts connections from any address on the "
    "internet; credential compromise or an API-server CVE is directly "
    "exploitable from outside the network."
)
# Separated from the sentence above because the operator here has already done
# the thing that sentence would prompt -- turned authorized networks on -- and
# telling them the endpoint answers the whole internet reads as a check that
# did not notice. The exposure is narrower and the remaining work is a
# different flag, so the finding has to say which.
_IMPACT_GOOGLE_CLOUD_ACCESS = (
    "Authorized networks is enabled on the cluster's public IP endpoint, but "
    "gcpPublicCidrsAccessEnabled excepts Google Cloud's external addresses "
    "from the allowlist — those of every Compute Engine VM, Cloud Run service "
    "and Cloud Function in every Google Cloud project, not this project's "
    "alone. An attacker starts a VM anywhere on Google Cloud and is inside the "
    "allowlist, so the allowlist narrows reachability to whoever can create a "
    "Google Cloud resource rather than closing it."
)
# The two endpoints are not the same exposure, and saying so overstates the
# one that is left. Reaching the DNS endpoint costs an attacker a Google
# identity carrying `container.clusters.connect` before a single byte reaches
# the API server, where the IP endpoint puts the server itself on the internet.
# Still a finding: authorized networks is the control an operator reaches for
# here and it does not apply, so the sentence has to say what does.
# The DNS-only arm opens with why the IP endpoint contributed nothing: an
# allowlist suppressed it, or the cluster serves no public IP endpoint at all.
# Claiming an allowlist on a cluster that has none tells the operator to look
# for a control they never configured.
_DNS_ENDPOINT_IP_ALLOWLISTED = "The IP endpoint is allowlisted, but the cluster also serves"
_DNS_ENDPOINT_IP_NOT_PUBLIC = "The cluster serves no public IP endpoint, but it does serve"
_IMPACT_PUBLIC_DNS_ENDPOINT = (
    " a DNS "
    "endpoint that resolves and answers from any address on the internet. "
    "Authorized networks do not gate it — IAM does, so reaching the API "
    "server needs a Google identity holding container.clusters.connect, and "
    "the exposure is that identity's blast radius rather than an unauthenticated "
    "API server. Widening the authorized-network list, or narrowing it, changes "
    "nothing about this path."
)


def check_public_control_plane(context: dict) -> list[dict]:
    """Whether the API server answers from the internet.

    Two generations of the same setting, and only one of them is authoritative
    on any given cluster. `controlPlaneEndpointsConfig.ipEndpointsConfig.
    enablePublicEndpoint` is the current field and says outright whether the
    public endpoint is served; `privateClusterConfig.enablePrivateEndpoint` is
    the legacy inversion of it, and GKE keeps returning that block for
    compatibility with only the addresses filled in. Reading them as an `or`
    took the union of two readings of the same fact: a cluster that turned the
    public endpoint off the current way, and so carries no legacy
    `enablePrivateEndpoint: true`, was reported as reachable from the internet
    at `critical`. Prefer the current field wherever GKE returns it and fall
    back to the legacy one only when it does not.

    Authorized networks answers for the IP endpoint and for nothing else, so it
    suppresses that path rather than the whole finding. A cluster whose IP
    endpoint is allowlisted and whose DNS endpoint takes external traffic is
    still answering the internet, and returning nothing for it told the
    operator the opposite -- the one shape where this check's silence was a
    false negative rather than a pass.

    An allowlist that excepts Google Cloud's own external addresses does not
    suppress the IP endpoint either, and that is the shape the fleet's own
    remediation used to produce: `--enable-master-authorized-networks
    --master-authorized-networks=<CIDR>` leaves `gcpPublicCidrsAccessEnabled`
    on, so acting on this finding closed it while the endpoint still answered
    every VM on Google Cloud. See `_has_restrictive_authorized_networks`.
    """
    describe = context.get("cluster_describe") or {}
    paths = _external_control_plane_paths(describe)
    ip_open = any("dnsEndpointConfig" not in p for p in paths)
    if _has_restrictive_authorized_networks(describe):
        paths = [p for p in paths if "dnsEndpointConfig" in p]
    if not paths:
        return []
    dns_only = all("dnsEndpointConfig" in path for path in paths)
    impact = _IMPACT_PUBLIC_IP_ENDPOINT
    if dns_only:
        lead = _DNS_ENDPOINT_IP_ALLOWLISTED if ip_open else _DNS_ENDPOINT_IP_NOT_PUBLIC
        impact = lead + _IMPACT_PUBLIC_DNS_ENDPOINT
    elif _allowlisted_but_for_google_cloud(describe):
        impact = _IMPACT_GOOGLE_CLOUD_ACCESS
    decided = "; ".join(paths)
    # Name the fields and the values, not the conclusion. `adopt_collector_evidence`
    # overwrites the model's excerpt with this string, so it is the only evidence
    # the finding will ever carry, and the constant sentence it used to be --
    # "public endpoint reachable with no restrictive authorized networks" -- was
    # byte-identical on all sixteen clusters of this fleet. That is unfalsifiable
    # by a reader and it hid a real difference: a cluster serving the endpoint
    # through the current field with `gcpPublicCidrsAccessEnabled` set read the
    # same as one caught by the legacy inversion with the whole config absent.
    excerpt = f"{decided}; {_authorized_networks_excerpt(describe)}"
    return [
        {
            "namespace": "",
            "object": _cluster_object(context),
            "excerpt": excerpt,
            "impact": impact,
        }
    ]


def _namespace_labels(context: dict, ns: str) -> dict:
    for item in context.get("namespaces") or []:
        meta = item.get("metadata") or {}
        if meta.get("name") == ns:
            return meta.get("labels") or {}
    return {}


def check_podsecurity_gaps(workload: dict, context: dict) -> dict | None:
    if check_privileged_container(workload, context) is not None:
        return None  # 2.1's finding subsumes this one; never emit both
    if _namespace_labels(context, workload["ns"]).get("pod-security.kubernetes.io/enforce") == "restricted":
        return None  # admission already guarantees it
    pod_sc = workload["spec"].get("securityContext") or {}
    bad = []
    for container in (workload["spec"].get("containers") or []) + (workload["spec"].get("initContainers") or []):
        c_sc = container.get("securityContext") or {}
        if "runAsNonRoot" in c_sc:
            non_root = c_sc["runAsNonRoot"]
        elif "runAsNonRoot" in pod_sc:
            non_root = pod_sc["runAsNonRoot"]
        else:
            non_root = None
        run_as_user = c_sc.get("runAsUser", pod_sc.get("runAsUser"))
        seccomp_type = ((c_sc.get("seccompProfile") or {}).get("type") or (pod_sc.get("seccompProfile") or {}).get("type") or "")
        # Which of the five fired, not just that one did. This check reads
        # five independent settings and a bare container name throws away the
        # only part a reader needs: the fix for `runAsUser=0` is not the fix
        # for a missing seccomp profile. `audit_report.py`'s
        # `adopt_collector_evidence` cites "a full securityContext breakdown
        # [coming back] as `containers: litellm-container`" as the detail loss
        # it exists to stop -- and then publishes this excerpt over the model's,
        # so the collector has to be the one carrying the detail.
        # `allowPrivilegeEscalation` and `capabilities` have no pod-level
        # fallback to read: `PodSecurityContext` carries neither field, so the
        # container's own value is the only one there is.
        allow_escalation = c_sc.get("allowPrivilegeEscalation")
        dropped = [str(cap).upper() for cap in ((c_sc.get("capabilities") or {}).get("drop") or [])]
        reasons = []
        if non_root is not True:
            reasons.append(f"runAsNonRoot={json.dumps(non_root)}")
        if run_as_user == 0:
            reasons.append("runAsUser=0")
        if seccomp_type not in ("RuntimeDefault", "Localhost"):
            reasons.append(f"seccompProfile.type={seccomp_type or 'absent'}")
        if allow_escalation is not False:
            reasons.append(f"allowPrivilegeEscalation={json.dumps(allow_escalation)}")
        if "ALL" not in dropped:
            reasons.append(f"capabilities.drop={json.dumps(dropped)}")
        if reasons:
            bad.append(f"{container.get('name', '')} ({', '.join(reasons)})")
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": f"containers: {'; '.join(bad)}"}


# --------------------------------------------------------------------------- #
# ai-security-audit: §3.1 `inference-endpoint-public` … §3.6
# `model-image-floating-tag`. Same dump kinds as compliance (`deploy,sts,ds,
# cronjob,pod`), plus a `svc` dump for §3.1's exposure check, so the workload
# normalizer here mirrors `normalize_compliance_workloads`'s suppressions but
# adds `lbl` (the pod template's labels, needed to match a Service selector
# against a workload in §3.1) and the §2 AI-workload discriminator, so the
# list this collector hands every check is already narrowed to AI workloads
# the way `$PRE`'s last `select` narrows the SOP's own pipeline.
# --------------------------------------------------------------------------- #

# Serving runtimes only. A name earns a place here by being a process that
# loads a model and answers inference requests, and by being distinctive enough
# that the `(^|/)`/`([-:@/]|$)` anchors cannot land it on something else. Two
# rejected candidates say where the line is: `litellm` is a proxy in front of
# model servers rather than one itself, so it holds no weights and belongs to
# the credential prong below instead; `nim` would match `nimlang/nim:2.0` under
# these anchors, and NVIDIA's NIM containers request a GPU anyway, so the
# accelerator prong already has them. `tei` is left off for the same reason
# `nim` is -- three letters, no distinctiveness -- while its full name is
# spelled out.
AI_MODEL_IMAGE_RE = re.compile(
    r"(^|/)(vllm|sglang|text-generation-inference|tgi|text-embeddings-inference|"
    r"tritonserver|torchserve|tensorflow-serving|kserve|ollama|ray|llama|mlserver|"
    r"seldon|lorax|aibrix|lmdeploy|xinference|localai|openllm)([-:@/]|$)"
)
AI_ACCELERATOR_KEY_RE = re.compile(r"nvidia\.com/gpu|google\.com/tpu")

# The third prong, and the only one that does not depend on recognising a name.
# An image allowlist is never finished, and the gap is not hypothetical: the
# `_EMPTY_SCOPE_REASON` comment further down names this stream's real exposure
# as a model server whose runtime nobody has listed, serving on CPU so no
# accelerator gives it away, reported as a clean cluster. A container holding
# `OPENAI_API_KEY` is talking to a model provider whatever its image is called.
#
# This is what puts a model gateway in scope, which the image prong deliberately
# will not do. That prong is right that a gateway holds no weights, and wrong
# about the consequence: of the six checks it opens, four cannot fire on a
# weightless workload at all -- 3.3 needs a CSI or PVC volume, 3.2 a
# `trust_remote_code` directive, 3.4 a `--model` flag or a plaintext artifact
# URL, 3.6 a floating tag. The two that remain are the two worth asking about a
# gateway, because a gateway is defined by holding provider credentials on
# behalf of everything behind it: whether its endpoint is public (3.1) and
# whether those credentials are literals (3.5). This fleet's `litellm` answers
# no to both -- ClusterIP, three `secretKeyRef`s -- which is a verdict the
# stream could not previously reach.
#
# Whole-name and named-provider only, which is stricter than §3.5's name rule
# below and deliberately so. Admitting a workload here subjects it to all six
# checks, including being asked whether its endpoint is a public *inference*
# endpoint; `(MODEL|REGISTRY|INFERENCE).*KEY` is a fair heuristic once a
# workload is known to serve models and a bad one for deciding that it does.
# §3.5 unions the two, so a provider named only here is still read for a
# plaintext value.
#
# `envFrom` is deliberately not read. The credential's name lives in the Secret
# rather than in the pod spec, so all a pod spec can say is "this container has
# some envFrom" -- true on this fleet of `ip-masq-agent` on five clusters and of
# no model server anywhere.
AI_PROVIDER_CREDENTIAL_ENV_RE = re.compile(
    r"^(OPENAI|ANTHROPIC|GEMINI|COHERE|MISTRAL|TOGETHER|GROQ|PERPLEXITY|DEEPSEEK|"
    r"FIREWORKS|ANYSCALE|AZURE_OPENAI|VERTEXAI|XAI|WANDB)_API_KEY$"
    r"|^(HF|HUGGING_FACE_HUB|HUGGINGFACE)_(TOKEN|API_TOKEN)$"
    r"|^REPLICATE_API_TOKEN$",
    re.IGNORECASE,
)


def _is_ai_workload(spec: dict) -> bool:
    containers = spec.get("containers") or []
    if any(AI_MODEL_IMAGE_RE.search(c.get("image") or "") for c in containers):
        return True
    for c in containers:
        limits = (c.get("resources") or {}).get("limits") or {}
        if any(AI_ACCELERATOR_KEY_RE.search(key) for key in limits):
            return True
    for c in containers:
        # Named, not valued: a `secretKeyRef` is the correct way to hold one of
        # these and still means the workload holds it. Whether the value is a
        # literal is 3.5's question, and answering it here would put exactly the
        # workloads that got it right out of the audit's reach.
        if any(AI_PROVIDER_CREDENTIAL_ENV_RE.search(e.get("name") or "") for e in c.get("env") or []):
            return True
    return False


def _pod_template_labels_of(item: dict) -> dict:
    """The same three-way fallback chain as the SOP's `lbl` field: a
    Deployment/StatefulSet/DaemonSet's pod template labels, a CronJob's
    (nested one level deeper), or a bare Pod's own labels."""
    spec = item.get("spec") or {}
    labels = ((spec.get("template") or {}).get("metadata") or {}).get("labels")
    if labels:
        return labels
    labels = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("metadata", {}).get("labels")
    if labels:
        return labels
    return (item.get("metadata") or {}).get("labels") or {}


def normalize_ai_workloads(dump: dict) -> list[dict]:
    out = []
    for item in dump.get("items", []) or []:
        if item.get("kind") not in COMPLIANCE_WORKLOAD_KINDS:
            continue
        meta = item.get("metadata") or {}
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):
            continue
        labels = meta.get("labels") or {}
        if "addonmanager.kubernetes.io/mode" in labels:
            continue
        if (meta.get("annotations") or {}).get("components.gke.io/component-name"):
            continue
        if item.get("kind") == "Pod" and meta.get("ownerReferences"):
            continue
        spec = _pod_spec_of(item)
        if not _is_ai_workload(spec):
            continue
        out.append({
            "kind": item["kind"], "ns": ns, "name": meta.get("name", ""), "spec": spec,
            "lbl": _pod_template_labels_of(item), "suspended": _is_suspended_cronjob(item),
            "scaled_to_zero": _is_scaled_to_zero(item),
            "reconciler": reconciler_of(meta),
            "release": release_of(meta),
        })
    return out


def _ai_containers(spec: dict) -> list[dict]:
    return (spec.get("containers") or []) + (spec.get("initContainers") or [])


# Programs whose inline-code flag turns the token after it into a command line
# (`sh`, and the shells that accept `-c`), and programs where it turns that token
# into source in some other language (`python -c`, `node -e`). The distinction
# decides what the tokens are worth reading as, so the two lists stay separate.
_INLINE_CODE_SHELLS = ("sh", "bash", "ash", "dash", "zsh", "ksh")
_INLINE_CODE_INTERPRETERS = ("python", "python2", "python3", "node", "nodejs", "perl", "ruby")
# `-c`, and the bundles a shell is routinely invoked with: `-ec`, `-lc`, `-exc`.
_SHELL_INLINE_FLAG_RE = re.compile(r"^-[a-z]*c$")
# Each family's inline-code flag, alone or after the value-less switches it is
# routinely bundled with: `python3 -uc`, `perl -le`, `ruby -ne`, `node -pe`.
_INTERPRETER_INLINE_FLAG_RES = {
    "python": re.compile(r"^-[bBdEhiIOPqRsSuvVx]*c$"),
    "perl": re.compile(r"^-[alnpswtTWX]*[eE]$"),
    "ruby": re.compile(r"^-[acdlnpswvWy]*e$"),
    "node": re.compile(r"^(-p?e|-p|--eval|--print)$"),
}
# The interpreter's own options stop at the first operand -- a script path, or
# the module `-m` names -- and every token after it belongs to that program. A
# `-c` there is the script's flag, so the scan stops rather than reading the
# token after it as inline code. The few options that take a separate value
# are skipped with their value so it is not mistaken for that operand. Both
# tables are per family: Python's `-O` and `-I` are switches where bash's `-O`
# takes a value, and `-m` names a module only to Python -- to a shell it is
# the monitor switch.
SHELL_FAMILY = "shell"
_DEFAULT_INLINE_OPTIONS_END = ("--",)
_INLINE_OPTIONS_END = {SHELL_FAMILY: _DEFAULT_INLINE_OPTIONS_END, "python": ("--", "-m")}
_INLINE_OPTIONS_WITH_VALUE = {
    SHELL_FAMILY: frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}),
    "python": frozenset({"-W", "-X", "--check-hash-based-pycs"}),
    "node": frozenset(
        {"-r", "--require", "-C", "--conditions", "--import", "--loader", "--experimental-loader", "--input-type"}
    ),
    "ruby": frozenset({"-I", "-r", "-C", "-E"}),
    "perl": frozenset({"-I"}),
}
_INTERPRETER_FAMILIES = {"python": "python", "python2": "python", "python3": "python", "nodejs": "node"}


def _container_argv(container: dict) -> list[str]:
    """`command` then `args`, the order the kubelet hands them to the process.

    `command` overrides the image's ENTRYPOINT and `args` its CMD, so this is the
    process's real argv. The order is load-bearing for anything reading a flag's
    value out of the next token: a `--model` ending `command` takes its value from
    the first element of `args`, and the reversed concatenation this replaced
    reported the flag with nothing beside it.
    """
    return [str(t) for t in (container.get("command") or [])] + [
        str(t) for t in (container.get("args") or [])
    ]


class _Argv(NamedTuple):
    """How one container's `command`/`args` resolve for the checks that read them.

    `tokens` is everything worth searching for a literal; `flags` is the subset a
    flag parser actually receives, which is empty when nothing in the container
    parses flags at all. See `_resolve_argv`.
    """

    tokens: list[str]
    flags: list[str]


def _resolve_argv(container: dict) -> _Argv:
    """The tokens a flag-parsing model loader in this container actually receives.

    A check that reads `--model` out of a manifest is claiming some argument
    parser will act on it, and `command:` is what decides whether one ever sees
    it. Three shapes, and only the third differs from the flat token list:

    * **No `command:`** -- the image's own ENTRYPOINT is the model server and
      `args` are the flags it parses. Every workload that runs its image the way
      the publisher shipped it, including this fleet's `ai-embeddings-tei`.
    * **A `command:` naming a program** -- `["python3", "-m", "vllm.entrypoints.
      openai.api_server"]`, `["text-generation-launcher"]`. The program parses the
      rest, so the whole argv counts.
    * **A `command:` invoking inline code** -- everything after the code string is
      a positional argument to that inline program (`$0`, `$1`, `sys.argv[1:]`),
      not a flag. Under `sh -c` the code string *is* a command line, so its words
      are what a parser sees and are returned in place of the positionals -- which
      also finds the `--model` in `sh -c "vllm serve --model foo"` that a
      token-prefix match cannot see, because no single token starts with it. Under
      `python -c` or `node -e` the code string is source in another language and
      nothing here parses flags at all, so this returns nothing.

    That last case is not hypothetical. On 2026-09-06 this fleet published
    `CronJob/ai-batch-finetune` as a `major` 3.4 finding reading `--model
    meta-llama/Llama-3.1-8B-Instruct with no --revision`, against a container whose
    entrypoint is `python3 -c "print('fixture; never scheduled')"`. The flag lands
    in `sys.argv` beside a print statement, no model is fetched at any point, the
    published impact ("the bytes that arrive at the next pod restart are whatever
    the source serves then") describes something that cannot happen, and the
    prescribed fix -- add `--revision <sha>` beside it -- changes nothing.

    3.2 deliberately does not use this. Its predicate is the textual presence of a
    trust directive anywhere the container carries one, and `trust_remote_code=True`
    written inside a `python -c` one-liner is a real instruction to the loader, not
    an inert flag. Different claim, different token set -- so this returns both.
    """
    command = [str(t) for t in (container.get("command") or [])]
    args = [str(t) for t in (container.get("args") or [])]
    argv = command + args
    if not command:
        return _Argv(argv, args)
    program = command[0].rsplit("/", 1)[-1]
    shell = program in _INLINE_CODE_SHELLS
    if not shell and program not in _INLINE_CODE_INTERPRETERS:
        return _Argv(argv, argv)
    family = SHELL_FAMILY if shell else _INTERPRETER_FAMILIES.get(program, program)
    flag_re = _SHELL_INLINE_FLAG_RE if shell else _INTERPRETER_INLINE_FLAG_RES[family]
    options_end = _INLINE_OPTIONS_END.get(family, _DEFAULT_INLINE_OPTIONS_END)
    options_with_value = _INLINE_OPTIONS_WITH_VALUE.get(family, frozenset())
    skip_value = False
    for i, token in enumerate(argv[1:], start=1):
        if skip_value:
            skip_value = False
            continue
        if token in options_end or not token.startswith(("-", "+")):
            break
        if token in options_with_value:
            skip_value = True
            continue
        if not flag_re.fullmatch(token) or i + 1 >= len(argv):
            continue
        if not shell:
            return _Argv(argv, [])
        try:
            words = shlex.split(argv[i + 1])
        except ValueError:
            # Unbalanced quoting. Read nothing rather than guess at a split, for
            # the same reason the interpreter arm returns nothing: a wrong token
            # list is a finding about an argument the process never receives.
            return _Argv(argv, [])
        # The words replace the code string rather than joining it, so a URL
        # inside a `sh -c` line is reported once, as itself, instead of twice --
        # once more with the whole command line labelled "plaintext URL".
        return _Argv(argv[: i + 1] + words + argv[i + 2 :], words)
    return _Argv(argv, argv)


# The setting and whatever value is written against it: `=false`,
# `"trust_remote_code": false` inside an `--hf-overrides` JSON, or nothing, when
# the value (if any) is the next argument. `no-`/`no_` is the explicit opt-out.
TRUST_REMOTE_CODE_ARG_RE = re.compile(
    r"(no[-_])?trust[-_]remote[-_]code[\"']?(?:\s*[=:]\s*[\"']?(\w+))?", re.IGNORECASE
)
TRUST_REMOTE_CODE_FALSE_VALUES = ("0", "false", "no", "off")
TRUST_REMOTE_CODE_ENV_NAME_RE = re.compile(r"TRUST_REMOTE_CODE", re.IGNORECASE)


# Returns which setting trusts remote code, not merely that one does. The two
# arms take different fixes -- a token in `args` is removed from the command
# line, an env var from the container's `env` -- so a bare container name leaves
# the reader unable to check the recommendation against the evidence.
# `check_podsecurity_gaps` above carries the same reasoning at length, including
# why the collector rather than the model has to be the one holding the detail.
#
# The 2026-09-06 08:55Z report is the instance: evidence `containers: embeddings`
# under a recommendation to "remove the TRUST_REMOTE_CODE=true environment
# variable". The recommendation was right and nothing published alongside it
# said so. `str | None` rather than a reason list because the arms are not
# independent settings the way §2.4's five are -- either one alone is the whole
# finding, and the first one found is enough to act on.
def _container_trusts_remote_code(c: dict) -> str | None:
    tokens = _resolve_argv(c).tokens
    for i, t in enumerate(tokens):
        for match in TRUST_REMOTE_CODE_ARG_RE.finditer(t):
            if match.group(1):
                continue  # `--no-trust-remote-code` refuses it
            value = match.group(2)
            if value is None and match.end() == len(t) and i + 1 < len(tokens):
                # `--trust-remote-code false`: the value is the next argument.
                # A flag followed by another flag, or by anything else, is the
                # bare boolean switch and does enable it.
                nxt = tokens[i + 1].lower()
                value = nxt if nxt in TRUST_REMOTE_CODE_FALSE_VALUES else None
            if value is not None and value.lower() in TRUST_REMOTE_CODE_FALSE_VALUES:
                continue
            # The match, not the token: an inline `python3 -c` program is one
            # token, and it can carry a `token=` literal beside the setting.
            return f"arg setting {match.group(0)}"
    for e in c.get("env") or []:
        if TRUST_REMOTE_CODE_ENV_NAME_RE.search(e.get("name") or "") and str(e.get("value", "")).lower() in ("1", "true", "yes"):
            return f"env {e.get('name')}={e.get('value')}"
    return None


def check_model_remote_code_trusted(workload: dict, context: dict) -> dict | None:
    bad = []
    for c in _ai_containers(workload["spec"]):
        reason = _container_trusts_remote_code(c)
        if reason:
            bad.append(f"{c.get('name', '')} ({reason})")
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": f"containers: {', '.join(bad)}"}


def _env_paths_under(container: dict, mount_path: str) -> list[str]:
    """Env vars on this container whose literal value is a path inside `mount_path`.

    A container that keeps HOME, a cache directory, or a model directory inside the
    weights mount writes to that mount, so adding `readOnly: true` to it stops the
    process instead of hardening it -- ollama derives OLLAMA_MODELS=$HOME/.ollama/models
    and prunes it at every start, vLLM writes HF_HOME, and both exit on boot. The
    remediation for 3.3 only sees this finding's evidence, so the conflict has to be
    named here for it to move the write path out in the same change rather than
    flipping the flag blind.
    """
    if not mount_path:
        return []
    base = mount_path.rstrip("/")
    hits = []
    for e in container.get("env") or []:
        v = e.get("value")
        if not isinstance(v, str) or e.get("valueFrom") is not None:
            continue
        if v == base or v.startswith(base + "/"):
            hits.append(f"{e.get('name')}={v}")
    return hits


def check_weights_mount_writable(workload: dict, context: dict) -> dict | None:
    vols_by_name = {v.get("name"): v for v in workload["spec"].get("volumes") or []}
    bad = []
    for c in workload["spec"].get("containers") or []:
        for m in c.get("volumeMounts") or []:
            if m.get("readOnly", False):
                continue
            vol = vols_by_name.get(m.get("name"))
            if vol is None:
                continue
            csi, pvc = vol.get("csi"), vol.get("persistentVolumeClaim")
            if (csi is not None and not csi.get("readOnly", False)) or (pvc is not None and not pvc.get("readOnly", False)):
                entry = f"{c.get('name', '')}:{m.get('name')}:{m.get('mountPath')}"
                writers = _env_paths_under(c, m.get("mountPath"))
                if writers:
                    entry += " (container writes here: " + ", ".join(writers) + ")"
                bad.append(entry)
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": "; ".join(bad)}


AI_URL_RE = re.compile(r"(^|=)(http|ftp)://")
# The URL itself, cut out of whatever token or value carries it: a token like
# `--opts=--api-key=K,src=http://m/x` is published only as `http://m/x`.
AI_URL_EXTRACT_RE = re.compile(r"(?:http|ftp)://[^\s,;'\"]+")
# §3.4(a) is about a model artifact, not every plaintext URL an AI container
# holds: `OPENAI_API_BASE=http://vllm:8000/v1` is a service endpoint. A URL
# counts when the flag or variable carrying it names a model artifact, or when
# its path ends in a model file.
AI_MODEL_URL_FLAG_RE = re.compile(
    r"^--(model|model-id|model-path|model-url|model-name|tokenizer|weights|checkpoint|adapter|lora[\w-]*)(=|$)"
)
AI_MODEL_ENV_NAME_RE = re.compile(r"MODEL|WEIGHT|CHECKPOINT|TOKENIZER|ADAPTER|LORA|HF_ENDPOINT|HF_HUB", re.IGNORECASE)
AI_MODEL_FILE_RE = re.compile(r"\.(safetensors|gguf|bin|pt|pth|onnx|ckpt|h5|tflite|pb|tar|tgz|tar\.gz|zip)$", re.IGNORECASE)
AI_MODEL_FLAG_RE = re.compile(r"^--model(-id)?(=|$)")
AI_REVISION_FLAG_RE = re.compile(r"^--revision(=|$)")
AI_UNPINNABLE_MODEL_PREFIXES = ("/", "./", "../", "gs://", "s3://", "file://")


def _model_value(flag: str) -> str:
    """`--model=x` and `--model x` both give `x`."""
    return re.split(r"[=\s]", flag, maxsplit=1)[-1] if re.search(r"[=\s]", flag) else ""
# Userinfo and query string are where a signed-URL token or a basic-auth
# password rides along, and this excerpt is published to a public GitHub
# issue. The scheme, host and path are the whole of what the finding needs.
AI_URL_CREDENTIAL_RE = re.compile(r"://[^@\s]*@|\?\S*")


def _ai_safe_url(value: str) -> str:
    return AI_URL_CREDENTIAL_RE.sub(lambda m: "://" if m.group(0).endswith("@") else "?…", value)


def _names_model_file(url: str) -> bool:
    return bool(AI_MODEL_FILE_RE.search(url.split("?", 1)[0].rstrip("/")))


def _model_artifact_urls(tokens: list[str], env: list[dict]) -> list[str]:
    """Plaintext URLs that name a model artifact (§3.4(a)), as bare URLs.

    Only the URL is returned, never the token around it: the excerpt is
    published, and the rest of a token or value can be a credential.
    """
    found = []
    for i, token in enumerate(tokens):
        if not AI_URL_RE.search(token):
            continue
        flag = token.split("=", 1)[0] if token.startswith("--") and "=" in token else (tokens[i - 1] if i else "")
        for url in AI_URL_EXTRACT_RE.findall(token):
            if AI_MODEL_URL_FLAG_RE.search(flag) or _names_model_file(url):
                found.append(url)
    for e in env:
        value = str(e.get("value", ""))
        if not AI_URL_RE.search(value):
            continue
        named = bool(AI_MODEL_ENV_NAME_RE.search(str(e.get("name", ""))))
        found.extend(url for url in AI_URL_EXTRACT_RE.findall(value) if named or _names_model_file(url))
    return found


def check_model_artifact_unpinned_source(workload: dict, context: dict) -> dict | None:
    bad = []
    escalate = False
    for c in _ai_containers(workload["spec"]):
        argv = _resolve_argv(c)
        urls = list(dict.fromkeys(_model_artifact_urls(argv.tokens, c.get("env") or [])))
        # The flag arm reads only what a flag parser in this container would see,
        # which is not always the whole argv -- see `_resolve_argv`. The URL arm
        # above stays on every token, because a plaintext fetch is a plaintext
        # fetch whoever issues it.
        flag_argv = argv.flags
        # `--model=x` carries its value; bare `--model x` leaves it in the next
        # argument, and reporting the flag without the model name names nothing.
        models = [
            a if "=" in a else " ".join(flag_argv[i : i + 2])
            for i, a in enumerate(flag_argv)
            if AI_MODEL_FLAG_RE.search(a)
        ]
        # The SOP's Do-NOT-flag: a path an image layer or volume already
        # populated fetches nothing, and an object store's control is its
        # versioning and IAM, which `--revision` does not touch.
        models = [m for m in models if not _model_value(m).startswith(AI_UNPINNABLE_MODEL_PREFIXES)]
        has_revision_flag = any(AI_REVISION_FLAG_RE.search(a) for a in flag_argv)
        # Name the value that tripped the check, not just the container it sat
        # in. The two conditions fail for different reasons and take different
        # fixes -- a plaintext URL wants a digest-addressed source, an
        # unpinned `--model` wants a `--revision` -- and an excerpt reading
        # `containers: inference` distinguishes neither, nor says what to go
        # and look at. Both can hold on one container, so both are reported.
        reasons = []
        if urls:
            reasons.append("plaintext URL " + ", ".join(_ai_safe_url(u) for u in urls[:3]))
        if models and not has_revision_flag:
            # Redacted for the same reason the URL clause above is: a `--model`
            # value is very often a URL, and this one reaches the same public
            # issue. Leaving a token here would have made the redaction on the
            # line above decorative -- the identical string arrives through
            # both clauses whenever a container passes its model as a URL.
            safe_models = [_ai_safe_url(m) for m in models[:3]]
            reasons.append(f"{', '.join(safe_models)} with no --revision")
        if reasons:
            bad.append(f"{c.get('name', '')}: {'; '.join(reasons)}")
            if _container_trusts_remote_code(c):
                escalate = True  # §3.4: escalates to critical alongside a 3.2 finding on the same container
    if not bad:
        return None
    hit = {"object": f"{workload['kind']}/{workload['name']}", "excerpt": " | ".join(bad)}
    if escalate:
        hit["severity"] = "critical"
    return hit


# Unioned with `AI_PROVIDER_CREDENTIAL_ENV_RE` at the call site rather than
# merged into it: that one is anchored whole-name because it decides scope,
# while the `(MODEL|REGISTRY|INFERENCE).*` heuristic here is safe only because
# everything reaching it is already a known AI workload. Keeping both means a
# provider that only the anchored list names -- `COHERE_API_KEY`,
# `MISTRAL_API_KEY` -- cannot admit a workload to the audit and then go unread
# for a plaintext value once it is in.
AI_CREDENTIAL_ENV_NAME_RE = re.compile(
    r"HF_[A-Z_]*TOKEN|HUGGING_?FACE.*TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|WANDB_API_KEY|"
    r"(MODEL|REGISTRY|INFERENCE).*(TOKEN|KEY|SECRET|PASSWORD)",
    re.IGNORECASE,
)
# §3.5's own named non-secret examples (`HF_TOKEN_PATH`, `OPENAI_API_KEY_FILE`,
# `MODEL_REGISTRY_KEY_ID`): a name ending in one of these suffixes names a
# path, a file, or an identifier *about* a credential, never the credential's
# value itself, no matter what it matches upstream of the suffix.
AI_CREDENTIAL_ENV_NAME_SAFE_SUFFIX_RE = re.compile(r"_(PATH|FILE|ID)$", re.IGNORECASE)


# The name rule says a literal is there; only the value says whether it is a
# secret. `HF_TOKEN=hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN` -- shipped in this
# fleet's own ai-inference demo -- is the same shape to a name-only rule as a
# live Hugging Face token, so any fleet carrying example manifests turns this
# check into noise at `major`.
AI_CREDENTIAL_PLACEHOLDER_WORD_RE = re.compile(
    r"EXAMPLE|PLACEHOLDER|CHANGE|DUMMY|FAKE|SAMPLE|REDACTED|NOT_?A_?REAL|TODO|FIXME"
    r"|YOUR|HERE|TOKEN|KEY|SECRET|PASSWORD|PASS|CRED|API|VALUE|NONE|REAL|NOT|ME|X{4,}",
    re.IGNORECASE,
)
# `$(FOO)` is Kubernetes' own env expansion, `${FOO}` and `{{ FOO }}` a
# templater's: the literal holds a reference to a value kept elsewhere, which
# is the opposite of the thing this check is looking for. Closing delimiter
# required -- an unterminated `$(` expands to nothing, so treating it as a
# reference would let it swallow the secret that follows it.
AI_CREDENTIAL_REFERENCE_RE = re.compile(r"\$\([^)]*\)|\$\{[^}]*\}|\{\{.*?\}\}")
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
#: An opaque run this long is the secret itself. Below it, a fragment is
#: decoration -- the `hf` in `hf_EXAMPLE_...`, the `sk` in `sk-...`.
_OPAQUE_RUN = 4


def _reads_as_inert(value: str) -> bool:
    """Does the whole value read as a placeholder or an unexpanded reference?

    Whole value, not any part of it: a substring test downgrades
    ``sk-proj-Todo7x...`` because it contains "todo", and a DSN whose *host* is
    ``db.example.com`` because of the host, while its password is live. So
    strip the reference expressions, split what is left on punctuation, and
    require every remaining run of ``_OPAQUE_RUN`` characters or more to be a
    placeholder word. One high-entropy run is enough to fail the test, which is
    the safe direction -- failing it keeps the finding at ``major``.

    The cost is the other direction: ``Bearer ${TOKEN}`` and AWS's own
    ``AKIAIOSFODNN7EXAMPLE`` do not read as inert here, because "bearer" is not
    a placeholder word and the AWS key is a single unbroken run. Both stay at
    ``major``, which is a wrong severity on a real finding rather than a
    suppressed one.
    """
    residue = AI_CREDENTIAL_REFERENCE_RE.sub(" ", value)
    referenced = residue != value
    matched = False
    for token in _WORD_SPLIT_RE.split(residue):
        if not token:
            continue
        if AI_CREDENTIAL_PLACEHOLDER_WORD_RE.fullmatch(token):
            matched = True
        elif len(token) >= _OPAQUE_RUN:
            return False
    return matched or referenced


#: The inert arm's consequence. The `CheckSpec` default states a credential *is*
#: embedded and must be rotated, which is the one sentence §3.5 of the SOP names
#: as false here: this arm fired precisely because every value reads as a
#: placeholder, so asserting a live credential contradicts the reason the finding
#: is `minor` and sends the owner to rotate something that does not exist.
_IMPACT_CREDENTIAL_INERT = (
    "A variable named for a model-registry credential carries a literal value "
    "rather than a `secretKeyRef`. Every value reads as a placeholder, so "
    "probably nothing is exposed today -- but the workload is shaped so that the "
    "day a real token is pasted in, it lands in `kubectl describe` output and in "
    "Git with no further change."
)


# What a literal credential can be replaced with, read off a live object instead
# of invented: `<variable name>` -> `(secret name, secret key)`, for the
# variables some container in `namespace` already sources from a `secretKeyRef`.
#
# `optional: true` is required, and it is the whole safety argument. This audit
# cannot read Secrets -- RBAC denies `secrets.get` and `secrets.list` fleet-wide,
# which is what makes §5's "never paste a credential into an excerpt" true by
# construction rather than by instruction -- so it can never confirm that the
# Secret behind a reference exists. Under `optional: true` it does not have to: a
# missing Secret leaves the variable unset, which is exactly as harmless as the
# placeholder being replaced. Under `optional: false` a missing Secret is
# `CreateContainerConfigError`, and the audit would have stopped a running
# workload to fix a `minor` finding about a value that was never a credential.
#
# One namespace, because a Secret is namespaced and a reference across namespaces
# does not resolve. Disagreement is dropped rather than arbitrated: two containers
# naming different Secrets for one variable means the namespace has no single
# answer, and choosing between them would be this collector inventing the Secret
# name the SOP's §4 forbids it to invent.
def _namespace_secret_refs(namespace: str, workloads: list[dict]) -> dict[str, tuple[str, str]]:
    seen: dict[str, set[tuple[str, str]]] = {}
    for other in workloads:
        if other.get("ns") != namespace:
            continue
        for c in _ai_containers(other.get("spec") or {}):
            for e in c.get("env") or []:
                ref = ((e.get("valueFrom") or {}).get("secretKeyRef")) or {}
                name, key, var = ref.get("name"), ref.get("key"), e.get("name") or ""
                if not (ref.get("optional") and name and key and var):
                    continue
                seen.setdefault(var, set()).add((str(name), str(key)))
    return {var: refs.pop() for var, refs in seen.items() if len(refs) == 1}


def check_model_credential_plaintext_env(workload: dict, context: dict) -> dict | None:
    bad, inert = [], []
    for c in _ai_containers(workload["spec"]):
        for e in c.get("env") or []:
            name, value = e.get("name") or "", e.get("value")
            if (
                value
                and e.get("valueFrom") is None
                and (
                    AI_CREDENTIAL_ENV_NAME_RE.search(name)
                    or AI_PROVIDER_CREDENTIAL_ENV_RE.search(name)
                )
                and not AI_CREDENTIAL_ENV_NAME_SAFE_SUFFIX_RE.search(name)
            ):
                bad.append(f"{c.get('name', '')}:{name}")
                if _reads_as_inert(value):
                    inert.append(f"{c.get('name', '')}:{name}")
    if not bad:
        return None
    hit = {"object": f"{workload['kind']}/{workload['name']}", "excerpt": f"set with a literal value: {', '.join(bad)}"}
    # Downgraded, never dropped: looking like a placeholder is not proof of
    # being one, and suppressing a real credential is the worse error by far.
    # A mixed workload keeps `major` -- one live token is not made safe by the
    # placeholders beside it.
    if len(inert) == len(bad):
        hit["severity"] = "minor"
        # Both arms, or neither. Setting `severity` alone leaves `emit` falling
        # back to `spec.impact` -- the `major` sentence -- and leaves
        # `impact_authoritative` unset, so `adopt_arm_impact` never corrects it
        # and `carry_unchanged_findings` republishes it forever.
        hit["impact"] = _IMPACT_CREDENTIAL_INERT
        # What was measured, not what it proves. `adopt_collector_evidence`
        # forces this excerpt onto the finding but never copies `severity`, so
        # the sentence has to still read correctly under a `major` the model
        # kept -- and "not a live secret" beside `major` reads as the report
        # contradicting itself.
        hit["excerpt"] += "; every value matches this check's placeholder or unexpanded-reference patterns"
        # Named here so the model can promote the remediation to `manifest`.
        # Only on the inert arm: on the `major` arm a value may be a live token,
        # and swapping it for a reference to a Secret whose contents this audit
        # cannot read would silently change which credential the workload uses
        # -- or drop it. The placeholder this arm fired on has no such value to
        # preserve, so the swap can only improve the declaration.
        #
        # All or nothing across `bad`. A partial rewrite leaves the finding open
        # against the variables it did not reach, and §3.1's suppression of a
        # resize that leaves its own finding standing is the same rule.
        refs = _namespace_secret_refs(workload.get("ns") or "", context.get("ai_workloads") or [])
        wanted = {entry.split(":", 1)[1] for entry in bad}
        if wanted and wanted <= refs.keys():
            shape = ", ".join(
                f"{var} -> secretKeyRef name={refs[var][0]} key={refs[var][1]} optional=true"
                for var in sorted(wanted)
            )
            hit["excerpt"] += (
                f"; a container in namespace {workload.get('ns') or ''} already sources every one "
                f"of them from a Secret ({shape}), so copying that reference over these literals "
                f"fixes the finding without this audit creating a Secret or reading a value"
            )
    return hit


AI_FLOATING_TAG_RE = re.compile(r":(latest|main|master|dev|nightly|stable)$")
AI_TAG_RE = re.compile(r":[^/]*$")
AI_DIGEST_RE = re.compile(r"@sha256:")


def check_model_image_floating_tag(workload: dict, context: dict) -> dict | None:
    bad = []
    for c in _ai_containers(workload["spec"]):
        img = c.get("image") or ""
        if not img or AI_DIGEST_RE.search(img):
            continue
        if AI_FLOATING_TAG_RE.search(img) or not AI_TAG_RE.search(img):
            bad.append(f"{c.get('name', '')}:{img}")
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": "; ".join(bad)}


def _is_private_address(value: str) -> bool:
    """Is this load-balancer address unreachable from the public internet?

    A hostname is not resolved -- the collector makes no network calls -- so it
    counts as public. That keeps the finding on something unverifiable rather
    than dropping it, which is the safe direction for this check.
    """
    try:
        return ipaddress.ip_address(value).is_private
    except ValueError:
        return False


def _restricting_source_ranges(spec: dict) -> list[str]:
    """The Service's source-range allowlist, or ``[]`` if it restricts nothing.

    GKE programs ``loadBalancerSourceRanges`` into the firewall rule in front of
    the forwarding rule, so it is enforcement rather than intent. Three ways a
    populated field still restricts nothing, all of which return ``[]``: a
    default route (``0.0.0.0/0``, ``::/0``) admits the whole internet, an
    unparseable entry means the allowlist cannot be read at all, and an empty
    list was never a restriction. Erring towards "unrestricted" keeps the
    severity where it was.

    The unparseable case is why this does not simply call `_is_default_route`
    for everything: there, a CIDR that will not parse is "not the allow-all
    entry" and keeps the control-plane finding, which is that caller's safe
    direction. Here it is "cannot vouch for this allowlist", and the safe
    direction is the opposite one.
    """
    ranges = [r.strip() for r in (spec.get("loadBalancerSourceRanges") or []) if isinstance(r, str) and r.strip()]
    for r in ranges:
        try:
            ipaddress.ip_network(r, strict=False)
        except ValueError:
            return []
        if _is_default_route(r):
            return []
    return ranges


# Prefixes reserved for documentation and examples, which no host anywhere is
# ever assigned: RFC 5737's three IPv4 blocks, RFC 3849's IPv6 block, and RFC
# 6666's discard prefix. Deliberately *not* the RFC 1918 ranges -- those address
# real hosts, and an operator who allowlists one on an external load balancer may
# be admitting traffic that arrives over Interconnect or a VPN with its source
# intact. Only these can be asserted to admit nobody.
_DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32", "100::/64")
)


def _is_documentation_range(cidr: str) -> bool:
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    return any(net.version == doc.version and net.subnet_of(doc) for doc in _DOCUMENTATION_NETWORKS)


def check_inference_endpoint_public(context: dict) -> list[dict]:
    hits = []
    for svc in context.get("services") or []:
        meta, spec = svc.get("metadata") or {}, svc.get("spec") or {}
        if spec.get("type") != "LoadBalancer":
            continue
        if _is_internal_load_balancer(meta):
            continue
        selector = spec.get("selector") or {}
        if not selector:
            continue
        ns = meta.get("namespace", "")
        matched = any(
            w["ns"] == ns and all(w.get("lbl", {}).get(k) == v for k, v in selector.items())
            for w in context.get("ai_workloads") or []
        )
        if not matched:
            continue
        # An annotation records what was asked for; `status.loadBalancer` records
        # what was handed out, and it is already in the same dump. A Service whose
        # every assigned address is private is not reachable from the internet
        # whatever its annotations say, so calling it a public endpoint is just
        # wrong. With no address yet the load balancer is still provisioning and
        # the annotations are all there is -- the behaviour before this check
        # looked at status at all.
        # Both fields, not `ip or hostname`: an ingress entry may carry each,
        # and short-circuiting on `ip` would throw away a hostname that
        # `_is_private_address` treats as public. That is the one direction
        # that loses a finding.
        assigned = [
            addr
            for ing in ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
            for addr in (ing.get("ip") or "", ing.get("hostname") or "")
            if addr
        ]
        public = [addr for addr in assigned if not _is_private_address(addr)]
        # Dropped rather than downgraded, unlike the placeholder branch in
        # `check_model_credential_plaintext_env`. That one guesses at intent
        # from a value's shape and can be wrong about a live secret; this one
        # reads routability off the assigned address, and an RFC 1918 address
        # is not reachable from the internet whatever else is true. A hostname
        # never lands here -- `_is_private_address` calls it public -- so the
        # branch needs every entry to be an unambiguously private literal.
        if assigned and not public:
            continue
        hit = {
            "namespace": ns,
            "object": f"Service/{meta.get('name', '')}",
            # §3.1 is always `manual` -- the correct caller range is a fact
            # about intended usage this audit cannot read -- so the reader is
            # being told to go and edit something, and on 2026-09-06 that
            # something was a Service the Argo CD Application
            # `workloads-adamparco-gitops` reasserts on every sync.
            "reconciler": reconciler_of(meta),
            "release": release_of(meta),
            # The count, never the address. `ai_security_audit_sop.md`
            # (Red Lines, and again under check 3.5) forbids publishing
            # the address of a reachable model endpoint, and these
            # findings are filed as issues on a public repository --
            # writing one here would hand a reader the target. It is not
            # advisory either: `adopt_collector_evidence` overwrites the
            # model's excerpt with this string, so an SOP-compliant
            # excerpt would be replaced by whatever is written here.
            "excerpt": "type=LoadBalancer, no internal-LB annotation, selects an AI workload in this namespace"
            + (_address_count_clause(assigned, public) if public else ""),
        }
        # Downgraded rather than dropped, the opposite of the private-address
        # branch above. A private address settles reachability outright; an
        # allowlist only bounds who reaches it, and a `/8` of public space
        # bounds very little. So the endpoint stays a finding and the severity
        # stops claiming what `impact` says of an unrestricted one -- that
        # anyone who finds the address can use it.
        #
        # Counts again, not the ranges themselves: which networks are trusted
        # is the other half of the target, and this goes in a public issue.
        ranges = _restricting_source_ranges(spec)
        if ranges:
            hit["severity"] = "major"
            # Set alongside the severity, never without it. `emit` derives
            # `impact_authoritative` from `hit["impact"]` alone, so an arm that
            # moved the severity and left the impact published the unrestricted
            # `critical` sentence -- "anyone who finds the address" -- on a
            # `major` finding, with nothing to stop the model rewriting it and
            # nothing for `adopt_arm_impact` to hold it to afterwards. That is
            # the flaw `test_a_downgraded_credential_candidate_is_impact_authoritative`
            # was written for on 3.5; this arm had it too.
            hit["impact"] = (
                "This model server is reachable from outside the cluster, bounded by the CIDR "
                "allowlist in spec.loadBalancerSourceRanges. Any caller inside an allowlisted "
                "range can send it inference traffic, consume its accelerator capacity, and probe "
                "whatever the model can reach."
            )
            hit["excerpt"] += (
                f"; loadBalancerSourceRanges admits {len(ranges)} CIDR{'s' if len(ranges) > 1 else ''}, not the whole internet"
            )
            # One more step down, for the same reason `major` is a step down
            # from `critical`: the severity should not claim an exposure the
            # allowlist rules out. Where every entry is a documentation prefix
            # no host is ever assigned, the allowlist admits nobody, and the
            # 2026-09-05 report nevertheless told an operator that "anyone
            # connecting from within that allowed range can send it inference
            # traffic, consume its accelerator capacity, and probe whatever the
            # model can reach" about `192.0.2.0/24`. Not dropped, unlike the
            # all-private-address branch above: that one settles reachability
            # from what the platform assigned, while this one is a Service
            # holding a public address and forwarding rule for callers who
            # cannot exist -- a real misconfiguration, just not an exposure.
            if all(_is_documentation_range(r) for r in ranges):
                hit["severity"] = "minor"
                hit["impact"] = (
                    "This Service holds a public load-balancer address and forwarding rule that no "
                    "caller can reach: every CIDR in its allowlist is a prefix reserved for "
                    "documentation, so no host is ever assigned one. Nothing is exposed. What is "
                    "wrong is the allowlist -- an example value left in place -- which means the "
                    "endpoint costs a public address and serves no one, and whoever set it "
                    "intended some real range."
                )
                hit["excerpt"] += (
                    "; every one of them is a reserved documentation prefix (RFC 5737/RFC 3849), "
                    "which no host is ever assigned -- the allowlist admits no caller at all, so "
                    "this endpoint holds a public address and reaches nobody"
                )
        hits.append(hit)
    return hits


# Management ports: remote access (22, 3389), the control plane's own (2379,
# 2380, 10250), and the datastores -- ports whose service admits a caller on a
# credential, so opening one to the internet makes that credential the whole
# perimeter. The networking audit has no firewall-side check with a port set of
# its own yet, so this is the only copy. One added there must use this same set
# and a test must pin the pair: a port admitted on one side and not the other is
# a firewall rule reported daily for a Service this check leaves open.
#
# 80, 443 and 8080 are absent on purpose: a LoadBalancer Service
# publishing a web port to the internet is a LoadBalancer Service doing its job,
# and admitting them would flag every one in the fleet.
_WORLD_OPEN_LB_PORTS: dict[int, str] = {
    22: "SSH",
    1433: "MSSQL",
    2379: "etcd",
    2380: "etcd-peer",
    3306: "MySQL",
    3389: "RDP",
    5432: "PostgreSQL",
    6379: "Redis",
    9200: "Elasticsearch",
    10250: "kubelet",
    27017: "MongoDB",
}

# The two annotations GKE accepts for "give me an internal load balancer". The
# second is the legacy spelling and still honoured, so reading only the first
# would report an internal Service as world-open.
_INTERNAL_LB_ANNOTATIONS = (
    "networking.gke.io/load-balancer-type",
    "cloud.google.com/load-balancer-type",
)
_INTERNAL_LB_ANNOTATION_VALUE = "Internal"

# `spec.ports[].protocol` when the field is omitted.
_DEFAULT_SERVICE_PROTOCOL = "TCP"

# S2, the label GKE stamps on the add-ons it manages. Named here rather than
# repeated as a literal because this is the first Service-scoped check to apply
# the suppression; the workload-scoped ones inherit it from `normalize_workloads`.
_ADDON_MANAGER_MODE_LABEL = "addonmanager.kubernetes.io/mode"

_IMPACT_LB_WORLD_OPEN = (
    "This Service holds a public load-balancer address that forwards a "
    "management or datastore port straight to its pods, with no source-range "
    "allowlist in front of it. Anyone on the internet can open a connection to "
    "it, so the only thing standing between the service behind that port and "
    "the whole internet is whatever authentication that service does itself -- "
    "and the ports here belong to software that is routinely deployed with "
    "none, on the assumption that the network is the boundary. Public address "
    "ranges are scanned continuously, so this is found without being looked for."
)


def _is_internal_load_balancer(meta: dict) -> bool:
    """Whether either GKE internal-load-balancer annotation is set on this Service."""
    annotations = meta.get("annotations") or {}
    return any(
        annotations.get(key) == _INTERNAL_LB_ANNOTATION_VALUE for key in _INTERNAL_LB_ANNOTATIONS
    )


def _assigned_public_addresses(svc: dict) -> tuple[list[str], list[str]]:
    """`(assigned, public)` load-balancer addresses, per `_is_private_address`.

    Both `ip` and `hostname` off every ingress entry, for the reason
    `check_inference_endpoint_public` gives where it does the same thing: an
    entry may carry each, and short-circuiting on `ip` throws away a hostname
    that counts as public.
    """
    assigned = [
        addr
        for ing in ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        for addr in (ing.get("ip") or "", ing.get("hostname") or "")
        if addr
    ]
    return assigned, [addr for addr in assigned if not _is_private_address(addr)]


def _address_count_clause(assigned: list[str], public: list[str]) -> str:
    """How many load-balancer addresses were assigned and how many route, never which.

    Counted over `assigned`, with the routable share stated when it is not all
    of them: an ingress of one private and one public address is two assigned
    addresses, and an excerpt that said "1 assigned address" would be
    contradicted by the first `kubectl get svc` a reader runs.
    """
    plural = "es" if len(assigned) > 1 else ""
    if len(public) == len(assigned):
        return f"; {len(assigned)} assigned address{plural}, none of them private"
    return f"; {len(assigned)} assigned address{plural}, {len(public)} of them not private"


def check_lb_world_open(context: dict) -> list[dict]:
    """Cluster-scoped: a LoadBalancer Service publishing a management port to the internet.

    A `type: LoadBalancer` Service is an instruction to GCP to put a public
    address and a forwarding rule in front of some pods, and GKE carries it out
    without asking what is behind them. Point one at a database and the database
    is on the internet -- reachable by anyone who scans the address space, which
    for a Postgres or Redis port is a matter of hours. Nothing in Kubernetes
    objects to it. The Service admits, the load balancer provisions, the
    `EXTERNAL-IP` column fills in, and every check anyone would think to run says
    the Service is healthy, because it is: it is doing exactly what the manifest
    asked.

    It is one field that does it, and usually a copied one. A Service written
    `type: ClusterIP` for local development, promoted with `type: LoadBalancer`
    to reach it from a laptop, and merged. A Helm chart whose `service.type` is
    set in the values file used for every environment. A datastore moved out of
    `kube-system` into its own namespace with the Service carried across intact.

    Scoped by port, and that is what makes the finding decidable. §3.1's
    `inference-endpoint-public` flags any world-reachable model endpoint and is
    always `manual`, because the range of callers a model server is supposed to
    serve is a fact about intended usage this audit cannot read. Here there is
    no such range: nothing legitimately reaches a cluster's Redis, etcd or
    kubelet port from the open internet, so the fix does not depend on knowing
    who the caller was meant to be, and a pull request can carry it.

    Deliberately not the web ports, which is the one exclusion that keeps this
    check usable -- see `_WORLD_OPEN_LB_PORTS`.

    Three conditions beyond the port, each of which would otherwise produce a
    finding on something that is not exposed:

    - An internal load balancer is not on the internet whatever ports it
      publishes, and both annotation spellings say so.
    - `spec.loadBalancerSourceRanges` is enforcement, not intent: GKE programs it
      into the firewall in front of the forwarding rule. A Service that restricts
      to real CIDRs is bounded rather than open. `_restricting_source_ranges`
      already reads a `0.0.0.0/0` entry as no restriction at all.
    - `status.loadBalancer` records the address that was actually handed out. A
      Service whose every assigned address is private -- an internal balancer
      configured some other way, or a cluster whose provider ignores the type --
      is not reachable from the internet, so calling it exposed is just wrong.
      No address yet means the balancer is still provisioning, and the spec is
      all there is.

    UDP is excluded because none of these ports are UDP services; a UDP 27017 is
    not MongoDB, and flagging it would be flagging the number rather than the
    thing.

    The address is never published. This finding is filed as a comment on a
    public GitHub issue, and the whole content of the vulnerability is where to
    connect -- the port names what it is, the count carries the scale, and
    neither hands a reader the target. `adopt_collector_evidence` overwrites the
    model's excerpt with this string, so it is the excerpt that ships.
    """
    hits = []
    for svc in context.get("services") or []:
        meta, spec = svc.get("metadata") or {}, svc.get("spec") or {}
        if spec.get("type") != SERVICE_TYPE_LOAD_BALANCER:
            continue
        if _is_system_namespace(meta.get("namespace", "")):  # S1
            continue
        if _ADDON_MANAGER_MODE_LABEL in (meta.get("labels") or {}):  # S2
            continue
        if _is_internal_load_balancer(meta):
            continue
        if _restricting_source_ranges(spec):
            continue
        assigned, public = _assigned_public_addresses(svc)
        if assigned and not public:
            continue
        exposed = sorted(
            {
                port
                for entry in spec.get("ports") or []
                if isinstance(entry, dict)
                and (entry.get("protocol") or _DEFAULT_SERVICE_PROTOCOL) == _DEFAULT_SERVICE_PROTOCOL
                for port in (entry.get("port"),)
                if isinstance(port, int) and port in _WORLD_OPEN_LB_PORTS
            }
        )
        if not exposed:
            continue
        named = ", ".join(f"{port}/TCP ({_WORLD_OPEN_LB_PORTS[port]})" for port in exposed)
        ranges = [str(r) for r in spec.get("loadBalancerSourceRanges") or []]
        ranges_clause = (
            f"loadBalancerSourceRanges {', '.join(ranges[:3])} that restrict nothing"
            if ranges
            else "no loadBalancerSourceRanges"
        )
        hits.append(
            {
                "namespace": meta.get("namespace", ""),
                "object": f"Service/{meta.get('name', '')}",
                "reconciler": reconciler_of(meta),
                "release": release_of(meta),
                "excerpt": (
                    f"type=LoadBalancer, no internal-LB annotation and "
                    f"{ranges_clause}, publishing {named} to 0.0.0.0/0"
                    + (_address_count_clause(assigned, public) if public else "; no address assigned yet")
                ),
            }
        )
    return hits


def _kcc_ready_condition(item: dict) -> dict | None:
    """The `Ready` condition off a Config Connector object, if it has one."""
    status = item.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    for condition in conditions or []:
        if isinstance(condition, dict) and condition.get("type") == KCC_READY_CONDITION:
            return condition
    return None


def check_kcc_object_wedged(context: dict) -> list[dict]:
    """Cluster-scoped: Config Connector objects stuck `Ready=False`.

    The audit reads GCP and reads the cluster. It never reads the thing that
    turns the GitOps repository into either of them, so a Config Connector that
    has stopped applying is invisible to every other check in this skill --
    while being the one condition that makes every `manifest` remediation the
    skill produces a no-op. `audit_report.disclose_stalled_kcc_remediations`
    covers the half where some finding's fix edits the stalled object's file.
    This covers the rest, which is most of it: nothing in the ledger has to
    point at an object for it to be wedged.

    Twice in two days on this fleet, and neither showed up anywhere a person
    was looking. On 2026-09-05 a `PubSubTopic` failed on a missing
    `roles/pubsub.editor` and took fifteen `ContainerCluster`s down with it. On
    2026-09-06 two `ComputeFirewall`s -- the merged remediation for a
    `firewall-world-open-ingress` critical -- retried a 403 every two minutes
    from 17:33 to 19:41 because the controller's service account had
    `compute.networkAdmin`, which grants the firewall read, and not
    `compute.securityAdmin`, which is where `compute.firewalls.update` lives.
    The rules stayed open to `0.0.0.0/0` on tcp:22 and tcp:3389 across fourteen
    nodes holding an external IP for four hours after the fix merged. Nothing
    crash-loops, no event lands outside the object, and the audit went on
    republishing the finding with no way to say why it was not clearing.

    One hit per *cause*. An object reporting `DependencyNotReady` is fine in
    itself and waiting on another object, so emitting one finding each would
    have turned that first incident into sixteen findings with one fix -- the
    flood this skill's `60k` body budget then spends on symptoms instead of
    causes. They are counted onto every cause's excerpt instead. The exception
    is a cluster where *every* unready object is a dependent: the cause is then
    not a Config Connector object at all (an external reference, or one deleted
    out from under its dependents), there is nothing else to name, and a
    `minor` hit each is the only way the reader hears about it.

    A dependent is only ever `DependencyNotReady`. `DependencyNotFound` reads
    like its neighbour and means the opposite thing for this check: the
    referent does not exist, so no other object here is going to be repaired
    into clearing it, and the object naming the missing reference *is* the
    cause. It stays a cause, and folding it in with the symptoms would drop the
    one object anybody can act on.
    """
    items = [i for i in context.get("kcc_objects") or [] if isinstance(i, dict)]
    causes: list[tuple[str, str, str, str]] = []  # kind, namespace, name, why
    blocked: list[tuple[str, str, str, str]] = []
    for item in items:
        ready = _kcc_ready_condition(item)
        # No condition yet means KCC has not reached the object, which is the
        # normal state for seconds after an apply and is not evidence of
        # anything. Only an explicit `False` is -- the same bar
        # `audit_report.kcc_resource_health` sets, for the same reason.
        if ready is None or str(ready.get("status")) != "False":
            continue
        meta = item.get("metadata") or {}
        kind = str(item.get("kind") or "")
        name = str(meta.get("name") or "")
        if not kind or not name:
            continue
        namespace = str(meta.get("namespace") or "")
        reason = str(ready.get("reason") or "").strip()
        message = str(ready.get("message") or "").strip()
        if reason and message:
            why = f"{reason}: {message}"
        else:
            why = reason or message or "no reason given"
        why = " ".join(why.split())[:MAX_KCC_WEDGE_MESSAGE_CHARS]
        entry = (kind, namespace, name, why)
        if reason == KCC_DEPENDENCY_NOT_READY_REASON:
            blocked.append(entry)
        else:
            causes.append(entry)

    if not causes and not blocked:
        return []

    def _tail(exclude: tuple[str, str, str, str] | None) -> str:
        others = [b for b in blocked if b != exclude]
        if not others:
            return ""
        named = ", ".join(f"{k}/{n}" for k, _, n, _ in sorted(others)[:MAX_KCC_BLOCKED_NAMED])
        if len(others) > MAX_KCC_BLOCKED_NAMED:
            named += f", +{len(others) - MAX_KCC_BLOCKED_NAMED} more"
        return (
            f"; {len(others)} other Config Connector object(s) here report "
            f"{KCC_DEPENDENCY_NOT_READY_REASON} behind an unready dependency ({named})"
        )

    symptoms_only = not causes
    hits = []
    for entry in sorted(causes if causes else blocked):
        kind, namespace, name, why = entry
        hit = {
            "namespace": namespace,
            "object": f"{kind}/{name}",
            "excerpt": (
                f"Config Connector reports {KCC_READY_CONDITION}=False on "
                f"{kind}/{name}: {why}" + _tail(entry)
            ),
        }
        if symptoms_only:
            # Symptom-only: see the docstring. Rated below a cause because the
            # object itself is correct and the fix is somewhere this read
            # cannot see, so the reader's first move is to find the dependency
            # rather than to edit this file.
            hit["severity"] = "minor"
            hit["impact"] = (
                "This Config Connector object is waiting on a dependency that is not itself a "
                "Config Connector object on this cluster, so nothing here declares the thing it "
                "needs. Its GCP resource holds whatever state it had when the reference broke, "
                "and any change merged to its manifest is not applied."
            )
        hits.append(hit)
    return hits


# A tag that moves. `latest` is the one everybody names; the other five are
# the branch- and channel-shaped tags that move exactly as often and get
# treated as though they do not. Deliberately not a general "does this tag
# look like a version" heuristic: `v2`, `stable-3` and `python3.12` are all
# mutable in practice and all indistinguishable from a pin by shape, so a
# heuristic either misses them or flags every tag in the fleet. This list is
# the set that can be asserted.
FLOATING_TAG_RE = re.compile(r":(latest|main|master|dev|nightly|stable|edge)$")
# An image reference with no tag at all, which the runtime resolves to
# `:latest`. The registry host may carry a port (`registry:5000/app`), so the
# tag is only the part after the last `/`.
UNTAGGED_IMAGE_RE = re.compile(r":[^/]*$")
DIGEST_RE = re.compile(r"@sha256:")
# What joins a reference to the digest an admission webhook may have added to it.
DIGEST_SEPARATOR = "@"
# `status.containerStatuses[].imageID` is not always a digest reference. A
# locally-loaded image (kind, minikube, `docker save`) records `docker://` or
# a bare id, and pinning to one of those produces a manifest no other node can
# pull -- worse than the floating tag it replaced.
_IMAGE_ID_DIGEST_RE = re.compile(r"^(?:docker-pullable://)?(?P<ref>[^@]+@sha256:[0-9a-f]{64})$")

_IMPACT_IMAGE_FLOATING = (
    "The manifest does not say which bytes run here, so every restart, "
    "rescale, node replacement and preemption re-resolves the tag and can "
    "bring back different code with no deploy and no record. A rollback "
    "resolves the same reference, so it does not go back."
)
_IMPACT_IMAGE_SPLIT = (
    "This workload's live pods are already running different builds of "
    "{containers} at the same time, because the tag was re-resolved when "
    "a pod restarted, rescaled or was replaced. Which version a request gets "
    "depends on which pod it lands on, and no deploy was made to cause it."
)
_IMPACT_IMAGE_REVISION_DRIFT = (
    "This workload's live pods are running different builds of {containers} "
    "under one image reference: a template change made a new revision without "
    "changing the reference, and the tag resolved to different bytes for it. "
    "The revision was deployed on purpose; the bytes it brought were not chosen "
    "by anyone, and which build a request gets depends on which pod it lands on."
)


def _running_digests(pod: dict) -> dict[str, str]:
    """`{container name: fully-qualified digest reference}` for this pod.

    Only entries that are a real registry digest reference; see
    `_IMAGE_ID_DIGEST_RE`. Init containers are included because they are
    pinned by the same edit and fail the same way.
    """
    status = pod.get("status") or {}
    out = {}
    for cs in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
        match = _IMAGE_ID_DIGEST_RE.match(str(cs.get("imageID") or ""))
        if match:
            out[cs.get("name", "")] = match.group("ref")
    return out


def _pod_image_refs(pod: dict) -> dict[str, str]:
    """`{container name: image reference as the pod spec writes it}`, init containers included."""
    spec = pod.get("spec") or {}
    return {
        c.get("name", ""): str(c.get("image") or "")
        for c in (spec.get("containers") or []) + (spec.get("initContainers") or [])
    }


#: The pod labels a controller stamps with the pod template revision a pod was
#: created from: a Deployment's ReplicaSet, and a StatefulSet's or DaemonSet's
#: ControllerRevision.
_POD_REVISION_LABELS = ("pod-template-hash", "controller-revision-hash")


def _pod_revision(pod: dict) -> str:
    """Which pod-template revision this pod came from, for grouping its digests.

    Pods of two revisions running two digests is a rollout in progress -- the
    template changed, and possibly the image with it -- not a tag re-resolving
    under one template. Falls back to the controlling owner's name, which is a
    ReplicaSet's for a Deployment and so names the revision as well.
    """
    labels = pod.get("labels") or {}
    for label in _POD_REVISION_LABELS:
        if labels.get(label):
            return labels[label]
    owners = pod.get("owners") or []
    return owners[0].get("name", "") if owners else ""


def _workload_running_images(
    workload: dict, context: dict
) -> tuple[dict[str, dict[str, set[str]]], dict[str, dict[str, set[str]]]]:
    """`({container: {revision: digests}}, {container: {revision: image references}})` over the live pods.

    Joined through the same owner-reference walk `netpol-missing` uses, so a
    Deployment's pods reach it through their ReplicaSet, in one pass because
    that walk is the expensive part.

    Digests are a set because the size of it is the finding's severity. One
    digest means the tag has resolved consistently and the pin is a no-op
    diff; more than one means the same container is running different bytes
    on different nodes *right now*. Keyed by revision (`_pod_revision`)
    because a split across revisions can be a rollout that changed the image,
    and the references the pods' specs write are what tells the two apart: a
    rollout leaves two references live, while one reference resolving to two
    digests is the tag moving under a template-only change -- an env edit, a
    `rollout restart` -- which is the drift itself. References are keyed by
    revision too, so a rollout's old revision beside two restarted ones does
    not hide the drift between the two.

    A reference carrying a digest an admission webhook added -- appended to
    the tag, or in place of it -- is read as the template's reference when
    the repository is the same, so a digest-pinning policy does not make
    every pod look as though it runs another reference than its template.
    """
    templates = {
        c.get("name", ""): c.get("image") or ""
        for c in (workload["spec"].get("containers") or []) + (workload["spec"].get("initContainers") or [])
    }
    digests: dict[str, dict[str, set[str]]] = {}
    refs: dict[str, dict[str, set[str]]] = {}
    ref = f"{workload['kind']}/{workload['name']}"
    for pod in context.get("pods") or []:
        if pod.get("ns") != workload["ns"] or not _is_live_pod(pod):
            continue
        if _pod_workload_ref(pod, context.get("workloads") or []) != ref:
            continue
        revision = _pod_revision(pod)
        for name, digest in (pod.get("images") or {}).items():
            digests.setdefault(name, {}).setdefault(revision, set()).add(digest)
        for name, image in (pod.get("image_refs") or {}).items():
            if DIGEST_SEPARATOR in image:
                base = image.split(DIGEST_SEPARATOR, 1)[0]
                template = templates.get(name, "")
                image = template if base in (template, UNTAGGED_IMAGE_RE.sub("", template)) else base
            refs.setdefault(name, {}).setdefault(revision, set()).add(image)
    return digests, refs


def check_image_floating_tag(workload: dict, context: dict) -> dict | None:
    """§2.13. A container whose image reference does not name specific bytes.

    Every restart, rescale, node replacement and preemption re-resolves the
    tag, so the pod that comes back can be running different code from the one
    that went away -- with no deploy, no change to the manifest, and nothing
    in the cluster recording that it happened. The same reference is what a
    rollback resolves, so rolling back does not go back either.

    The excerpt carries the digest the tag currently resolves to, where the
    live pods agree on one, because that digest is what turns this from a
    request the reviewer has to research into a diff that changes no bytes.
    """
    running, image_refs = _workload_running_images(workload, context)
    bad, split, moved = [], [], []
    for container in (workload["spec"].get("containers") or []) + (workload["spec"].get("initContainers") or []):
        image = container.get("image") or ""
        if not image or DIGEST_RE.search(image):
            continue
        if not (FLOATING_TAG_RE.search(image) or not UNTAGGED_IMAGE_RE.search(image)):
            continue
        name = container.get("name", "")
        by_revision = running.get(name) or {}
        digests = sorted(set().union(*by_revision.values())) if by_revision else []
        refs_by_revision = image_refs.get(name) or {}
        live_refs = set().union(*refs_by_revision.values()) if refs_by_revision else set()
        # Revisions whose pods all write the template's reference: across
        # those, more than one digest is the tag moving, not a rollout.
        same_ref = [rev for rev, refs in refs_by_revision.items() if refs == {image} and rev in by_revision]
        same_ref_digests = sorted(set().union(*(by_revision[rev] for rev in same_ref))) if same_ref else []
        split_digests = sorted(set().union(*(d for d in by_revision.values() if len(d) > 1))) if by_revision else []
        if split_digests:
            split.append(name)
            # Only the split revisions' digests: a rollout's older revision
            # beside them runs another reference, and pinning to it reverts.
            stale = (
                f"; the pods still write {', '.join(sorted(live_refs))}, not this reference"
                if live_refs and image not in live_refs
                else ""
            )
            bad.append(
                f"{name}: {image} -> live pods are split across {len(split_digests)} digests: "
                f"{', '.join(split_digests)}{stale}"
            )
        elif live_refs and image not in live_refs:
            # The template names a reference no live pod runs yet -- an
            # `OnDelete` StatefulSet or DaemonSet, a paused Deployment after
            # `set image`. The running digest belongs to the old reference, so
            # pinning to it would undo the change rather than change no bytes.
            bad.append(
                f"{name}: {image} -> no live pod runs this reference yet; they still write "
                f"{', '.join(sorted(live_refs))}"
            )
        elif len(digests) == 1:
            bad.append(f"{name}: {image} -> currently running {digests[0]}")
        elif len(same_ref_digests) > 1:
            # One digest per revision, but revisions writing the template's
            # reference disagree: the template changed without the image, and
            # the tag resolved differently at each revision's pull. That is
            # the drift.
            moved.append(name)
            bad.append(
                f"{name}: {image} -> {len(same_ref)} pod-template revisions live, all writing this "
                f"reference, resolved to {len(same_ref_digests)} digests: {', '.join(same_ref_digests)}"
            )
        elif digests:
            # One digest per revision under different references: a rollout
            # mid-flight that changed the image, which proves no drift, so it
            # stays at the unpinned-reference severity.
            bad.append(
                f"{name}: {image} -> {len(by_revision)} pod-template revisions live, "
                f"one digest each: {', '.join(digests)}"
            )
        else:
            bad.append(f"{name}: {image} (no running pod to read a digest from)")
    if not bad:
        return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": "; ".join(bad),
        # Severity is proven drift, not the unpinned reference itself. An
        # unpinned tag is everywhere in every fleet, and grading all of it
        # `major` would flood the promotion queue with hygiene while burying
        # the workloads whose pods have *already* diverged.
        "severity": "major" if split or moved else "minor",
        "impact": (
            _IMPACT_IMAGE_SPLIT.format(containers=", ".join(split))
            if split
            else _IMPACT_IMAGE_REVISION_DRIFT.format(containers=", ".join(moved))
            if moved
            else _IMPACT_IMAGE_FLOATING
        ),
    }


class CheckSpec(NamedTuple):
    slug: str
    kind: str  # "workload": run(workload, context) -> hit|None, one call per workload.
    #             "cluster": run(context) -> list[hit], one call per cluster.
    run: Callable
    severity: str  # A hit's own "severity" key overrides this (§3.4, §3.6, §3.7's two-condition checks).
    autopilot_severity: str | None  # None: severity is mode-independent
    impact: str  # A hit's own "impact" key overrides this (§3.1's non-BestEffort arms).


OBTAINABILITY_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "no-requests",
        "workload",
        check_no_requests,
        "major",
        "minor",
        # Every arm of §3.1 sets its own, so this is unreachable in practice;
        # it stays as the BestEffort arm's own string rather than a fourth
        # sentence nothing produces.
        _IMPACT_BEST_EFFORT,
    ),
    CheckSpec(
        "no-memory-limit",
        "workload",
        check_no_memory_limit,
        "major",
        "minor",
        # Both arms of §3.2 set their own, so this is unreachable in practice.
        # It stays as the arm that describes a container with no memory request
        # -- the shape the fleet is actually made of -- rather than a third
        # sentence nothing produces.
        _IMPACT_NO_LIMIT_UNREQUESTED,
    ),
    CheckSpec(
        "no-pdb",
        "workload",
        check_no_pdb,
        "major",
        None,
        "Nothing constrains the eviction API, so a single node drain during "
        "an upgrade can terminate every replica at once.",
    ),
    CheckSpec(
        "blocking-pdb",
        "cluster",
        check_blocking_pdb,
        "critical",
        None,
        "Blocks every node drain in this cluster indefinitely: node-pool "
        "upgrades, node auto-repair, and autoscaler scale-down all stall "
        "until a human deletes or edits this PDB.",
    ),
    CheckSpec(
        "pdb-overlapping",
        "cluster",
        check_pdb_overlapping,
        "critical",
        None,
        _IMPACT_PDB_OVERLAPPING,
    ),
    CheckSpec(
        "no-hpa",
        "workload",
        check_no_hpa,
        "minor",
        None,
        "Capacity is pinned at a hand-chosen number: the workload cannot "
        "absorb a traffic spike and cannot give capacity back when idle.",
    ),
    CheckSpec(
        "hpa-cannot-scale",
        "cluster",
        check_hpa_cannot_scale,
        "major",  # overridden per hit; see check_hpa_cannot_scale
        None,
        "An HPA is attached but cannot scale this workload in either "
        "direction, or targets an object that no longer exists.",
    ),
    CheckSpec(
        "hpa-floors-at-one",
        "cluster",
        check_hpa_floors_at_one,
        "minor",
        None,
        "The autoscaler is allowed to take this Service-backed workload down "
        "to one pod, and at that floor every rollout, node drain, and "
        "preemption is a full outage -- during exactly the quiet hours the "
        "floor is reached.",
    ),
    CheckSpec(
        "rigid-scheduling",
        "workload",
        check_rigid_scheduling,
        "major",  # overridden per hit: critical for a hostname pin
        None,
        "This pod's scheduling is pinned to one node or one zone: the next "
        "node upgrade, repair, or zonal event takes it down and it may not "
        "come back.",
    ),
    CheckSpec(
        "no-spread",
        "workload",
        check_no_spread,
        "minor",
        None,
        "Nothing guarantees these replicas are on different nodes; losing "
        "one node can take the whole workload out despite the replica count.",
    ),
    CheckSpec(
        "probes-readiness",
        "workload",
        check_probes_readiness,
        "major",
        None,
        "Every rollout sends production traffic to pods that are not yet "
        "serving, and a broken new version is never detected as broken.",
    ),
    CheckSpec(
        "probes-liveness",
        "workload",
        check_probes_liveness,
        "minor",
        None,
        "A wedged process is never restarted automatically; recovery "
        "requires a human.",
    ),
    CheckSpec(
        "single-replica",
        "workload",
        check_single_replica,
        "minor",
        None,
        "Zero-downtime is impossible: every rollout, node drain, and node "
        "repair is a full outage for this service.",
    ),
    CheckSpec(
        "schedule-never-succeeds",
        "cluster",
        check_schedule_never_succeeds,
        "major",
        None,
        "Whatever this schedule exists to do has not been done since its last "
        "successful run, and nothing reports that on its own: the CronJob is "
        "still listed, still enabled, and still firing on time.",
    ),
    CheckSpec(
        "rollout-drops-traffic",
        "workload",
        check_rollout_drops_traffic,
        "major",
        None,
        "Every rollout, node drain, and preemption resets the connections in "
        "flight to the terminating pod: the container is sent SIGTERM while "
        "the endpoint removal is still propagating, so requests arrive at a "
        "process that has already begun shutting down.",
    ),
    CheckSpec(
        "strategy-causes-downtime",
        "workload",
        check_strategy_causes_downtime,
        "major",
        None,
        "The update strategy permits every replica to be down at once, so a "
        "rollout is a full outage lasting until the first replacement pod "
        "passes its readiness probe -- an image pull plus a cold start.",
    ),
    CheckSpec(
        "cronjob-runs-overlap",
        "cluster",
        check_cronjob_runs_overlap,
        "major",
        None,
        "Runs of this schedule are piling up on each other: each period starts "
        "another copy without the last one having finished, so the load on "
        "whatever the job writes to grows until something there gives out.",
    ),
    CheckSpec(
        "service-selects-nothing",
        "cluster",
        check_service_selects_nothing,
        "major",
        None,
        "This Service resolves to no pod, so every caller that reaches it -- by "
        "DNS name, by cluster IP, or through whatever is pointed at its "
        "external address -- has its connection refused. The Service object "
        "itself stays healthy, which is why nothing has reported this.",
    ),
    CheckSpec(
        "service-port-unresolved",
        "cluster",
        check_service_port_unresolved,
        "major",
        None,
        "This Service asks for a port by a name none of its pods declare, so "
        "the endpoint controller left that port out and kube-proxy programmed "
        "nothing for it. The pods are up and the Service lists their "
        "addresses, so every check anyone would run says it is healthy while "
        "the port refuses.",
    ),
    CheckSpec(
        "liveness-preempts-readiness",
        "workload",
        check_liveness_preempts_readiness,
        "major",
        None,
        _IMPACT_LIVENESS_PREEMPTS,
    ),
    CheckSpec(
        "spread-not-achieved",
        "workload",
        check_spread_not_achieved,
        # `major`, where §3.8's hypothetical is `minor`: this one has already
        # happened. The replicas are on one node now, so the workload has the
        # availability of a single pod whatever its replica count says.
        "major",
        None,
        _IMPACT_SPREAD_NOT_ACHIEVED,
    ),
    CheckSpec(
        "prestop-outlives-grace",
        "workload",
        check_prestop_outlives_grace,
        # `major`, matching §3.13, because the workload is in the state §3.13
        # describes -- the hook does not run, so nothing it was added to
        # prevent is prevented. Rated no lower for having a fix already in the
        # manifest: a defect with its own remediation applied and inert is
        # harder to find, not less costly, and §3.13 is not reporting it.
        "major",
        None,
        _IMPACT_PRESTOP_TRUNCATED,
    ),
    CheckSpec(
        "rwo-claim-contended",
        "workload",
        check_rwo_claim_contended,
        # `major` on both arms, and the hit carries the Impact that says which
        # one. Not `critical`: nothing is down. The rollout arm leaves the old
        # pod serving indefinitely and the replica arm leaves one pod serving,
        # so in both the workload answers requests while being unable to change
        # or to grow. Not `minor` either -- a Deployment that cannot take an
        # update is one no patch reaches, and it says nothing about it.
        "major",
        None,
        # Overridden per hit; kept here because `CheckSpec` requires one and a
        # placeholder would ship if an arm ever forgot its own.
        _IMPACT_RWO_ROLLOUT_DEADLOCK,
    ),
)

COMPLIANCE_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "privileged-container",
        "workload",
        check_privileged_container,
        "critical",
        None,
        "Container has full host device and kernel access; compromising this "
        "workload compromises the node.",
    ),
    CheckSpec(
        "host-namespace",
        "workload",
        check_host_namespace,
        "major",  # overridden per hit: critical for hostPID/hostIPC
        None,
        # Every hit composes its own from the flags actually set, so this is
        # unreachable in practice; it stays as the arm that carries the check's
        # own severity default rather than a fourth sentence nothing produces.
        _IMPACT_HOST_NETWORK,
    ),
    CheckSpec(
        "hostpath-mount",
        "workload",
        check_hostpath_mount,
        "major",  # overridden per hit: critical for a sensitive or writable path
        None,
        "Workload mounts a node filesystem path, giving it access to state "
        "belonging to the node and to other tenants' pods.",
    ),
    CheckSpec(
        "cluster-admin-binding",
        "cluster",
        check_cluster_admin_binding,
        "critical",  # overridden per hit: minor for an org-email Group
        None,
        "Subject holds unrestricted read/write on every resource in the "
        "cluster, including Secrets in every namespace.",
    ),
    CheckSpec(
        "wildcard-rbac",
        "cluster",
        check_wildcard_rbac,
        "critical",  # overridden per hit: major for a namespaced Role
        None,
        "Subject holds write access to every resource in this scope — enough "
        "to read Secrets in any namespace it covers and rewrite a workload "
        "into a privileged pod. Wildcarding the verbs and enumerating them "
        "reach the same ceiling.",
    ),
    CheckSpec(
        "netpol-missing",
        "cluster",
        check_netpol_missing,
        "major",  # overridden per hit: minor for allow-all-only
        None,
        "Every pod in this namespace accepts traffic from every pod in the "
        "cluster; a compromise anywhere reaches these workloads unimpeded.",
    ),
    CheckSpec(
        "default-sa-automount",
        "cluster",
        check_default_sa_automount,
        "major",
        None,
        # Not "a credential it does not use": the check reads the SA reference
        # and the automount flag, and neither says whether the workload calls
        # the API server. A finding that asserts an unobservable is one an owner
        # can refute from memory, and refuting a true finding on a false clause
        # is how a whole audit stops being read.
        "Workload mounts an API-server credential by default rather than by "
        "request, handing an attacker who lands in the container an "
        "authenticated foothold for free.",
    ),
    CheckSpec(
        "workload-identity-off",
        "cluster",
        check_workload_identity_off,
        "critical",
        None,
        "All pods on this cluster share the node service account's Google "
        "Cloud permissions; there is no per-workload IAM boundary.",
    ),
    CheckSpec(
        "legacy-metadata",
        "cluster",
        check_legacy_metadata,
        "critical",
        None,
        "Any pod on this node pool can read the node service account's "
        "access token from the legacy metadata endpoint and escalate to "
        "that identity's full Google Cloud permissions.",
    ),
    CheckSpec(
        "public-control-plane",
        "cluster",
        check_public_control_plane,
        "critical",
        None,
        # Both arms set their own, so this is unreachable in practice; it stays
        # as the IP-endpoint arm rather than a third sentence nothing produces.
        _IMPACT_PUBLIC_IP_ENDPOINT,
    ),
    CheckSpec(
        "podsecurity-gaps",
        "workload",
        check_podsecurity_gaps,
        "minor",
        None,
        "Containers miss the container-level settings the restricted Pod "
        "Security Standard requires: running as root, an unfiltered syscall "
        "surface, retained Linux capabilities, or privilege escalation left "
        "enabled. A runtime escape starts with capabilities to use rather "
        "than having to acquire them.",
    ),
    CheckSpec(
        "kcc-object-wedged",
        "cluster",
        check_kcc_object_wedged,
        "critical",
        None,
        # Overridden on the symptom-only arm; see check_kcc_object_wedged.
        "Config Connector has stopped applying this declaration. The GCP "
        "resource holds whatever state it drifted to, the repository says "
        "something else, and every pull request merged against this object -- "
        "including a remediation this audit proposes -- changes nothing on the "
        "cloud until the controller is unblocked.",
    ),
    CheckSpec(
        "image-floating-tag",
        "workload",
        check_image_floating_tag,
        "minor",  # overridden per hit: major where the live pods already disagree
        None,
        _IMPACT_IMAGE_FLOATING,
    ),
    CheckSpec(
        "unbound-sa-automount",
        "cluster",
        check_unbound_sa_automount,
        "major",
        None,
        _IMPACT_UNBOUND_SA_AUTOMOUNT,
    ),
    CheckSpec(
        "lb-world-open",
        "cluster",
        check_lb_world_open,
        "critical",
        None,
        _IMPACT_LB_WORLD_OPEN,
    ),
    CheckSpec(
        "anonymous-rbac-binding",
        "cluster",
        check_anonymous_rbac_binding,
        "critical",
        None,
        # Every hit carries its own, because the two arms grant to different
        # sets of callers and a sentence true of one is false of the other.
        # This is the anonymous arm's, which is the one a reader reaches for.
        _IMPACT_ANONYMOUS_RBAC_ANONYMOUS,
    ),
)

AI_SECURITY_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "inference-endpoint-public",
        "cluster",
        check_inference_endpoint_public,
        "critical",
        None,
        "This model server is reachable from the public internet. Anyone who finds the address can "
        "send it inference traffic, consume its accelerator capacity, and probe whatever the model "
        "can reach.",
    ),
    CheckSpec(
        "model-remote-code-trusted",
        "workload",
        check_model_remote_code_trusted,
        "critical",
        None,
        "The model loader executes arbitrary code shipped inside the model repository, with this "
        "pod's ServiceAccount, network access, and mounted volumes. A compromised or swapped model "
        "artifact is remote code execution in this namespace.",
    ),
    CheckSpec(
        "weights-mount-writable",
        "workload",
        check_weights_mount_writable,
        "major",
        None,
        "The serving process can overwrite its own model weights. Any code execution in this pod "
        "becomes a persistent, replica-wide model swap that outlives the compromised pod and is "
        "invisible to an image scanner.",
    ),
    CheckSpec(
        "model-artifact-unpinned-source",
        "workload",
        check_model_artifact_unpinned_source,
        "major",  # overridden per hit: critical alongside a model-remote-code-trusted finding on the same container
        None,
        "The model artifact this container loads is not pinned: the bytes that arrive at the next "
        "pod restart are whatever the source serves then. Nothing in the manifest records which "
        "model is actually running.",
    ),
    CheckSpec(
        "model-credential-plaintext-env",
        "workload",
        check_model_credential_plaintext_env,
        "major",
        None,
        "A model-registry credential is embedded in this workload's pod spec in plaintext. It is "
        "visible to anyone who can describe the pod or read the manifest in Git, and it is not "
        "rotatable without a redeploy.",
    ),
    CheckSpec(
        "model-image-floating-tag",
        "workload",
        check_model_image_floating_tag,
        "minor",
        None,
        "The image this container runs is not reproducible: a restart can pull different bytes than "
        "the ones running now, with no manifest change to review.",
    ),
)

CHECK_TABLES: dict[str, tuple[CheckSpec, ...]] = {
    "obtainability-audit": OBTAINABILITY_CHECKS,
    "compliance-audit": COMPLIANCE_CHECKS,
    "ai-security-audit": AI_SECURITY_CHECKS,
}


# --------------------------------------------------------------------------- #
# Orchestration
#
# A stream's *context builder* owns how it collects (one dump, or several —
# compliance-audit's RBAC/NetworkPolicy/ServiceAccount/gcloud reads have no
# single-dump shape to share); `collect_cluster` owns what every stream does
# with the result once collected, which is identical regardless of shape:
# run each check, apply the autopilot/per-hit severity rule, emit candidates.
# --------------------------------------------------------------------------- #


class CollectedContext(NamedTuple):
    context: dict
    workloads: list[dict]  # for "workload"-kind checks; [] for a stream with none
    commands: dict[str, dict]  # check slug -> {command, rc, duration_s, output_sha256}


def _record(argv_str: str, result: Run) -> dict:
    return {
        "command": argv_str,
        "rc": result.rc,
        "duration_s": round(result.duration_s, 2),
        "output_sha256": output_digest(result.stdout),
    }


def _collect_obtainability(cluster: dict, kubeconfig: Path, checks: tuple[CheckSpec, ...], *, run: RunFn) -> CollectedContext:
    dump_path, dump_run, gate_ok = dump_state(
        kubeconfig, cluster["name"], project=cluster["project"], location=cluster["location"], run=run
    )
    if not gate_ok:
        raise GateFailure(f"dump gate failed (rc={dump_run.rc}): {dump_run.stderr.strip()[:ERROR_EXCERPT_CHARS]}")
    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    workloads = normalize_workloads(dump)
    record = _record(f"KUBECONFIG={kubeconfig} kubectl get {DUMP_COMMAND_KINDS} -A -o json", dump_run)
    return CollectedContext(build_context(dump, workloads), workloads, {spec.slug: record for spec in checks})


# check slug -> which named collection(s) it reads. Only the keys are used:
# a slug listed here gets its `commands` record from the collection that feeds
# it, and every slug not listed is attributed to the workload dump. The values
# document the dependency; they do not isolate failures -- every gate failure
# raises GateFailure for the whole cluster, whichever collection it hit.
_COMPLIANCE_CHECK_SOURCES: dict[str, tuple[str, ...]] = {
    "cluster-admin-binding": ("rbac",),
    "wildcard-rbac": ("rbac",),
    "anonymous-rbac-binding": ("rbac",),
    "netpol-missing": ("netpol", "namespaces", "workloads", "ccnp"),
    "default-sa-automount": ("serviceaccounts", "workloads"),
    "unbound-sa-automount": ("serviceaccounts", "rbac", "workloads"),
    "workload-identity-off": ("describe",),
    "legacy-metadata": ("node_pools",),
    "public-control-plane": ("describe",),
    "kcc-object-wedged": ("kcc",),
    "lb-world-open": ("services",),
}

# `kubectl get gcp` is the category Config Connector registers across every CRD
# it installs, so one read returns every object it owns -- rather than the
# ~200 `kubectl get <kind>` calls that enumerating the CRDs would cost.
KCC_CATEGORY = "gcp"

DEFAULT_SERVICE_ACCOUNT = "default"

# The four Autopilot CRDs that can lift the admission rules
# `_COMPLIANCE_AUTOPILOT_NOT_APPLICABLE` rests on.
AUTOPILOT_ALLOWLIST_KINDS = (
    "workloadallowlists.auto.gke.io",
    "allowlistsynchronizers.auto.gke.io",
    "allowlistedworkloads.auto.gke.io",
    "allowlistedv2workloads.auto.gke.io",
)

# The one read in this file that does not fit `DEFAULT_TIMEOUT_S`, because a
# category read is not one list call. `kubectl` expands `gcp` to every CRD
# carrying the category -- 221 of them on this fleet's hub -- and issues a list
# against each, so the cost scales with how much of Config Connector is
# installed rather than with how many objects exist. Measured from the agent
# pod against an idle hub holding 19 objects: 10-14 seconds.
#
# That fits inside 60 comfortably, which is why the check looked fine until it
# ran for real. The collector sweeps `MAX_WORKERS` clusters at once, and the
# fifteen without Config Connector answer this read in about a tenth of a
# second, so the hub is the only one that is still working when the other seven
# in its wave are hammering the same pod -- and on 2026-09-06, the first day
# the check was live, it went past 60 and was recorded as inapplicable. The
# check has never once executed against the only cluster it can apply to.
#
# 240 is four times the default and roughly twenty times the idle read. It is
# affordable because it is one read on one cluster: the other fifteen fail
# fast, so the worst case adds minutes to a sweep that already runs for half an
# hour, and only when something is genuinely wrong.
KCC_READ_TIMEOUT_S = 240

# §1's three node-facing checks Autopilot's admission controller rules out
# for in-scope workloads, with the SOP's own canonical reasons verbatim. On
# an Autopilot cluster these must never appear in the manifest's `commands`
# -- an agent that copies `commands` verbatim into `checks_run` (§2's
# instruction for every other check) and *also* follows §1's instruction to
# record these in `checks_not_applicable` would name the same slug in both
# lists, which the validator rejects.
#
# Each reason is scoped two ways on purpose, because the earlier wording
# ("privileged containers are rejected at admission and cannot exist here")
# was false on both counts. Privileged containers, host namespaces and
# hostPath mounts all run on this fleet's Autopilot clusters today -- they
# are Google's own system add-ons in kube-system, which S1 and S3 suppress,
# so "in-scope workloads" rather than a claim about the cluster. And
# admission rejecting them is a policy a WorkloadAllowlist can lift
# (`autogke-disallow-privilege`, `autogke-no-write-mode-hostpath`, GKE >=
# 1.32.0-gke.1000000), so the trailing clause is a fact the collector reads
# for -- see `autopilot_allowlists` -- rather than one it assumes.
_COMPLIANCE_AUTOPILOT_NOT_APPLICABLE: tuple[tuple[str, str], ...] = (
    (
        "privileged-container",
        "GKE Autopilot: admission rejects privileged: true and the SYS_ADMIN capability for "
        "in-scope workloads, and this cluster carries no WorkloadAllowlist that would exempt one.",
    ),
    (
        "host-namespace",
        "GKE Autopilot: admission rejects hostPID/hostIPC/hostNetwork for in-scope workloads, "
        "and this cluster carries no WorkloadAllowlist that would exempt one.",
    ),
    (
        "hostpath-mount",
        "GKE Autopilot: admission rejects write-mode hostPath for in-scope workloads and allows "
        "read access under /var/log alone, and this cluster carries no WorkloadAllowlist "
        "that would exempt one.",
    ),
)


def _collect_compliance(cluster: dict, kubeconfig: Path, checks: tuple[CheckSpec, ...], *, run: RunFn) -> CollectedContext:
    name, project, location = cluster["name"], cluster["project"], cluster["location"]
    commands: dict[str, dict] = {}
    # `cluster_name` is here for the two cluster-scoped checks, whose object is
    # the cluster itself and which are handed nothing else that names it.
    context: dict = {"workloads": [], "cluster_name": name}

    def gated(argv: list[str]) -> tuple[dict | list | None, Run]:
        return run_and_gate(argv, kubeconfig, run=run)

    # Every collection below is gate-checked independently, and a gate
    # failure raises immediately — this stream fails the whole cluster
    # closed on any missing input, the same as a single-dump stream, per
    # `GateFailure`'s own docstring.
    workload_argv = ["kubectl", "get", COMPLIANCE_DUMP_KINDS, "-A", "-o", "json"]
    parsed, result = gated(workload_argv)
    if parsed is None:
        raise GateFailure(f"workload dump gate failed (rc={result.rc}): {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}")
    context["workloads"] = normalize_compliance_workloads(parsed)
    # Raw, from the same dump: `netpol-missing`'s exposure test asks whether a
    # namespace runs pods, which is not the same question as whether it holds
    # anything this audit is allowed to name.
    # Live pods only, as the name promises: a namespace holding nothing but a
    # finished Job pod exposes nothing, which `_is_live_pod` already says.
    context["pod_namespaces"] = {
        (i.get("metadata") or {}).get("namespace", "")
        for i in parsed.get("items", []) or []
        if i.get("kind") == "Pod" and _is_live_pod({"phase": (i.get("status") or {}).get("phase")})
    }
    # The same pods again, with the labels kept. "Does this namespace hold a
    # NetworkPolicy" and "is this pod selected by one" are different questions,
    # and only the labels can answer the second. The owner refs answer a third:
    # which object a reader is supposed to go and fix, since a pod name is a
    # hash away from stable and a label value names nothing at all. `images`
    # answers a fourth, for §2.13: the digest a floating tag resolved to at the
    # last pull, which is the one value that turns "pin this image" from a
    # request the reviewer has to research into a diff that changes no bytes.
    context["pods"] = [
        {
            "ns": (i.get("metadata") or {}).get("namespace", ""),
            "name": (i.get("metadata") or {}).get("name", ""),
            "labels": (i.get("metadata") or {}).get("labels") or {},
            "phase": (i.get("status") or {}).get("phase", ""),
            "owners": _controller_refs(i.get("metadata") or {}),
            "images": _running_digests(i),
            "image_refs": _pod_image_refs(i),
        }
        for i in parsed.get("items", []) or []
        if i.get("kind") == "Pod"
    ]
    workload_record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(workload_argv)}", result)
    for spec in checks:
        if spec.slug not in _COMPLIANCE_CHECK_SOURCES:
            commands[spec.slug] = workload_record

    rbac_argv = ["kubectl", "get", "clusterroles,roles,clusterrolebindings,rolebindings", "-A", "-o", "json"]
    parsed, result = gated(rbac_argv)
    if parsed is None:
        raise GateFailure(f"RBAC dump gate failed (rc={result.rc}): {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}")
    items = parsed.get("items", [])
    context["roles"] = [i for i in items if i.get("kind") in ("ClusterRole", "Role")]
    context["clusterrolebindings"] = [i for i in items if i.get("kind") == "ClusterRoleBinding"]
    context["rolebindings"] = [i for i in items if i.get("kind") == "RoleBinding"]
    record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(rbac_argv)}", result)
    for slug in ("cluster-admin-binding", "wildcard-rbac", "anonymous-rbac-binding"):
        commands[slug] = record

    netpol_argv = ["kubectl", "get", "netpol,ns", "-A", "-o", "json"]
    parsed, result = gated(netpol_argv)
    if parsed is None:
        raise GateFailure(f"NetworkPolicy/Namespace dump gate failed (rc={result.rc}): {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}")
    items = parsed.get("items", [])
    context["networkpolicies"] = [i for i in items if i.get("kind") == "NetworkPolicy"]
    context["namespaces"] = [i for i in items if i.get("kind") == "Namespace"]
    commands["netpol-missing"] = _record(f"KUBECONFIG={kubeconfig} {shlex.join(netpol_argv)}", result)

    # Gated like the reads above rather than treated as optional: every cluster
    # has a Services API, so a failure here is a broken read and not an absent
    # feature, and `check_lb_world_open` reading an empty list would report a
    # cluster clean on a collection it never saw.
    service_argv = ["kubectl", "get", "svc", "-A", "-o", "json"]
    parsed, result = gated(service_argv)
    if parsed is None:
        raise GateFailure(f"Service dump gate failed (rc={result.rc}): {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}")
    context["services"] = parsed.get("items", []) or []
    commands["lb-world-open"] = _record(f"KUBECONFIG={kubeconfig} {shlex.join(service_argv)}", result)

    # A deliberate exception to "every read above raises": Dataplane V2's
    # `ClusterNetworkPolicy` CRD (§2.6's Do-NOT-flag case, `kubectl get ccnp
    # -o name`) is not installed on every cluster, so a failure here almost
    # always means "this cluster has no such CRD," not "this input is
    # missing." Gating the whole cluster closed on that would fail every
    # compliance-audit run on a cluster without the CRD -- worse than the
    # false positive it exists to suppress. Absence reads as zero
    # ClusterNetworkPolicies, the same posture as before this read existed.
    ccnp_argv = ["kubectl", "get", "ccnp", "-A", "-o", "json"]
    ccnp_parsed, ccnp_result = run_and_gate(ccnp_argv, kubeconfig, run=run)
    context["cluster_network_policies"] = [i for i in (ccnp_parsed or {}).get("items", [])] if ccnp_parsed else []
    # Only the API server's own "not served" answer is absence. A forbidden or
    # timed-out read on a Dataplane V2 cluster would otherwise report every
    # namespace a cluster-wide policy covers, and resolve them on the next
    # good read.
    if ccnp_parsed is None and RESOURCE_TYPE_ABSENT_MARKER not in ccnp_result.stderr:
        stderr = ccnp_result.stderr.strip()[:ERROR_EXCERPT_CHARS] or "no stderr"
        context.setdefault("unevaluated", {})["netpol-missing"] = (
            f"{UNDETERMINED_PREFIX} `kubectl get ccnp -A` exited {ccnp_result.rc} "
            f"without saying the type is unserved ({stderr}), so whether a "
            "cluster-wide policy covers these namespaces was not established. "
            "This check cleared nothing on this cluster."
        )

    # Ungated for the reason `ccnp` above is: Config Connector is installed on
    # the hub that reconciles the fleet's GCP resources and on nothing else, so
    # a failure here means "this cluster does not run KCC" on fifteen of this
    # fleet's sixteen clusters. Gating would fail every one of them closed on
    # the absence of a CRD they are not supposed to have. Absence is reported
    # as an inapplicable check rather than as a clean one -- a cluster with no
    # Config Connector has not cleared anything.
    kcc_argv = ["kubectl", "get", KCC_CATEGORY, "-A", "-o", "json"]
    kcc_parsed, kcc_result = run_and_gate(kcc_argv, kubeconfig, run=run, timeout=KCC_READ_TIMEOUT_S)
    context["kcc_objects"] = (kcc_parsed or {}).get("items") or []
    # `run_and_gate` returns `None` for four different things, and only one of
    # them means "no Config Connector here". Collapsing them would publish
    # "Config Connector is not installed on this cluster" as the audit's own
    # claim about a cluster where the read timed out or came back truncated --
    # a statement the collector cannot support, on the strength of a read that
    # failed. Each gets the reason that is true of it.
    kcc_reason = ""
    if kcc_parsed is not None and not context["kcc_objects"]:
        kcc_reason = (
            f"Config Connector is installed on this cluster -- `kubectl get "
            f"{KCC_CATEGORY} -A` was served -- but it declares no GCP "
            "resources, so there is nothing whose reconciliation could stall."
        )
    elif kcc_result.rc == TIMEOUT_RC:
        # Split out ahead of the `rc != 0` arm below, which is only entitled to
        # read an exit code as the API server's answer when the API server is
        # the one that produced it. A timeout is this collector giving up, and
        # the cluster it is most likely to give up on is the one where the read
        # has the most work to do -- which is the cluster that runs Config
        # Connector, the exact opposite of what the arm below would have said.
        kcc_reason = (
            f"Undetermined: `kubectl get {KCC_CATEGORY} -A` did not finish "
            f"within {KCC_READ_TIMEOUT_S}s and was killed, so whether Config "
            "Connector is installed here -- and whether anything it owns is "
            "stalled -- was not established. This check cleared nothing on "
            "this cluster."
        )
    elif kcc_result.rc != 0 and RESOURCE_TYPE_ABSENT_MARKER in kcc_result.stderr:
        kcc_reason = (
            f"Config Connector is not installed on this cluster: `kubectl get "
            f"{KCC_CATEGORY} -A` answered that the server does not serve the "
            "category. Nothing here declares a GCP resource. This fleet runs "
            "one Config Connector, on the hub cluster."
        )
    elif kcc_result.rc != 0:
        # Any other failure -- Forbidden, one CRD in the category refusing to
        # list, an API server error -- is a read that failed, and the cluster
        # most likely to produce one is the hub the check exists for.
        stderr = kcc_result.stderr.strip()[:ERROR_EXCERPT_CHARS] or "no stderr"
        kcc_reason = (
            f"Undetermined: `kubectl get {KCC_CATEGORY} -A` exited "
            f"{kcc_result.rc} without saying the category is unserved "
            f"({stderr}), so whether Config Connector is installed here -- and "
            "whether anything it owns is stalled -- was not established. This "
            "check cleared nothing on this cluster."
        )
    elif kcc_parsed is None:
        kcc_reason = (
            f"Undetermined: `kubectl get {KCC_CATEGORY} -A` exited 0 but "
            "returned output this collector could not read as a list of "
            "objects, so whether Config Connector is installed here -- and "
            "whether anything it owns is stalled -- was not established. This "
            "check cleared nothing on this cluster."
        )
    if kcc_reason.startswith(UNDETERMINED_PREFIX):
        # Not `not_applicable`: that list leaves the coverage denominator, so
        # a read that timed out on the hub would publish as complete and let
        # `finish` resolve every open finding this check filed there.
        context.setdefault("unevaluated", {})["kcc-object-wedged"] = kcc_reason
    elif kcc_reason:
        context.setdefault("not_applicable", {})["kcc-object-wedged"] = kcc_reason
    else:
        commands["kcc-object-wedged"] = _record(
            f"KUBECONFIG={kubeconfig} {shlex.join(kcc_argv)}", kcc_result
        )

    # Every ServiceAccount, not only `default`: §2.14 looks up the one each
    # workload names, and a read narrowed to `default` left it nothing to find
    # -- every named account read as absent, so the check returned no hits on
    # any cluster while its command recorded a clean run.
    sa_argv = ["kubectl", "get", "sa", "-A", "-o", "json"]
    parsed, result = gated(sa_argv)
    if parsed is None:
        raise GateFailure(f"ServiceAccount dump gate failed (rc={result.rc}): {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}")
    context["serviceaccounts"] = parsed.get("items", [])
    sa_record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(sa_argv)}", result)
    commands["default-sa-automount"] = sa_record
    commands["unbound-sa-automount"] = sa_record

    describe_argv = [
        "gcloud", "container", "clusters", "describe", name, "--location", location, "--project", project,
        "--format", "json(workloadIdentityConfig,privateClusterConfig,masterAuthorizedNetworksConfig,"
        "controlPlaneEndpointsConfig,nodePools[].name,nodePools[].config.workloadMetadataConfig)",
    ]
    parsed, result = gated(describe_argv)
    if parsed is None:
        raise GateFailure(f"cluster describe gate failed (rc={result.rc}): {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}")
    context["cluster_describe"] = parsed
    describe_command = shlex.join(describe_argv)
    describe_record = _record(describe_command, result)
    for slug in ("workload-identity-off", "public-control-plane"):
        commands[slug] = describe_record

    # Not attempted on Autopilot, where the API refuses it outright:
    # `node-pools list` answers HTTP 400 "Autopilot node pools cannot be
    # accessed or modified". Gating on that read anyway threw the whole
    # cluster away for the single check it backs. The table was simply
    # unreachable: the gate raises here, `collect_cluster` returns
    # `gate-failed`, and the not-applicable block that would have said so
    # never runs. Three of this fleet's four clusters were Autopilot, so every
    # daily `compliance-audit` collected one cluster and gate-failed the rest
    # -- eleven checks per cluster that had already succeeded, discarded on
    # the twelfth.
    #
    # The refusal is one API surface being closed, not the node pools being
    # absent, and the fix for the gate failure used to be to declare
    # `legacy-metadata` inapplicable -- publishing "no user-managed node
    # pools to carry a metadata setting" for a cluster that has five pools,
    # each carrying `config.metadata` and `config.workloadMetadataConfig`,
    # readable through the `clusters describe` above. That cost real coverage:
    # every Autopilot pool reports the compliant `GKE_METADATA`, so the check
    # does not fail to apply, it runs and passes, and saying otherwise
    # published an enforced control as unverified. Worse, the "run the
    # detection anyway and withhold the declaration if it fires" guard in
    # `collect_cluster` was structurally dead for this one slug: with
    # `node_pools` hardcoded empty, `check_legacy_metadata` returned no hits
    # by construction on every Autopilot cluster forever, so nothing could
    # ever falsify the reason. Take the pools from the describe projection
    # instead, and record that describe as the command behind the check.
    if cluster.get("autopilot"):
        context["node_pools"] = context["cluster_describe"].get("nodePools") or []
        commands["legacy-metadata"] = describe_record
    else:
        node_pools_argv = ["gcloud", "container", "node-pools", "list", "--cluster", name, "--location", location, "--project", project, "--format", "json"]
        parsed, result = gated(node_pools_argv)
        if parsed is None:
            raise GateFailure(f"node-pools list gate failed (rc={result.rc}): {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}")
        context["node_pools"] = parsed if isinstance(parsed, list) else []
        commands["legacy-metadata"] = _record(shlex.join(node_pools_argv), result)

    # Whether the three admission premises `_COMPLIANCE_AUTOPILOT_NOT_APPLICABLE`
    # rests on are still intact on this cluster. They hold only while nothing
    # lifts them, and Google documents two things that do: a customer-authored
    # `WorkloadAllowlist` naming `autogke-disallow-privilege` or
    # `autogke-no-write-mode-hostpath` (GKE >= 1.32.0-gke.1000000), and a
    # partner allowlist synchronised in (>= 1.32.2-gke.1652000). With one
    # installed, a privileged or hostPath workload is admissible and the three
    # checks are checks that ran and found nothing, not checks the cluster's
    # shape rules out -- so read for it rather than assert its absence in a
    # reason string an operator is meant to trust.
    #
    # One read per type, not one multi-type read: kubectl resolves every type
    # before listing any, so a cluster serving three of the four CRDs would
    # fail the combined read outright. A type the server does not serve is the
    # answer (none of that kind); any other failure leaves the allowlist state
    # unknown, and `collect_cluster` then declares nothing, because each reason
    # asserts no allowlist exists.
    if cluster.get("autopilot"):
        allowlists: list[str] = []
        unread: list[str] = []
        for kind in AUTOPILOT_ALLOWLIST_KINDS:
            parsed, result = run_and_gate(["kubectl", "get", kind, "-o", "json"], kubeconfig, run=run)
            if parsed is not None:
                allowlists.extend(
                    f"{i.get('kind') or '?'}/{(i.get('metadata') or {}).get('name', '')}"
                    for i in parsed.get("items", []) or []
                )
            elif not (result.rc != 0 and RESOURCE_TYPE_ABSENT_MARKER in result.stderr):
                unread.append(kind)
        context["autopilot_allowlists"] = allowlists
        context["autopilot_allowlists_unread"] = unread

    return CollectedContext(context, context["workloads"], commands)


def _collect_ai_security(cluster: dict, kubeconfig: Path, checks: tuple[CheckSpec, ...], *, run: RunFn) -> CollectedContext:
    """Two dumps, the ones the SOP's §2 fallback issues by hand: the workload
    dump backs every `workload`-kind check, the Service dump backs
    `inference-endpoint-public` alone. Either failing fails the whole
    cluster closed, the same trade-off compliance-audit's several
    independent reads accept."""
    workload_argv = ["kubectl", "get", COMPLIANCE_DUMP_KINDS, "-A", "-o", "json"]
    parsed, result = run_and_gate(workload_argv, kubeconfig, run=run)
    if parsed is None:
        raise GateFailure(f"workload dump gate failed (rc={result.rc}): {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}")
    ai_workloads = normalize_ai_workloads(parsed)
    workload_record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(workload_argv)}", result)

    svc_argv = ["kubectl", "get", "svc", "-A", "-o", "json"]
    svc_parsed, svc_result = run_and_gate(svc_argv, kubeconfig, run=run)
    if svc_parsed is None:
        raise GateFailure(f"service dump gate failed (rc={svc_result.rc}): {svc_result.stderr.strip()[:ERROR_EXCERPT_CHARS]}")
    svc_record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(svc_argv)}", svc_result)

    context = {"ai_workloads": ai_workloads, "services": svc_parsed.get("items", [])}
    commands = {
        spec.slug: (svc_record if spec.slug == "inference-endpoint-public" else workload_record) for spec in checks
    }
    return CollectedContext(context, ai_workloads, commands)


_COLLECTORS: dict[str, Callable[..., CollectedContext]] = {
    "obtainability-audit": _collect_obtainability,
    "compliance-audit": _collect_compliance,
    "ai-security-audit": _collect_ai_security,
}


# Why a target's per-workload checks had nothing to examine, per stream. Each
# stream filters the same dump differently -- obtainability reads templates and
# drops Pods, compliance reads resolved pod specs and keeps unowned ones,
# ai-security keeps only what `_is_ai_workload` matches -- so "empty" means a
# different thing in each and one sentence for all three would be false for two
# of them.
# Cluster-kind checks that match against `collected.workloads`, so an empty
# workload set leaves them nothing to examine exactly as it does a workload
# check: a PDB is only graded against the workload it covers, and an HPA's
# floor only against the Service-backed workload it scales. Compliance's two
# automount checks iterate the workload set too. `netpol-missing` does not: it
# reads `pod_namespaces`, every namespace holding a live Pod, so it still has
# something to examine on a cluster whose workloads are all out of scope.
_WORKLOAD_ANCHORED_CLUSTER_CHECKS = frozenset({
    "blocking-pdb", "pdb-overlapping", "hpa-floors-at-one",
    "default-sa-automount", "unbound-sa-automount",
})

_EMPTY_SCOPE_REASON: dict[str, str] = {
    "obtainability-audit": (
        "No workload on this cluster is in scope: every Deployment, StatefulSet "
        "and DaemonSet the dump returned is in a system namespace, "
        "addon-managed, owned by another object, opted out, or scaled to zero. "
        "This check examined nothing here, which is not the same as finding nothing."
    ),
    "compliance-audit": (
        "No workload on this cluster is in scope: every Deployment, StatefulSet, "
        "DaemonSet, CronJob and unowned Pod the dump returned is in a system "
        "namespace, addon-managed, or a GKE component. This check examined nothing "
        "here, which is not the same as finding nothing."
    ),
}

# `ai-security-audit` is deliberately absent, and the omission is the whole
# reason this is a table rather than a constant. What separates it from the two
# above is *what the empty set is empty of*. Obtainability and compliance drop
# workloads that exist and could carry the defect -- a system namespace, an
# addon, an owned Pod, one scaled to zero -- so "no finding" there is an
# unexamined absence and the reason says so. ai-security's filter is the
# subject definition itself: with no AI workload on the cluster there is no
# object a public inference endpoint or a writable weights mount could be true
# of. "Six checks ran and matched nothing" is a verdict about the cluster, not
# a gap in the audit, and §1 of the SOP says the same in the instruction it
# calls the most important in that section.
#
# An earlier version of this comment argued the omission mechanically instead,
# and every step of it was wrong: it claimed all six checks take an AI workload
# so an empty scope leaves `commands` empty, forcing a `limitations` note and
# pinning the stream `partial` forever. `inference-endpoint-public` is
# `"cluster"`-kind, so `commands` keeps an entry and `checks_run` is never
# empty; the arm below writes `checks_not_applicable`, which is not
# `limitations`; and `checks_not_applicable` leaves the coverage denominator
# without producing a gap, so it could not
# make a run `partial` even if it did fire. Do not re-derive the omission from
# that chain -- it does not hold, and the argument above does not need it.
#
# The real exposure is upstream, in `_is_ai_workload`: a model server under an
# image name `AI_MODEL_IMAGE_RE` does not list, serving on CPU so no
# accelerator limit gives it away, is invisible to this stream and reported as
# a clean cluster. That is a discriminator false negative, not a bookkeeping
# one, and no entry in this table would catch it.
#
# `AI_PROVIDER_CREDENTIAL_ENV_RE` is the answer to part of that, and only part.
# It admits a workload on a mark no allowlist has to keep up with -- holding a
# named provider credential -- which covers the unlisted runtime that pulls from
# Hugging Face or calls a hosted provider, and put this fleet's `litellm` in
# scope for the first time. What it still cannot see is a model server that
# neither matches a listed image nor holds a credential: one serving weights
# baked into its own image, on CPU, anonymously. Judge a "clean cluster" verdict
# from this stream against that shape before believing it.


def workload_declarations(root: Path) -> dict[tuple[str, str, str, str], set[str]]:
    """Index the Kubernetes objects a GitOps clone declares, by cluster tree.

    Maps `(cluster, kind, namespace, metadata.name)` to the set of
    clone-relative paths declaring it — a set, because two files claiming one
    object is the ambiguity `declaration_for` refuses to resolve rather than
    guessing between.

    This is `audit_report.kcc_declarations` for workload objects, and it exists
    for the reason that one gives: "parsed rather than grepped, deliberately".
    Config Connector resources got that treatment; the Kubernetes objects every
    other stream flags did not, and their SOPs still tell the *model* to run
    `grep -rl "name: <object>"` once per finding and decide for itself. It does
    not decide consistently. On 2026-09-06 the live fleet carried 116 findings
    in which `Deployment/waste-unsized` was `kind: manifest` with a correct
    path under `unsized-workload` and `kind: manual` -- "no pull request is
    possible" -- under `probes-liveness`, `no-requests` and `no-memory-limit`,
    which are the same object in the same file. Fourteen findings were `manual`
    on objects this index resolves.

    Config Connector resources are skipped, not overlooked: `kcc_declarations`
    already indexes them, keyed by the GCP resource name that `spec.resourceID`
    can override, and a `Cluster/<name>` finding must keep resolving through
    that table rather than through this one.

    Returns `{}` when PyYAML is absent or the clone is unreadable, which leaves
    every candidate unannotated and the SOP's grep as the only answer -- the
    behaviour that shipped before this existed.
    """
    try:
        import yaml  # noqa: PLC0415 -- optional; absence disables the annotation
    except ImportError:
        return {}

    index: dict[tuple[str, str, str, str], set[str]] = {}
    try:
        paths = sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml"))
    except OSError:
        return {}
    for path in paths:
        # `.git` holds packed objects, not manifests, and rglob walks into it.
        if GIT_DIR_NAME in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            # A file this collector cannot parse is one it cannot make a claim
            # about. Skipping leaves those findings unannotated.
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        # Only a file under `clusters/<name>/` is applied to a known cluster.
        # Anything else -- `gcp/`, `bootstrap/`, the repo root -- is either a
        # Config Connector resource this index skips or hub infrastructure no
        # per-cluster finding should resolve to, so it is indexed under no
        # cluster and can never match: `declaration_for` requires the cluster.
        if len(parts) <= GITOPS_CLUSTER_TREE_DEPTH or parts[0] != GITOPS_CLUSTER_TREE_ROOT:
            continue
        cluster = parts[1]
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            api = str(doc.get("apiVersion") or "")
            kind = str(doc.get("kind") or "")
            if not kind or KCC_API_GROUP_SUFFIX in api:
                continue
            meta = doc.get("metadata")
            if not isinstance(meta, dict):
                continue
            name = str(meta.get("name") or "")
            namespace = str(meta.get("namespace") or "")
            if not name or not namespace:
                continue
            index.setdefault((cluster, kind, namespace, name), set()).add(str(relative))
    return index


def declaration_for(
    index: dict[tuple[str, str, str, str], set[str]],
    cluster: str,
    namespace: str,
    obj: str,
) -> dict | None:
    """Where the GitOps repo declares one candidate's object, or `None`.

    `None` means unannotated — no claim either way — and is what an absent
    index, an object outside `Kind/name` form, or a genuine miss all return.
    A hit carries `path` (the declaration itself, for a remediation that
    *changes* the object) and `directory` (its parent, for one that *creates* a
    sibling beside it, as a new PDB or NetworkPolicy does).

    The match is exact on all four of cluster, kind, namespace and name. Every
    looser arm was measured against the live fleet's 116 findings first and
    matched nothing the exact arm had not already matched, so the fallbacks
    that would have made this kind-blind or cluster-blind -- the two failure
    modes every SOP warns the model's `grep` has -- buy nothing and are not
    here. A namespaced object resolved into another cluster's tree fails that
    tree's sync, which is worse than resolving nothing.
    """
    kind, _, name = obj.partition("/")
    if not kind or not name or not cluster or not namespace:
        return None
    paths = index.get((cluster, kind, namespace, name))
    if not paths or len(paths) > 1:
        # Two files declaring one object is a duplicate resource id that Argo
        # and Config Sync both reject; picking one would name the wrong file
        # half the time. Say nothing and let the SOP's `manual` branch hold.
        return None
    path = next(iter(paths))
    parent = str(Path(path).parent)
    return {"path": path, "directory": parent}


def _argocd_chart_source(spec: dict) -> dict | None:
    """The one chart source on an Argo CD Application, with its field path.

    An Application sources either a git path or a chart, and only a chart is a
    Helm release. `spec.source.chart` absent is therefore the discriminator
    that keeps this off the plain-manifest Applications an ordinary GitOps repo
    is mostly made of -- including the `workloads-<cluster>` ones here, whose
    objects `workload_declarations` already resolves to their own YAML.

    Multi-source Applications (`spec.sources`) carry the chart in one entry, so
    the returned `field` records which: an edit has to name `sources[2].helm`,
    not `source.helm`. Two chart entries is an ambiguity with no right answer
    and returns None, the way `declaration_for` treats two declaring files.
    """
    single = spec.get("source")
    if isinstance(single, dict) and single.get("chart"):
        return {"source": single, "field": "spec.source"}
    sources = spec.get("sources")
    if not isinstance(sources, list):
        return None
    charts = [
        {"source": entry, "field": f"spec.sources[{position}]"}
        for position, entry in enumerate(sources)
        if isinstance(entry, dict) and entry.get("chart")
    ]
    return charts[0] if len(charts) == 1 else None


def _argocd_kustomize_source(spec: dict, root: Path) -> dict | None:
    """The one Kustomize source on an Argo CD Application, with its field path.

    Reached only where `_argocd_chart_source` found no chart, and answers the
    case its docstring sets aside. A plain directory of manifests is resolved
    by `workload_declarations`, which finds the object's own YAML -- but an
    overlay over a *remote* base renders objects no file in the repo declares,
    so that lookup finds nothing and the finding falls to `manual`. It is the
    same hole a chart put a workload in, and as common: `resources:` pointing
    at a tagged base in another repository is an ordinary way to run a fleet.

    A `kustomization.yaml` at `spec.source.path` is the discriminator, checked
    against the clone rather than against `spec.source.kustomize`, which Argo
    CD infers from that same file and most Applications therefore omit. Where
    the base *is* local, the object has its own manifest, and the
    `"declaration" not in candidate` guard in `_emit` keeps that direct edit
    ahead of a patch here.

    Multi-source and two-source ambiguity follow `_argocd_chart_source`.
    """

    def rooted(entry: object) -> bool:
        if not isinstance(entry, dict) or entry.get("chart"):
            return False
        path = str(entry.get("path") or "").strip()
        if not path or path.startswith("/") or ".." in Path(path).parts:
            return False
        try:
            directory = root / path
            return any((directory / name).is_file() for name in KUSTOMIZATION_FILE_NAMES)
        except OSError:
            return False

    single = spec.get("source")
    if rooted(single):
        return {"source": single, "field": "spec.source"}
    sources = spec.get("sources")
    if not isinstance(sources, list):
        return None
    overlays = [
        {"source": entry, "field": f"spec.sources[{position}]"}
        for position, entry in enumerate(sources)
        if rooted(entry)
    ]
    return overlays[0] if len(overlays) == 1 else None


def _argocd_values_field(source: dict, field: str) -> str:
    """Where a values override belongs on one Argo CD chart source.

    Names the block already in the file when there is one, so an override joins
    it rather than sitting beside a second block Argo CD would ignore --
    `values` and `valuesObject` are mutually exclusive and Argo rejects an
    Application carrying both.
    """
    helm = source.get("helm")
    if isinstance(helm, dict) and isinstance(helm.get(ARGOCD_VALUES_STRING_FIELD), str):
        return f"{field}.helm.{ARGOCD_VALUES_STRING_FIELD}"
    return f"{field}.helm.{ARGOCD_VALUES_OBJECT_FIELD}"


def release_declarations(root: Path) -> dict[tuple, dict]:
    """Index the renderers a GitOps clone installs, by destination cluster.

    The companion `workload_declarations` above resolves a workload to the file
    declaring *that object*. A workload a chart renders has no such file: the
    repo declares the release, and the object exists only after Helm expands
    the chart. That is the single largest reason a finding cannot open a pull
    request. Of the 53 findings in the 2026-09-07 obtainability run, 48 were
    `manual`, and the reason most of them gave was that the workload "is not
    declared in this repository" -- true of Argo CD's own Deployments, of
    cert-manager, and of the kube-agents chart's `litellm`. It is true of most
    workloads in most real fleets, because most real fleets install charts.
    A Kustomize overlay over a base in another repository leaves the object
    equally undeclared, and is resolved here for the same reason.

    Maps a key to a record naming the declaration and what it renders. Two key
    shapes, because the reconcilers mark their objects differently and
    `release_of` reads both:

    - `(cluster, RELEASE_KEY_APPLICATION, application_name)` -- an Argo CD
      `Application`, sourcing either a chart or a Kustomize overlay, found
      from a tracking id.
    - `(cluster, RELEASE_KEY_RELEASE, release_namespace, release_name)` -- a
      Flux `HelmRelease`, or that same Argo CD Application under the release
      coordinates it installs, found from the `meta.helm.sh` annotation pair.
      Helm only: Kustomize output carries no such annotation.

    Each record carries a `renderer` of `RENDERER_HELM` or `RENDERER_KUSTOMIZE`
    saying how `values_field` is written -- a values mapping the chart reads by
    key, or a list of patches Kustomize applies to a matched object.

    Resolving the cluster is the part that differs from `workload_declarations`,
    which reads it off the path. An Argo CD Application names its own
    destination and may live anywhere in the repo -- `bootstrap/`, `apps/`, a
    flat root -- so the path convention would miss it entirely. Where the
    destination is a `server` URL rather than a `name`, the Argo cluster
    registration Secrets carry the mapping, and a first pass builds it.
    A `HelmRelease` names no cluster at all, Flux being per-cluster, so that
    one does fall back to the `clusters/<name>/` convention.

    `ApplicationSet` is deliberately not indexed. Its generated Applications
    have templated names and destinations this cannot evaluate without
    reimplementing the generators, and guessing which cluster a generated
    Application lands on is exactly the cluster-blind failure mode
    `declaration_for` refuses. A fleet installing charts through an
    ApplicationSet keeps the `manual` verdict it has today.

    Returns `{}` when PyYAML is absent or the clone is unreadable, which is the
    behaviour that shipped before this existed.
    """
    try:
        import yaml  # noqa: PLC0415 -- optional; absence disables the annotation
    except ImportError:
        return {}

    try:
        paths = sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml"))
    except OSError:
        return {}

    documents: list[tuple[str, tuple[str, ...], dict]] = []
    for path in paths:
        if GIT_DIR_NAME in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        for doc in docs:
            if isinstance(doc, dict) and doc.get("kind"):
                documents.append((str(relative), relative.parts, doc))

    # Pass 1: the two lookups an Application or a HelmRelease resolves through.
    servers: dict[str, str] = {}
    repositories: dict[tuple[str, str], str] = {}
    for _, _, doc in documents:
        kind = str(doc.get("kind") or "")
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        if kind == "Secret":
            labels = meta.get("labels") or {}
            if labels.get(ARGOCD_CLUSTER_SECRET_LABEL) != ARGOCD_CLUSTER_SECRET_VALUE:
                continue
            # `stringData` is what a committed registration uses; `data` is
            # base64 and a committed one would be a leaked credential, so only
            # the plaintext form is read.
            entry = doc.get("stringData")
            if not isinstance(entry, dict):
                continue
            server = str(entry.get("server") or "").strip()
            cluster = str(entry.get("name") or "").strip()
            if server and cluster:
                servers[server] = cluster
        elif kind == FLUX_HELM_REPOSITORY_KIND:
            url = str((doc.get("spec") or {}).get("url") or "").strip()
            name = str(meta.get("name") or "")
            namespace = str(meta.get("namespace") or "")
            if url and name:
                repositories[(namespace, name)] = url

    index: dict[tuple, dict] = {}
    ambiguous: set[tuple] = set()

    def record(key: tuple, entry: dict) -> None:
        existing = index.get(key)
        if existing is not None and existing != entry:
            # Two declarations for one release. Same reasoning as
            # `declaration_for`: naming one would name the wrong file half the
            # time, so name neither.
            ambiguous.add(key)
            return
        index[key] = entry

    # Pass 2: the declarations themselves.
    for relative, parts, doc in documents:
        kind = str(doc.get("kind") or "")
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        name = str(meta.get("name") or "")
        if not name:
            continue
        if kind == ARGOCD_APPLICATION_KIND:
            chart = _argocd_chart_source(spec)
            overlay = _argocd_kustomize_source(spec, root) if chart is None else None
            if chart is None and overlay is None:
                continue
            found = chart or overlay
            source, field = found["source"], found["field"]
            destination = spec.get("destination") if isinstance(spec.get("destination"), dict) else {}
            cluster = str(destination.get("name") or "").strip()
            if not cluster:
                server = str(destination.get("server") or "").strip()
                # The in-cluster destination is whichever cluster Argo CD runs
                # on, which this index has no way to name. Skip rather than
                # resolve a finding into the wrong tree.
                if server and server != ARGOCD_IN_CLUSTER_SERVER:
                    cluster = servers.get(server, "")
            if not cluster:
                continue
            release_namespace = str(destination.get("namespace") or "").strip()
            helm = source.get("helm") if isinstance(source.get("helm"), dict) else {}
            entry = {
                "path": relative,
                "kind": ARGOCD_APPLICATION_KIND,
                "renderer": RENDERER_HELM if chart else RENDERER_KUSTOMIZE,
                "chart": str(source.get("chart") or source.get("path") or ""),
                "repo": str(source.get("repoURL") or ""),
                "version": str(source.get("targetRevision") or ""),
                "values_field": (
                    _argocd_values_field(source, field)
                    if chart
                    else f"{field}.{RENDERER_KUSTOMIZE}.{ARGOCD_KUSTOMIZE_PATCHES_FIELD}"
                ),
            }
            record((cluster, RELEASE_KEY_APPLICATION, name), entry)
            release_name = str(helm.get("releaseName") or "").strip() or name
            # Only a chart install leaves the `meta.helm.sh` pair behind, so
            # only a chart is worth the second key. Kustomize output carries
            # the tracking id alone, which the Application key above already
            # answers.
            if chart and release_namespace:
                record((cluster, RELEASE_KEY_RELEASE, release_namespace, release_name), entry)
            # A local Kustomize root is also an answer to "where does a new
            # object for this namespace go", which no other key gives. A chart
            # is not: its directory is in another repository, and the values
            # override that reaches an *existing* object cannot create one.
            if overlay and release_namespace and entry["chart"]:
                record((cluster, RELEASE_KEY_NAMESPACE, release_namespace), entry)
        elif kind == FLUX_HELM_RELEASE_KIND:
            # Flux is installed per-cluster, so the path convention is the only
            # cluster signal a HelmRelease carries.
            if len(parts) <= GITOPS_CLUSTER_TREE_DEPTH or parts[0] != GITOPS_CLUSTER_TREE_ROOT:
                continue
            cluster = parts[1]
            namespace = str(meta.get("namespace") or "")
            chart_spec = ((spec.get("chart") or {}).get("spec") or {}) if isinstance(spec.get("chart"), dict) else {}
            source_ref = chart_spec.get("sourceRef") if isinstance(chart_spec.get("sourceRef"), dict) else {}
            repo_namespace = str(source_ref.get("namespace") or namespace)
            repo_name = str(source_ref.get("name") or "")
            release_namespace = str(spec.get("targetNamespace") or namespace)
            release_name = str(spec.get("releaseName") or "").strip() or name
            if not release_namespace:
                continue
            entry = {
                "path": relative,
                "kind": FLUX_HELM_RELEASE_KIND,
                "renderer": RENDERER_HELM,
                "chart": str(chart_spec.get("chart") or ""),
                "repo": repositories.get((repo_namespace, repo_name), ""),
                "version": str(chart_spec.get("version") or ""),
                "values_field": f"spec.{FLUX_VALUES_FIELD}",
            }
            record((cluster, RELEASE_KEY_RELEASE, release_namespace, release_name), entry)

    for key in ambiguous:
        index.pop(key, None)
    return index


def release_declaration_for(
    index: dict[tuple, dict],
    cluster: str,
    release: dict | None,
) -> dict | None:
    """Where the GitOps repo declares one candidate's chart release, or `None`.

    `None` is no claim either way, exactly as in `declaration_for`: an empty
    index, a workload no chart installs, a release installed by a `helm
    install` nothing in the repo records, or an ApplicationSet this does not
    index all return it, and the SOP's `manual` branch holds.

    The Argo CD Application name is tried first. It is the more specific of the
    two keys -- one Application, named outright by the tracking id the object
    carries -- where the release coordinates could in principle be shared by
    two Applications installing into one namespace.
    """
    if not index or not cluster or not release:
        return None
    application = str(release.get("application") or "")
    if application:
        entry = index.get((cluster, RELEASE_KEY_APPLICATION, application))
        if entry:
            return entry
    name = str(release.get("name") or "")
    namespace = str(release.get("namespace") or "")
    if name and namespace:
        entry = index.get((cluster, RELEASE_KEY_RELEASE, namespace, name))
        if entry:
            return entry
    return None


def _projects_restrict_namespaces(root: Path | None) -> bool:
    """Whether any Argo CD AppProject in the clone whitelists namespaces.

    True withdraws `namespace_directories`' `cluster` arm, whose whole claim is
    that a directory applied to a cluster may carry an object for any namespace
    on it. An AppProject's `destinations[].namespace` is what makes that false,
    and Argo CD refuses the object at sync rather than applying it.

    Deliberately blunt: one restrictive project anywhere withdraws the arm
    everywhere, because this collector parses chart and overlay sources only
    and so cannot say which project owns the plain-directory Application whose
    directory the arm names. Unreadable or unparseable is treated as
    restrictive, which costs findings rather than opening wrong pull requests.
    """
    if root is None:
        return False
    try:
        import yaml  # noqa: PLC0415 -- optional; absence disables the annotation
    except ImportError:
        return True
    try:
        paths = sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml"))
    except OSError:
        return True
    for path in paths:
        if GIT_DIR_NAME in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True
        if ARGOCD_APPPROJECT_KIND not in text:
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            return True
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != ARGOCD_APPPROJECT_KIND:
                continue
            spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
            destinations = spec.get(ARGOCD_DESTINATIONS_FIELD)
            if not isinstance(destinations, list):
                # A project with no destinations permits nothing at all, which
                # is a restriction rather than the absence of one.
                return True
            for destination in destinations:
                if not isinstance(destination, dict):
                    return True
                if str(destination.get("namespace") or "") != ARGOCD_DESTINATION_WILDCARD:
                    return True
    return False


def _kustomize_roots(releases: dict[tuple, dict]) -> dict[str, set[str]]:
    """Per cluster, the local directories Argo CD renders through Kustomize.

    A directory `release_declarations` recorded as a Kustomize root is not a
    directory objects are applied *from*, even though every manifest in it
    parses and `workload_declarations` indexes each one under its own cluster.
    Kustomize builds what the kustomization lists, so a file dropped in renders
    only once `resources:` names it, which a one-file remediation cannot also
    add -- so the SOPs make a fix landing there `kind: manual`.

    Keyed by cluster, because that is the destination of the Application the
    root belongs to, and every key `release_declarations` writes leads with it.
    """
    roots: dict[str, set[str]] = {}
    for key, entry in (releases or {}).items():
        if not key or entry.get("renderer") != RENDERER_KUSTOMIZE:
            continue
        path = str(entry.get("chart") or "").strip()
        if path:
            roots.setdefault(str(key[0]), set()).add(str(Path(path)))
    return roots


def _plainly_applied(directories: set[str], roots: set[str]) -> set[str]:
    """`directories`, minus each one a Kustomize root at or above it renders."""
    kept: set[str] = set()
    for directory in directories:
        path = Path(directory)
        if any(path == Path(entry) or Path(entry) in path.parents for entry in roots):
            continue
        kept.add(directory)
    return kept


def namespace_directories(
    declarations: dict[tuple[str, str, str, str], set[str]],
    releases: dict[tuple, dict],
    root: Path | None = None,
) -> dict[tuple[str, str], dict]:
    """Where a *new* object for one cluster's namespace belongs in the clone.

    The two indexes above both answer "where is this object declared", and a
    finding whose fix is an edit needs nothing more. A finding whose fix is a
    new object needs the other question answered — `netpol-missing` writes a
    NetworkPolicy that does not exist yet, `default-sa-automount` a
    ServiceAccount — and neither index answers it, because the object they
    would look up is the one that is missing.

    So the model is left to decide, and its reasoning is wrong in a way that
    reads as careful. From the 2026-09-07 obtainability run, on a namespace
    this function resolves: "No file in this repository declares an applied
    object in namespace podinfo-kustomize -- only the ArgoCD Application
    sources that install a chart/overlay into it, which is not a sibling under
    SOP section 3's rule." Every clause of that is true and the conclusion is
    false. `clusters/spot-capacity-test/kustomize/podinfo/` is a Kustomize root
    *in this repository* that the Application renders into that namespace;
    `_argocd_kustomize_source` proved it is one, against the clone, before the
    finding was written. The collector knew and the model did not.

    Two shapes, in precedence order:

    - `NAMESPACE_DIRECTORY_SIBLING` — the one directory holding declarations
      already applied to that cluster and namespace. This is the SOP's own
      sibling rule, computed from the parsed index rather than grepped, and it
      wins where both apply: a directory of plain manifests is applied as it
      stands, so a file added to it needs no wiring at all.
    - `NAMESPACE_DIRECTORY_OVERLAY` — the local Kustomize root an Argo CD
      Application renders into the namespace. A file added here renders only
      once the `resources:` list names it, which a one-file remediation cannot
      also add, so the SOPs make such a fix `kind: manual`.
    - `NAMESPACE_DIRECTORY_CLUSTER` — the one directory in the clone declaring
      objects applied to that *cluster*, whatever their namespace, stored under
      `NAMESPACE_KEY_ANY` for the caller to fall back to. This is the arm with
      the yield, and the reason is that the namespaces a finding lands in are
      mostly not ones the repo declares into: on 2026-09-07 the manual findings
      here sat in `argocd`, `cert-manager` and `kubeagents-system`, none of
      which any file names, while the cluster each of them runs on has a
      directory Argo CD applies wholesale. A NetworkPolicy for `cert-manager`
      goes there — that is where a NetworkPolicy for someone else's chart
      belongs anyway, rather than in a fork of the chart.

      Two things have to hold, and both are checked rather than assumed. The
      directory has to be unambiguous, as in the other arms. And no AppProject
      may whitelist namespaces, since that is what turns "applied to the
      cluster" into "applied to these namespaces on it"; see
      `_projects_restrict_namespaces`, which is deliberately blunt about it. A
      remediation using this arm has to set `metadata.namespace` explicitly:
      the Application's own destination namespace, where it has one, is only
      the default for objects that declare none.

      Unambiguous is counted over the directories objects are applied *from*,
      which is not every directory holding a manifest: a Kustomize root is
      indexed like any other and renders only what its kustomization lists, so
      counting it makes a cluster ambiguous that has exactly one plain-apply
      tree. That was not hypothetical either. On 2026-09-07 the clone held five
      such directories across four clusters and this arm resolved three of
      them; the cluster it skipped was `spot-capacity-test`, whose second
      directory is the `clusters/spot-capacity-test/kustomize/podinfo` overlay
      that the merged `no-pdb` remediation had just added. `_kustomize_roots`
      is the exclusion, and it takes the root's subdirectories with it, since a
      manifest one level down inside an overlay is no more applied as it stands
      than one at the top.

    Two directories declaring into one namespace is the ambiguity
    `declaration_for` refuses, and is refused here for the same reason: naming
    one would name the wrong one about half the time. Absent means unresolved,
    never "nowhere" — the SOP's grep is still the answer then.
    """
    resolved: dict[tuple[str, str], dict] = {}
    directories: dict[tuple[str, str], set[str]] = {}
    per_cluster: dict[str, set[str]] = {}
    for (cluster, _kind, namespace, _name), paths in (declarations or {}).items():
        for path in paths:
            parent = str(Path(path).parent)
            directories.setdefault((cluster, namespace), set()).add(parent)
            per_cluster.setdefault(cluster, set()).add(parent)
    rendered = _kustomize_roots(releases)
    for key, found in directories.items():
        # A directory inside a Kustomize root is not a sibling: a file added
        # there renders only once `resources:` names it. Left to the overlay
        # arm below, or unresolved, rather than answered as needing no wiring.
        if len(found) == 1 and _plainly_applied(found, rendered.get(key[0]) or set()):
            resolved[key] = {
                "path": next(iter(found)),
                "source": NAMESPACE_DIRECTORY_SIBLING,
            }
    for key, entry in (releases or {}).items():
        if len(key) != NAMESPACE_KEY_WIDTH or key[1] != RELEASE_KEY_NAMESPACE:
            continue
        cluster, namespace = key[0], key[2]
        if (cluster, namespace) in resolved:
            continue
        resolved[(cluster, namespace)] = {
            "path": entry["chart"],
            "source": NAMESPACE_DIRECTORY_OVERLAY,
            "declaration": entry["path"],
        }
    if not _projects_restrict_namespaces(root):
        for cluster, found in per_cluster.items():
            applied = _plainly_applied(found, rendered.get(cluster) or set())
            if len(applied) != 1:
                continue
            resolved[(cluster, NAMESPACE_KEY_ANY)] = {
                "path": next(iter(applied)),
                "source": NAMESPACE_DIRECTORY_CLUSTER,
            }
    return resolved


def collect_cluster(
    cluster: dict,
    audit_id: str,
    checks: tuple[CheckSpec, ...],
    *,
    run: RunFn = default_run,
    declarations: dict[tuple[str, str, str, str], set[str]] | None = None,
    releases: dict[tuple, dict] | None = None,
    namespaces: dict[tuple[str, str], dict] | None = None,
) -> dict:
    """One manifest `clusters[]` entry: every enumerated cluster gets
    one, whatever happened — `outcome` says which of the three shapes it is.
    """
    name, project, location = cluster["name"], cluster["project"], cluster["location"]
    # `name` is what gcloud and the GitOps tree call the cluster; `target` is
    # what the manifest does, and so every candidate's `cluster`.
    target = cluster.get("target") or target_name(project, location, name)
    autopilot = bool(cluster.get("autopilot"))
    kubeconfig, cred_run = fetch_credentials(project, name, location, run=run)
    if cred_run.rc != 0:
        return {
            "name": target, "project": project, "location": location,
            "autopilot": autopilot,
            "outcome": OUTCOME_UNREACHABLE,
            "error": f"get-credentials rc={cred_run.rc}: {cred_run.stderr.strip()[:ERROR_EXCERPT_CHARS]}",
        }

    try:
        collected = _COLLECTORS[audit_id](cluster, kubeconfig, checks, run=run)
    except GateFailure as exc:
        return {
            "name": target, "project": project, "location": location,
            "autopilot": autopilot,
            "outcome": OUTCOME_GATE_FAILED,
            "error": str(exc),
        }

    def emit(spec: CheckSpec, hit: dict, default_namespace: str, workload: dict | None = None) -> dict:
        severity = hit.get("severity") or spec.severity
        # Same override `severity` already has, for the same reason: a check
        # whose arms differ in what they prove cannot state one consequence for
        # all of them, and the arm is only known where the hit is built.
        impact = hit.get("impact") or spec.impact
        # Which arm fired is an observation, not prose, and only the hit knows
        # it. Flagged so `adopt_arm_impact` can hold the model to this sentence
        # the way `adopt_collector_evidence` holds it to the excerpt -- and so
        # a later run cannot carry a stale arm sentence forward over a
        # corrected one. The `spec.impact` default is deliberately *not*
        # flagged: there the model's object-specific rewrite is usually the
        # better sentence, naming the quota or the cluster the constant cannot.
        arm_specific = bool(hit.get("impact"))
        if severity == spec.severity and cluster.get("autopilot") and spec.autopilot_severity:
            severity = spec.autopilot_severity
            impact = f"{impact} (Autopilot: severity downgraded — the platform sets requests and limits at admission.)"
        excerpt = hit["excerpt"]
        if workload and workload.get("suspended"):
            excerpt += _SUSPENDED_CRONJOB_NOTE
        if workload and workload.get("scaled_to_zero"):
            excerpt += _scaled_to_zero_note(workload["kind"])
        emitted = {
            "check": spec.slug,
            "cluster": target,
            "namespace": hit.get("namespace", default_namespace),
            "object": hit["object"],
            "severity": severity,
            "excerpt": excerpt,
            "impact": impact,
            "needs_triage": None,
        }
        if arm_specific:
            emitted["impact_authoritative"] = True
        # Where the GitOps repo declares this object, when it does. Absent
        # means unannotated, never "no declaration exists": the index is empty
        # without `--workspace`, and the SOP's own grep is still the answer
        # then. See `workload_declarations`.
        if declarations:
            declaration = declaration_for(declarations, name, emitted["namespace"], hit["object"])
            if declaration:
                emitted["declaration"] = declaration
        # Where the repo declares the *release* that renders this object, for
        # the workloads a chart installs and no file declares directly. Only
        # when the object itself is undeclared: a workload with its own
        # manifest is edited there, and a values override for it would be a
        # second, weaker expression of the same fix. See `release_declarations`.
        if releases and "declaration" not in emitted:
            release = (workload or {}).get("release") or hit.get("release")
            resolved = release_declaration_for(releases, name, release)
            if resolved:
                emitted["release_declaration"] = resolved
        # Where a *new* object for this namespace would go, which is a
        # different question from either of the two above and the only one a
        # create-an-object remediation can use. Attached whatever else
        # resolved: an object with its own file still needs somewhere to put a
        # NetworkPolicy that has none. See `namespace_directories`.
        if namespaces:
            directory = namespaces.get((name, emitted["namespace"])) or namespaces.get(
                (name, NAMESPACE_KEY_ANY)
            )
            if directory:
                emitted["namespace_directory"] = directory
        # What reasserts this object's spec, where something does. A workload
        # check gets it off the workload record; a cluster check has to put it
        # on the hit itself, because `workload` is None there and only the
        # check knows which of its inputs `hit["object"]` names.
        #
        # This used to be workload-only, on the premise that "a cluster-scoped
        # hit has no object whose spec a controller could be holding". That is
        # false for most cluster checks -- `inference-endpoint-public`
        # names a Service, `blocking-pdb` a PodDisruptionBudget,
        # `hpa-cannot-scale` an HPA, the two RBAC checks a binding or a role,
        # and `default-sa-automount` a workload it reached without iterating
        # `collected.workloads`. Cluster-scoped means the check runs once per
        # cluster, not that its findings name the cluster. The one that cost:
        # `inference-endpoint-public` on `Service/ai-inference-unsafe`, a manual
        # finding telling the owner to choose a fix, on an object the Argo CD
        # Application `workloads-adamparco-gitops` holds. The four checks left
        # out name a GKE cluster, a node pool, or -- `netpol-missing` -- a
        # Namespace whose fix is a NetworkPolicy that does not exist yet, where
        # "a change applied by hand is reverted" would be false.
        reconciler = (workload or {}).get("reconciler") or hit.get("reconciler")
        if reconciler:
            emitted["reconciler"] = reconciler
        return emitted

    candidates = []
    for spec in checks:
        if spec.kind == "workload":
            for workload in collected.workloads:
                hit = spec.run(workload, collected.context)
                if hit is not None:
                    candidates.append(emit(spec, hit, workload["ns"], workload))
        else:
            for hit in spec.run(collected.context):
                candidates.append(emit(spec, hit, hit.get("namespace", "")))

    # A check whose own read failed ran against nothing. Its candidates, if
    # any, rest on the missing input, and a `commands` entry would let
    # `cross_check_manifest` corroborate it as run and clean.
    unevaluated = collected.context.get("unevaluated") or {}
    candidates = [c for c in candidates if c["check"] not in unevaluated]
    not_applicable_slugs: set[str] = set(unevaluated)
    checks_not_applicable: list[dict] = []
    if audit_id == "compliance-audit" and cluster.get("autopilot"):
        applicable = {spec.slug for spec in checks}
        # A check that found something plainly applied. Each reason below
        # asserts admission rejects the shape for in-scope workloads, so a
        # candidate is that premise being wrong on this cluster -- a preview
        # channel, a workload predating the conversion, an exemption Google
        # granted -- and the manifest must not say both. Declaring it
        # inapplicable while carrying its candidate is the incoherence the
        # `commands` filter already avoids, and it resolves the wrong way: the
        # finding is real and the claim about the cluster's shape is not.
        #
        # Every slug in the table is backed by a detection fed the real
        # workload dump, so this guard has something to guard for each of
        # them. It did not always: `legacy-metadata` was in the table while
        # `_collect_compliance` handed `check_legacy_metadata` a hardcoded
        # empty `node_pools`, so it returned no hits by construction and could
        # never falsify its own reason. Keep that property -- a slug added
        # here whose detection cannot see real input silently disarms this.
        found = {c["check"] for c in candidates}
        # The second way each premise fails. Every reason ends by saying this
        # cluster carries no WorkloadAllowlist, and an allowlist is exactly
        # what lifts the admission rule the rest of the sentence rests on --
        # so one existing makes all three declarations false at once, whether
        # or not any check fired. `_collect_compliance` reads for them; the
        # three then report as checks that ran, which they did.
        allowlists = collected.context.get("autopilot_allowlists") or []
        unread = collected.context.get("autopilot_allowlists_unread") or []
        if allowlists:
            print(
                f"[collect] {project}/{name}: {len(allowlists)} Autopilot workload "
                f"allowlist object(s) present ({', '.join(sorted(allowlists)[:3])}); "
                f"admission can be exempted here, so §1's node-facing checks are "
                f"reported as checks that ran rather than declared inapplicable",
                file=sys.stderr,
            )
        elif unread:
            print(
                f"[collect] {project}/{name}: could not read {', '.join(unread)}; "
                f"whether an allowlist exempts admission here is unknown, so §1's "
                f"node-facing checks are reported as checks that ran rather than "
                f"declared inapplicable",
                file=sys.stderr,
            )
        # One candidate falsifies all three: each reason rests on the same
        # admission rule, and a workload that got past it for one shape says
        # the rule is not holding here.
        trio_found = sorted(found & {slug for slug, _ in _COMPLIANCE_AUTOPILOT_NOT_APPLICABLE})
        if trio_found:
            print(
                f"[collect] {project}/{name}: {', '.join(trio_found)} produced candidates "
                f"on Autopilot, so admission is not holding here; §1's node-facing "
                f"checks are reported as checks that ran",
                file=sys.stderr,
            )
        for slug, reason in _COMPLIANCE_AUTOPILOT_NOT_APPLICABLE:
            if slug not in applicable or allowlists or unread or trio_found:
                continue
            not_applicable_slugs.add(slug)
            checks_not_applicable.append({"check": slug, "reason": reason})

    if not collected.workloads and audit_id in _EMPTY_SCOPE_REASON:
        # Nothing survived S1-S5, so every per-workload check on this target
        # examined an empty set. Reporting those as checks that ran is what
        # the 2026-09-05 obtainability run did on 12 of 16 clusters: the
        # document said 11 checks on 16 targets with nothing inapplicable and
        # no limitation, which reads as fourteen healthy clusters when twelve
        # of them were never in scope at all. A check with nothing to examine
        # has not cleared anything, and the manifest is the only place that
        # distinction still exists -- by publish time a clean cluster and an
        # empty one are the same absence of candidates.
        #
        # `outcome` is already "collected", so the dump succeeded; this is the
        # scope being empty, not the read failing. Cluster-scoped checks are
        # untouched, bar the ones `_WORKLOAD_ANCHORED_CLUSTER_CHECKS` names
        # because they walk the workload set: an HPA pointing at a workload
        # that no longer exists, a public control plane, a cluster-admin
        # binding are all still true of a cluster running nothing.
        reason = _EMPTY_SCOPE_REASON[audit_id]
        for spec in checks:
            if spec.slug in not_applicable_slugs:
                continue
            if spec.kind != "workload" and spec.slug not in _WORKLOAD_ANCHORED_CLUSTER_CHECKS:
                continue
            not_applicable_slugs.add(spec.slug)
            checks_not_applicable.append({"check": spec.slug, "reason": reason})

    # A check the collector itself established has nothing to examine here,
    # from a read it made. The two blocks above infer inapplicability from a
    # property of the cluster (Autopilot) or of the scope (no workloads); this
    # one carries a fact only the read knows -- that the CRD is absent, or
    # installed and empty. Same coherence guard as the Autopilot block, and for
    # the same reason: a manifest must never both declare a check inapplicable
    # and carry its candidates.
    found = {c["check"] for c in candidates}
    applicable = {spec.slug for spec in checks}
    for slug, reason in sorted((collected.context.get("not_applicable") or {}).items()):
        if slug not in applicable or slug in not_applicable_slugs:
            continue
        if slug in found:
            print(
                f"[collect] {project}/{name}: {slug} was declared inapplicable by the "
                f"collector but produced candidates here; reporting it as a check that ran",
                file=sys.stderr,
            )
            continue
        not_applicable_slugs.add(slug)
        checks_not_applicable.append({"check": slug, "reason": reason})

    result = {
        "name": target, "project": project, "location": location,
        "autopilot": autopilot,
        "outcome": OUTCOME_COLLECTED,
        "commands": [{"check": spec.slug, **collected.commands[spec.slug]} for spec in checks if spec.slug not in not_applicable_slugs],
        "candidates": candidates,
    }
    if checks_not_applicable:
        result["checks_not_applicable"] = checks_not_applicable
    if unevaluated:
        result["checks_unevaluated"] = [
            {"check": slug, "reason": reason} for slug, reason in sorted(unevaluated.items())
        ]
    return result


def crashed_entry(cluster: dict, exc: BaseException) -> dict:
    """A `clusters[]` entry for a worker that raised something unmodelled.

    `future.result()` re-raises, so one unhandled exception on one cluster
    aborts `collect_fleet` — and every SOP invokes this collector as
    `collect.py … > manifest_<audit>.json`, so by then the shell has already
    truncated the file. The run loses the whole fleet to one bad object
    instead of one cluster, and the operator sees an empty manifest rather
    than a reason. `gate-failed` is the shape the document already carries for
    "enumerated, could not be read": the validator counts it as a scope loss
    and the remaining clusters still publish.
    """
    print(
        f"[collect] {cluster.get('project', '?')}/{cluster.get('name', '?')}: "
        f"collector raised {type(exc).__name__}: {exc}",
        file=sys.stderr,
    )
    return {
        "name": cluster.get("target")
        or target_name(cluster.get("project", ""), cluster.get("location", ""), cluster.get("name", "?")),
        "project": cluster.get("project", "?"),
        "location": cluster.get("location", "?"),
        "autopilot": bool(cluster.get("autopilot")),
        "outcome": OUTCOME_GATE_FAILED,
        "error": f"collector raised {type(exc).__name__}: {exc}"[:ERROR_EXCERPT_CHARS],
    }


def summary_line(manifest: dict) -> str:
    """The one line the shell shows after stdout went to the manifest file.

    The manifest runs to thousands of lines and each cluster's candidates sit
    below its `commands`, so a run that reads the head sees commands only. One
    did exactly that: it copied `commands` into `checks_run`, wrote an empty
    `findings`, and published a clean audit over a fleet the collector had
    flagged. `fleet_drift.candidate_summary` prints the same fact for the same
    reason; unlike drift's, every check here is mechanical, so there is no
    hand exclusion to name.
    """
    clusters = manifest.get("clusters") or []
    collected = [c for c in clusters if c.get("outcome") == OUTCOME_COLLECTED]
    by_check: dict[str, int] = {}
    for cluster in collected:
        for candidate in cluster.get("candidates") or []:
            check = candidate.get("check", "?")
            by_check[check] = by_check.get(check, 0) + 1
    head = (
        f"{len(collected)} cluster(s) collected, {len(clusters) - len(collected)} "
        f"other target(s); {sum(by_check.values())} candidate(s) to report"
    )
    if not by_check:
        return head
    counts = "; ".join(f"{check}: {n}" for check, n in sorted(by_check.items()))
    return (
        f"{head} -- {counts}. Each one under `clusters[].candidates` is a "
        "finding for the document, not only what `commands` shows"
    )


def collect_fleet(
    audit_id: str,
    project: str | None = None,
    *,
    run: RunFn = default_run,
    max_workers: int = MAX_WORKERS,
    workspace: Path | None = None,
) -> dict:
    checks = CHECK_TABLES.get(audit_id)
    if not checks:
        raise ValueError(f"no check table for {audit_id!r} yet — see this file's module docstring")

    started_at = time.strftime(TIMESTAMP_FORMAT, time.gmtime())
    discovery = discover_fleet(project, run=run)

    clusters: list[dict] = []
    not_running: list[dict] = []
    failed_projects: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        listings = {pool.submit(enumerate_clusters, p, run=run): p for p in discovery.projects}
        for future in as_completed(listings):
            listed_project = listings[future]
            try:
                running, stopped, error = future.result()
            except Exception as exc:  # noqa: BLE001 — see crashed_entry
                log(f"{listed_project}: enumeration raised {type(exc).__name__}: {exc}")
                running, stopped, error = [], [], f"enumeration raised {type(exc).__name__}: {exc}"
            clusters.extend(running)
            not_running.extend(stopped)
            # Not `elif`: a partial listing returns clusters *and* an error.
            if error:
                failed_projects[listed_project] = error[:ERROR_EXCERPT_CHARS]
    # The pool finishes projects in any order; the manifest should not.
    clusters.sort(key=lambda c: c["target"])
    not_running.sort(key=lambda e: e["name"])

    # Built once for the whole fleet, before the pool: every cluster's
    # candidates resolve against the same clone, and walking it per cluster
    # would read the same tree sixteen times to get the same answer.
    declarations = workload_declarations(workspace) if workspace else {}
    releases = release_declarations(workspace) if workspace else {}
    namespaces = namespace_directories(declarations, releases, workspace)

    results = [None] * len(clusters)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                collect_cluster,
                cluster,
                audit_id,
                checks,
                run=run,
                declarations=declarations,
                releases=releases,
                namespaces=namespaces,
            ): index
            for index, cluster in enumerate(clusters)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 — see crashed_entry
                results[index] = crashed_entry(clusters[index], exc)

    # A project whose `clusters list` failed contributed no clusters and, with
    # no entry of its own, no evidence of that either -- the manifest would
    # read exactly like a fleet that never had them. A `gate-failed` target is
    # what makes `cross_check_manifest` require the document to account for it.
    entries = results + not_running
    entries += [
        {
            "name": f"{PROJECT_TARGET_PREFIX}{failed}",
            "project": failed,
            "location": "global",
            "outcome": OUTCOME_GATE_FAILED,
            "error": error,
        }
        for failed, error in sorted(failed_projects.items())
    ]
    # The same one rung up: a `projects list` that failed, or a `--project`
    # that skipped it, took the other projects' names with it.
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
        "audit": audit_id,
        "started_at": started_at,
        "finished_at": time.strftime(TIMESTAMP_FORMAT, time.gmtime()),
        "clusters": entries,
    }
    if discovery.error:
        # The SOP's zero-cluster rule tells the worker what an empty fleet with
        # this key set means: do not publish, report the failure.
        manifest["error"] = discovery.error
    elif failed_projects and not clusters and not not_running:
        # Discovery named projects and the run read no cluster from any of
        # them. The SOP redirects stdout into the manifest without checking
        # the exit status, so this key, not `main`'s return code, is what
        # stops the run.
        first, error = sorted(failed_projects.items())[0]
        manifest["error"] = (
            f"no cluster could be read: {len(failed_projects)} of "
            f"{len(discovery.projects)} project(s) in scope failed `clusters list` "
            f"and the rest held none; {first}: {error}"
        )[:ERROR_EXCERPT_CHARS]
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audit", choices=sorted(CHECK_TABLES))
    parser.add_argument(
        "--project",
        help=(
            "single project to audit; omit to audit the active project and every "
            "project `gcloud projects list` returns"
        ),
    )
    parser.add_argument(
        "--workspace",
        help=(
            "the GitOps clone `audit_report.py start` made, so each candidate "
            "carries where the repository declares its object; omit and no "
            "candidate is annotated"
        ),
    )
    args = parser.parse_args(argv)
    workspace = Path(args.workspace) if args.workspace else None
    if workspace is not None and not workspace.is_dir():
        # Loud, and not fatal. A typo here would otherwise annotate nothing and
        # read exactly like a repository that declares none of the fleet, which
        # is the answer that sends every finding to `manual`.
        print(
            f"collect.py: --workspace {str(workspace)!r} is not a directory; "
            "no candidate will carry a declaration",
            file=sys.stderr,
        )
        workspace = None
    manifest = collect_fleet(args.audit, args.project, workspace=workspace)
    print(json.dumps(manifest, indent=2))
    log(summary_line(manifest))
    if manifest.get("error"):
        # The manifest is still written -- the shell has already redirected
        # stdout -- but a run that found nothing to audit is a failed run.
        log(f"WARNING: {manifest['error']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
