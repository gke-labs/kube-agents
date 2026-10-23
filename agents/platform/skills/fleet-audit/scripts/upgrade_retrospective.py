#!/usr/bin/env python3
"""upgrade_retrospective.py — the deterministic collector behind the
`upgrade-retrospective` review (docs/designs/upgrade-failure-catalogue.md).

After a GKE upgrade nobody writes down what the upgrade did to the workloads
on the cluster, so the next cluster meets the same failure. This collector
produces the deterministic half of the retrospective:

  (A) what happened  — the `UPGRADE_MASTER` / `UPGRADE_NODES` operations in
      the window, with versions before (the ledger) and after (now);
  (B) what failed    — pods not Ready, pods Pending, nodes NotReady and the
      Warning events since the first operation, each classified against the
      catalogue by signature;
  (C) detect and mitigate next time — rendered from `MITIGATIONS`, one row
      per catalogue entry, naming the cluster's own object;
  (D) the guards     — one entry per classified failure in `guards.json`,
      which the daily readiness watch will read before the next upgrade.

Symptoms, (C) rows and guards are keyed by a pod's top owner (Deployment,
CronJob, StatefulSet, DaemonSet; a bare pod stays a Pod), so one finding
covers every replica and a guard goes away when the owner's replacement pods
are healthy. A budget stays keyed as a budget.

A cluster is reviewed when it is *new* (absent from the ledger) or *upgraded*
(a version differs from the ledger, or an upgrade operation started since the
last run). Every other cluster is listed as unchanged.

The Markdown report has three sections, Errors, Warnings and Info. An entry
under the first two is one incident: one owner object (or budget) on one
cluster, carrying the four parts above inline. Error is a high-confidence
classified symptom on a user workload, an operation GKE reported failed, or a
node NotReady after its pool was upgraded; Warning is a medium-confidence or
unclassified symptom, anything in a system namespace, and a guard still live
from an earlier run on a cluster this run did not review; Info is each clean
review's (A) summary, the unchanged clusters, and the reads that failed. A
read that fails is listed rather than failing the run: a cluster this run
could not reach is a gap the report names, not a reason to drop the rest.
Each reviewed cluster also gets an Info block: the next upgrade (the
channel's current default as the target, the maintenance window and when it
next opens, exclusions with scope and end), the catalogue shapes present
(static before-signals: a budget allowing no disruption, a selector on a
deprecated node label, an image on a retired registry, an in-tree PD volume,
a user DaemonSet on hostNetwork or the containerd socket, state on a
node-local volume, a CUDA pin on a GPU workload, a fail-closed webhook with
no backend, a pre-cgroup-v2 runtime image on an exposed pool; GKE's own
agents are counted on one line),
and the baseline recorded. Shapes are risks, never incidents; each writes a
`risk` guard beside the symptoms' `failure` guards. The JSON carries the same
grouping under `sections` beside the per-cluster `reviews`.

The fleet is the install's declared scope, as every fleet-audit collector
reads it: the SOP calls the platform_control `fleet_scope` tool and passes
its `collector_args` verbatim, `--scope-projects` with the resolved project
set, swept exactly as handed with nothing listed, and `--scope-unread` with
the declared projects the install could not read, whose known clusters are
listed under "Reads that failed" and are `unreachable` in the manifest, the
unread note its top-level `limitations` and, as the sibling collectors
record a scope they could not enumerate in full, a `gate-failed`
`project/UNENUMERATED_PROJECTS` target the document skips, so the run is
partial whether or not the ledger knows a cluster of those projects. The
tool's `declared-scope=unresolved` row names no project (the render
answered, or a folder, organisation or selector lookup is unresolved), so
the handed set is then not the fleet: a full run handed it prunes no
project and holds every ledger project outside the handed set the same
way, recording the fleet as the ledger's set plus the projects it read. A
project whose Kubernetes Engine API is off is an empty project for both
listings, never a gap. `--project` names one project and
is a hand run's override: free on an install that declares no scope, and on
one that does admitted only beside the scope flags and for a project inside
the readable scope; `--cluster` names its project under the same rule.
Without any of them, on an install that declares no scope, the projects are
the active gcloud project plus `gcloud projects list`, and a listing that
failed or did not name the active project is incomplete: the run sweeps
what it has, a full run holds every ledger project outside it the same way,
and the listing's error is the gap; on a sandbox whose operator says a scope is declared
(`KUBEAGENTS_SCOPE_DECLARED`) a run without the flags is refused and lists
nothing. The shared `fleet_scope_args` module (agents/platform/scripts)
holds the flags and the rule, as for `collect.py` and `patch_readiness.py`.

Only a run invoked with `--full` is *full* (`--full` takes the handed scope,
or the listing on an install that declares no scope, and cannot be combined
with `--project`, `--cluster` or `--since`): the scope it was handed is the
fleet it records in the ledger, a record of the last full run's scope; it
prunes the entries and guards of a project the scope no longer names with
their clusters, adds the clusters of a project that joined, prunes departed clusters,
refreshes every fleet cluster's stored symptom set (pods, nodes and owners,
one list each), writes `reports/<finish-UTC>.md` and moves
`upgrade-retro-report.md` to it. A roster project whose listing failed is
that project's gap, not the run's: its entries and guards stay unchanged,
its known clusters are listed under "Reads that failed", and the run stays
full. Every other run is *scoped*, whatever its `--project` set: it reviews
its targets, never adds a project to the fleet (a cluster outside it is
reported "outside the fleet; not recorded beyond the operations reviewed"
and gets a ledger entry holding only the operations reviewed, and no
guard), prunes nothing, reports no stale guard outside its scope, writes
`reports/<finish-UTC>-scoped.md` and leaves the link alone; `--since` is a
hand run that widens the window and advances no last-run time.
`--manifest-file` also writes the fleet-audit collector manifest
(docs/designs/fleet-audit-collector-manifest.md): every enumerated target
with an outcome (`collected`; `unreachable` when its credentials failed;
`gate-failed` when its reads failed, its project's listing failed or it is
upgrading now; `out-of-scope` outside the fleet), the command behind each
of the SOP's checks (`CHECK_READS`; an unchanged cluster carries all six,
backed by the operations listing that found no upgrade, or by the read the
re-check made for the checks that read pods or nodes), `checks_unevaluated`
for a check whose read ran and failed, never backed by the listing, a
candidate per Error (`major`) and Warning (`minor`) with the
mitigation text the report prints, the identity tuples of guards held but
not re-observed, and the per-cluster facts the SOP copies. Each check has
one deciding read (`CHECK_READS`) and feeding reads: events, budgets, owners,
workloads and the nodes list feed the pod checks, the operations listing the
node check; `limitations` names a failed deciding or feeding read, never the
server config or the storage, webhook and endpoint-slice reads, which cost
Info alone (`INFO_ONLY_READS`). A failed upgrade operation is an incident of
its own kind, `Operation/<name>`, with a `failure` guard re-checked through
the operations listing until a later operation of the same type on the same
target reaches DONE without error, the operation is listed clean, or its
target pool is gone; an operation the listing no longer returns holds the
guard, since GKE's record retention is not a retry. The newest fourteen reports of each kind are kept. Each
symptom carries an onset. The pod's own evidence is every symptom's
onset: a
Pending pod's scheduling transition or start; a not-Ready pod's `Ready`
transition to False, else its start (the earlier of the two once it has
restarted), never its latest crash. A pod created inside a pool
operation's window is marked recreated, which is ambiguous only for a
failure a recreation carries over (a crash loop, an OOM kill, an image pull
failure); for those the owner is consulted as proof of age only: a
Deployment whose `Available=False` or `Progressing=False` transition
predates the window makes the symptom predate the upgrade; a transition
inside the window proves nothing, a ReplicaSet's age proves nothing (a pod
can run for months on an old ReplicaSet and fail only on the rebuilt node,
entry 14's own mechanism; a ReplicaSet created inside the window is noted
as a rollout during it), and with no proof a first run grades it medium
with the recreated-pod reason until a later full run settles it through the
stored set. A Pending replica created
inside the window is the one the drain displaced and is new. Events carry
their first observation, nodes their condition's transition; a budget hold
and a node not back are keyed by their operation in the stored set, so this
upgrade's hold is never "already recorded" from the last one; one whose onset predates the window's first operation, or
that the previous full run recorded, is a Warning that says so, never an
Error; with no readable onset the stored set decides, and a first run grades
by onset alone and says so. A pod whose last activity predates the window
is not graded again, and a failure guard whose object the API still shows
failing is held by the merge as a re-check holds it, with its candidate
carrying the persistence. Confidence is high only when the signature names
the entry's own mechanism and an operation in the window reached the object
(its pool; the control plane for 6 and 7); entry 14 also needs a runtime
image below the floor, entry 20 the same image still running on an
untouched pool. Row 12 (a node label removed) is decided by the nodes read,
not the scheduler's message: it holds only when no node carries the pod's
pinned labels, and is medium unless the label is in `DROPPED_NODE_LABELS`,
empty until a GKE source names a removal. A crash record beside no ledger
or guards file blocks every run until
`--reset-ledger` archives it; a run starts from empty only when neither
exists.

The store (`ledger.json`, `guards.json`, `reports/`) lives on the shell
sandbox's data volume under /opt/data/upgrade-retrospective, readable by the
agent's tools in any session and surviving restarts. One run at a time: an
exclusive lock on `.lock` there is held for the whole run, and a second run
exits 0 saying so with nothing written (`--dry-run` takes no lock).
`--probe-lock` tests the lock without taking it: a non-blocking flock
attempt held for no time that reads and writes nothing (a missing lock file
is free and is not created), exit 0 when free and 1 with the holder line
when another run holds it, so the SOP probes before `start`; a run that
still loses the lock, taken between the probe and the run, exits with the
lock line and writes nothing, and the SOP releases the in-flight note at
once with a limitations-only `finish` (its step: `--no-collector-manifest`,
since the collector wrote none this run), not the note's two-hour TTL; the
lock line and the crash-record line end with the projects the run resolved
for its route (`this run's projects: ...`), which that `finish` lists. The
run holds its own deadline under the SOP's 600-second budget
(`RUN_DEADLINE_S`, from the process's start): every subprocess gets the
smaller of its own timeout and the time left, one attempted past the deadline
fails at once with the budget as its error, no further cluster is started,
what was read is written as a partial run, and the clusters not reached
are held, `gate-failed` with the budget as the reason, and read first next
run (reviews run in last-reviewed order), so a fleet whose reads do not fit
one run is read across runs rather than killed every week with nothing
written. A run
writes its report, then the JSON, then the guards, then the ledger last, each
atomically, so a crash leaves at most a report with no ledger advance. A
ledger or guards file that exists but cannot be read, or carries another
version, is moved aside as `<name>.unreadable-<ts>` and the run stops: that
file is a crash record, not a re-baseline, and a run proceeds as a first run
only when neither file nor a record of it exists; a guards file absent beside
a live ledger is refused the same way. `--reset-ledger` archives the records
and the live files and marks the store, so the next full run is a baseline
whose manifest carries `after_reset`, which the SOP calls no `finish` for. A cluster with an upgrade operation
still in flight, or with one that reached DONE less than fifteen minutes
before the run (the settle floor, on every route), is not reviewed; Info
lists it as upgrading now or as settling, nothing is read from it and no
guard is written, its older operations wait with it, and only operations
that ended inside the window count. An unchanged cluster
that still holds a live guard is re-read cheaply every run -- only the reads
its guards need -- so a guard clears when an operator's fix lands between
upgrades, without waiting for the next one. The weekly run therefore
re-reads every cluster that holds a guard, and with risk guards present
that is most of the fleet; the SOP sizes the schedule for that. A failure
guard whose only source was an event is re-checked through the events read:
refreshed while an event in the API server's hour still names the object, or
the pods read shows the object failing, cleared when both answer and neither
does; a guard whose re-check read failed is held as it stands.

Every subprocess goes through `default_run`; tests inject a fake in its place.
Nothing here writes to a cluster: `gcloud ... list`, `get-credentials` into a
private kubeconfig, and `kubectl get`.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple

# Where the image and the shell sandbox ship the platform scripts the
# collectors share (deploy/docker/Dockerfile and deploy/sandbox/Dockerfile
# copy them to /opt/defaults/scripts), then the checkout's own
# `agents/platform/scripts` for a run from the repository: the search path
# `collect.py` and `patch_readiness.py` use for `fleet_scope_args`. The
# checkout's copy only when there is one: a file three or fewer directories
# below `/` has no parents[3].
SHARED_SCRIPT_DIRS = (
    "/opt/defaults/scripts",
    "/opt/data/scripts",
    *([str(Path(__file__).resolve().parents[3] / "scripts")] if len(Path(__file__).resolve().parents) > 3 else []),
)
for _shared_dir in SHARED_SCRIPT_DIRS:
    if _shared_dir not in sys.path:
        sys.path.append(_shared_dir)
import fleet_scope_args  # noqa: E402

# The install's declared scope, handed in by the SOP from the platform_control
# `fleet_scope` tool (`--scope-projects`, `--scope-unread`), one holder per
# collector module as the siblings keep it: set from the scope flags by
# `validate_args`, which `main` and `collect_fleet` both call first.
declared_scope = fleet_scope_args.DeclaredScope()

LEDGER_VERSION = 1
GUARDS_VERSION = 1

HERMES_HOME_ENV = "HERMES_HOME"
DEFAULT_HERMES_HOME = "/opt/data"
# The store: ledger, guards and reports. The collector runs in the agent's
# shell sandbox, whose /opt/data is the sandbox's own volume rather than the
# gateway's profile volume, so the path is absolute there and not derived
# from HERMES_HOME; `UPGRADE_RETROSPECTIVE_HOME` overrides it.
STORE_HOME_ENV = "UPGRADE_RETROSPECTIVE_HOME"
DEFAULT_STORE_DIR = "/opt/data/upgrade-retrospective"
DATA_SUBDIR = "upgrade-retrospective"
LEDGER_FILENAME = "ledger.json"
GUARDS_FILENAME = "guards.json"
REPORTS_SUBDIR = "reports"
# One run at a time per store: an exclusive, non-blocking lock on this file.
LOCK_FILENAME = ".lock"
REPORT_IS_LINK_TEXT = "--report must not be named {name}: that is the link a full run points at the newest report"
LOCK_HELD_TEXT = "another retrospective run holds {path} since {since}; waited {minutes} min for it; nothing written"
# `--probe-lock`, the SOP's probe before `start`: a non-blocking flock attempt
# held for no time, reading and writing nothing; the held line shares
# `LOCK_HELD_TEXT`'s form so one match reads both.
LOCK_PROBE_FREE_TEXT = "{path} is free; no retrospective run holds it"
# The probe is the step every route runs before `start`: it names a pending
# reset, so the SOP runs the baseline as the no-repository route, without
# `start`, and leaves no in-flight note standing.
LOCK_PROBE_RESET_PENDING_FORMAT = "{line}; a reset is pending ({marker}): the next full run is a baseline the SOP files nothing from, run without start"
LOCK_PROBE_HELD_TEXT = "another retrospective run holds {path} since {since}; nothing started"
PROBE_LOCK_WITH_OTHERS_TEXT = "--probe-lock takes no other mode flag: it tests the lock and nothing else"
PROBE_LOCK_EXCLUSIVE = ("full", "after_upgrade", "cluster", "since", "dry_run", "reset_ledger", "report", "output", "manifest_file", "no_report")
# The reset is the same kind of flag: it archives the store and runs nothing,
# so a run-mode flag beside it would be ignored silently. --dry-run, --ledger
# and --guards are its own.
RESET_LEDGER_WITH_OTHERS_TEXT = "--reset-ledger takes no other mode flag (--dry-run, --ledger and --guards apart): it archives the store and runs nothing"
RESET_LEDGER_EXCLUSIVE = ("full", "after_upgrade", "cluster", "since", "report", "output", "manifest_file", "no_report")
# Three triggers write the same files: a run that finds the lock held waits
# this long for it, polling, and only then prints the line above and exits.
# Two minutes: the SOP runs the collector in the foreground under the
# terminal tool's 600-second budget, and the wait must fit under it with the
# run; a scoped run of a few clusters, the collision that matters, finishes
# inside it.
LOCK_WAIT = timedelta(minutes=2)
# The run's own deadline under the SOP's foreground `timeout: 600`, measured
# from the process's start, the lock wait included. Every subprocess the run
# starts gets the smaller of its own timeout and the time left under it, and
# one attempted past it fails at once with the budget as its error
# (`BUDGET_READ_TEXT`), so the reads end by the deadline (worst case, a read
# started at 479 s runs one second) and the margin to the terminal's limit
# covers the merge and the writes, not a read; a cluster whose API server
# hangs costs the run the time left, never a timeout per read. Past the
# deadline no further cluster is started, what was read is written as a
# partial run, and the clusters not reached are held and read first next
# run, in last-reviewed order, so a fleet whose reads do not fit one run is
# read across runs rather than killed every week with nothing written.
# `fleet_waste.py`'s `PROJECT_READ_DEADLINE_S` bounds only when a project's
# reads start; this one bounds each read.
RUN_DEADLINE_S = 480
# A read the deadline refused outright, and one it ended part-way (given the
# time left rather than its own timeout): both carry the budget's prefix, so
# the cut counts and the report name the budget rather than a hung read.
BUDGET_PREFIX_FORMAT = "the read budget ({seconds} s)"
BUDGET_READ_TEXT = BUDGET_PREFIX_FORMAT + " was spent before this read"
BUDGET_CUT_TEXT = BUDGET_PREFIX_FORMAT + " ended this read after {given} s"
BUDGET_SPENT_TEXT = "{cluster}: not read this run; the read budget ({seconds} s) was spent after {count} cluster(s); ledger entry and guards kept unchanged, read first next run"
BUDGET_SPENT_REASON = "not read this run: the read budget ({seconds} s) was spent; held, read first next run"
RECHECK_DEFERRED_TEXT = "the read budget ({seconds} s) was spent before its re-check started"
RECHECK_DEFERRED_LINE = "re-check not started, the read budget spent; {guards} guard(s) held"
INFO_BUDGET_SPENT = "Read budget spent: {seconds} s after {reviewed} review(s) and {rechecked} re-check(s); {cut} review(s) and {cut_rechecks} re-check(s) cut mid-read, {deferred} cluster(s) held for the next run, {deferred_rechecks} re-check(s) not started."
PRUNE_FAILED_FORMAT = "reports not pruned this run: {error}"
INFO_AFTER_RESET = "Baseline after --reset-ledger: the store was archived under {archive} at {at}; every cluster is reviewed as new, the manifest carries after_reset, and the SOP files nothing from this run."
LOCK_POLL_INTERVAL = timedelta(seconds=15)
STATE_UNREADABLE_DRY_RUN_TEXT = "{path} {why}; a dry run moves nothing and stops here. Nothing written."
STATE_NOT_MOVED_TEXT = "{path} {why} and could not be moved aside ({error}); it is unchanged in place. Fix or move it by hand, then rerun. Nothing written."
STATE_UNREADABLE_TEXT = "{path} {why}; moved to {aside}. A set-aside ledger or guards file is a crash record, not a re-baseline: restore it or remove it on purpose, then rerun. Nothing written."
EXIT_USAGE = 2
EXIT_LOCK_HELD = 1
# Reports are named by the run's finish time in UTC; a scoped run (one that
# did not cover the whole fleet) is marked so it never stands for the fleet.
REPORT_TS_FORMAT = "%Y%m%dT%H%M%SZ"
REPORT_FILENAME = "{ts}{scoped}.md"
REPORT_JSON_FILENAME = "{ts}{scoped}.json"
SCOPED_SUFFIX = "-scoped"
REPORT_NAME_RE = re.compile(r"^(\d{8}T\d{6}Z)(-scoped)?\.(md|json)$")
# How many reports of each kind the store keeps.
REPORTS_KEPT = 14
LATEST_REPORT_LINK = "upgrade-retro-report.md"
# The report file and its link are swapped in through these.
LINK_TEMP_SUFFIX = ".tmp-link"
# The same kubeconfig directory `collect.py` and the SOPs use, one file per
# cluster, passed per command rather than exported; `collect.prepare_kubeconfig`
# makes it usable for whichever uid runs (this uid's and closed to other
# uids, another uid's directory moved aside, anything at the file's name that
# is not this uid's own file out of the way) before `get-credentials` writes
# into it.
KUBECONFIG_SUBDIR = ".kubeconfigs"
KUBECONFIG_FILENAME = "kubeconfig_{project}_{cluster}_{location}.yaml"

# The first run has no ledger, so it reviews this much history.
DEFAULT_SINCE_DAYS = 14
# `--since` also takes an ISO timestamp or `<n>d`.
SINCE_DAYS_RE = re.compile(r"^(\d+)d?$")
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
REPORT_DATE_FORMAT = "%Y-%m-%d"

GCLOUD_TIMEOUT_S = 120
KUBECTL_TIMEOUT_S = 120
# What `timeout(1)` exits with, so a timed-out read reads the same as one the
# shell cut off.
TIMEOUT_RC = 124
MAX_WORKERS = 4
ERROR_EXCERPT_CHARS = 300
# How much of a message a symptom keeps: enough for the signature and the
# object it names, short enough that one FailedScheduling does not fill a page.
MESSAGE_EXCERPT_CHARS = 400
# Every file this collector writes is 0644 whatever the writer's umask, the
# store's and a caller-named `--output`, `--report`, `--manifest-file`,
# `--ledger` or `--guards` alike, set after the write as the lock is:
# `mkstemp` makes 0600 under every umask, and a root hand run's output must
# stay readable by the uid-1000 run or `finish` that reads it next (the
# SOP's run and the readiness watch read the store's). The next run replaces
# a file through its directory, so no write bit is needed. Directories have
# their own rule (`STORE_DIR_MODE`): a caller-named path's directory and
# ancestors keep the caller's umask (`output_parent`).
STORE_FILE_MODE = 0o644
TEMP_SUFFIX = ".tmp"
# The store's directories are 0755 and its files 0644 whatever the writer's
# umask, and a run as root hands each directory and file it creates to the
# owner of the nearest directory that already existed: the fleet-audit report
# store's rule (`audit_report.py`'s `REPORT_DIR_MODE`, `REPORT_FILE_MODE` and
# `_adopt_owner`), carried here as the sibling collectors carry their copies.
# Mode alone would not do: a hand run over `kubectl exec` lands as root while
# the tick runs as uid 1000, which can read a root-owned 0755 directory but
# cannot write the next run into it; a world-writable directory would let any
# login on the pod replace the store's files instead. A directory that already
# exists is left as found, mode and owner alike, as the report store leaves
# it: a root run that tightened one an operator had opened for the tick would
# lock the tick out of a store it could write before. The lock is made with
# the umask cleared so it is `LOCK_FILE_MODE` whoever creates it; a
# caller-named output path's directory and ancestors keep the caller's umask
# (`output_parent`); the kubeconfig directory has its own rule in
# `collect.prepare_kubeconfig`.
LOCK_FILE_MODE = 0o644
STORE_DIR_MODE = 0o755
ROOT_UID = 0
# A store path this uid cannot write ends the run with one line, the owner
# named when it is another uid's, as the report store's `_ownership_note`
# names it: the residual a root run from before `_adopt_owner`, or a
# directory made by hand, leaves, which only an operator can repair.
STORE_DENIED_FORMAT = "{path}: {error}{note}; the run stops here and nothing more is written"
STORE_OWNER_NOTE_FORMAT = " ({where} is owned by uid {owner} and this run is uid {uid}; a run as another user made it, and it stays unwritable until its ownership is restored)"
STORE_DENIED_PATH_UNKNOWN = "the store"

# Project discovery, as `collect.py`'s `discover_fleet`: `--project` (one
# project, a hand run's override), else the declared scope the SOP handed
# from the fleet_scope tool (`fleet_scope_args`), else the active gcloud
# project plus every project `gcloud projects list` returns. No environment
# variable beyond the sandbox's scope guard: this script reads the same fleet
# as its three siblings in this directory.
# gcloud's words for a project whose Kubernetes Engine API is off: it cannot
# hold a cluster, so its failed listing is an empty project, not a lost one.
API_DISABLED_MARKERS = ("SERVICE_DISABLED", "accessNotConfigured", "has not been used in project")
# Only a refusal naming this project is that answer: a quota project with the
# API off names itself in every `--project X` refusal, and read as X's own it
# would list every project empty and let a full run prune the fleet. The rule
# is `collect.refusal_owner`, which `fleet_waste.py` and `fleet_stockout.py`
# read too; a refusal that is not this project's is a failed read, held.
API_REFUSED_OTHER_FORMAT = "{listing} refused and {why}: {error}"

OP_UPGRADE_MASTER, OP_UPGRADE_NODES = "UPGRADE_MASTER", "UPGRADE_NODES"
UPGRADE_OPERATION_TYPES = (OP_UPGRADE_MASTER, OP_UPGRADE_NODES)
# Bounded by end time, never by start: every rule downstream is about when an
# operation ended or whether it is still in flight, and a long one (a
# blue-green soak) starts well before the window it ends in.
OPERATIONS_FILTER = "operationType:({types}) AND (endTime>={since} OR status!={done})"
# `targetLink` is `.../projects/<n>/(zones|locations)/<loc>/clusters/<name>[/nodePools/<pool>]`.
TARGET_LINK_RE = re.compile(r"/(?:zones|locations)/(?P<location>[^/]+)/clusters/(?P<cluster>[^/]+)(?:/nodePools/(?P<pool>[^/]+))?$")
CONTROL_PLANE_TARGET = "control plane"
# Entry 1's after-signal: a surge upgrade honours a budget for up to an hour
# per node, so an `UPGRADE_NODES` that ran longer than that per node was held.
DRAIN_HOLD_PER_NODE = timedelta(hours=1)
# GKE's own measure of a drain: an UPGRADE_NODES operation's `progress.metrics`
# carry the nodes it upgraded and how long disruption budgets delayed it. Read
# when present; the wall-clock estimate above is the fallback when they are
# absent, and the evidence says which one spoke.
OPERATION_METRIC_NODES_TOTAL = "NODES_TOTAL"
OPERATION_METRIC_PDB_DELAY = "NODE_PDB_DELAY_SECONDS"
DRAIN_HELD_PREFIX = "drain held by the budget on "
# A budget at zero only because its covered pods are not ready: the hold is
# real, the thing to fix is the covered pods' own incident, not the budget.
BUDGET_DOWNSTREAM_DETAIL_FORMAT = "spec {spec} allows disruption; at zero because {not_ready} of {expected} covered pod(s) are not ready, the covered pods' own incident"
BUDGET_DOWNSTREAM_BEFORE = "A budget at disruptionsAllowed 0 only while its covered pods are not ready; the spec allows disruption."
BUDGET_DOWNSTREAM_MITIGATE_BEFORE = "Nothing on the budget: make the covered pods schedulable and ready (their own incident) and the budget allows disruption again."
BUDGET_DOWNSTREAM_MITIGATE_AFTER = "The drain resumes once the covered pods are ready; the budget needs no change."
DRAIN_HELD_MEASURED_FORMAT = "{pool} ({metric} {seconds} s, measured by GKE)"
DRAIN_HELD_ESTIMATED_FORMAT = "{pool} (estimated from the operation's duration, {minutes} min over {nodes} node(s); no progress metrics)"
NO_PROGRESS_METRICS_TEXT = "no progress metrics"

CLUSTER_KEY_SEPARATOR = "/"
NODEPOOL_LABEL = "cloud.google.com/gke-nodepool"
# A pod's placement pins: its nodeSelector and the `In` expressions of its
# required node-affinity terms, as the scheduler reads them.
AFFINITY_REQUIRED_KEY = "requiredDuringSchedulingIgnoredDuringExecution"
AFFINITY_OPERATOR_IN = "In"
AFFINITY_OPERATOR_GT, AFFINITY_OPERATOR_LT = "Gt", "Lt"
# The four operators `_selector_matches` reads; `Gt`, `Lt` and `matchFields`
# on the node's name are the rest of the NodeSelectorRequirement predicate.
LABEL_SELECTOR_OPERATORS = ("In", "NotIn", "Exists", "DoesNotExist")
NODE_NAME_FIELD = "metadata.name"
# What the scheduler holds a pod to besides its pins: a taint with one of
# these effects refuses an intolerant pod, and a request for an extended
# resource (a domain-qualified name outside kubernetes.io, `nvidia.com/gpu`)
# needs a node that offers it.
TOLERATION_EXISTS = "Exists"
BLOCKING_TAINT_EFFECTS = ("NoSchedule", "NoExecute")
RESOURCE_DOMAIN_SEPARATOR = "/"
BUILTIN_RESOURCE_DOMAIN = "kubernetes.io/"
CGROUP_V2_MODE = "EFFECTIVE_CGROUP_MODE_V2"
CGROUP_V1_MODE = "EFFECTIVE_CGROUP_MODE_V1"

# Namespaces the report orders last. Not excluded: a broken kube-dns is the
# upgrade's doing as much as a broken payments-api.
SYSTEM_NAMESPACES = ("kube-system", "gmp-system")
SYSTEM_NAMESPACE_PREFIXES = ("gke-",)

# Pod phases that are not a symptom on their own.
PHASE_SUCCEEDED = "Succeeded"
PHASE_RUNNING = "Running"
PHASE_PENDING = "Pending"
REASON_ERROR = "Error"
REASON_COMPLETED = "Completed"
REASON_UNSCHEDULABLE = "Unschedulable"
REASON_FAILED_SCHEDULING = "FailedScheduling"
CONTAINER_FAILURE_REASONS = ("OOMKilled", "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerError", REASON_ERROR)
# Symptom categories, selection statuses, and the catalogue entries the
# classifier and the shape detectors decide in code.
CATEGORY_PENDING, CATEGORY_NOT_READY, CATEGORY_NODE, CATEGORY_EVENT, CATEGORY_PDB = "pending", "not-ready", "node", "event", "pdb"
# Entry 3: a Deployment or StatefulSet every replica of which sat on the
# drained nodes or in the drained zone and whose availability fell to zero
# inside one pool operation. Entry 5: a pool more than one minor behind its
# control plane, standing `SKEW_STANDING` or longer since the run that first
# recorded it.
CATEGORY_UNAVAILABLE, CATEGORY_VERSION_SKEW = "unavailable", "version-skew"
UNAVAILABLE_REASON = "Available=False"
SPREAD_KINDS = ("Deployment", "StatefulSet")
# The catalogue's floor for entry 3: a workload with more than one replica.
# A single replica displaced by a drain is entry 2 or 1, never 3.
SPREAD_MIN_REPLICAS = 2
ZONE_LABEL = "topology.kubernetes.io/zone"
SPREAD_POOL_DETAIL = "every replica sat on the drained pool"
SPREAD_ZONE_DETAIL = "every replica sat in the drained zone, not all on the drained pool"
SKEW_REASON = "version skew"
SKEW_MINORS = 2
# Entry 5 is graded on the skew's own age: the describe carries no date for
# the gap, and the control plane's last upgrade is no measure of it (a weekly
# auto-upgrade would reset it), so the age runs from the run that first
# recorded the skew in the ledger's stored set (`LEDGER_SKEW_FIRST_SEEN_KEY`).
# No window arithmetic: a daily window closes every day and a weekly one
# once, and neither says how long the pool has stood behind. One pool on two
# kubelet versions is no row: an open operation holds the cluster, and a
# finished one leaves the pool on one version.
SKEW_STANDING_DAYS = 7
SKEW_STANDING = timedelta(days=SKEW_STANDING_DAYS)
SKEW_CONTROL_PLANE_FORMAT = "pool {pool} at {pool_version}, control plane at {master} ({minors} minors apart)"
SKEW_AGE_FORMAT = "standing {days} d since first recorded at {since}"
SKEW_AGE_FIRST_FORMAT = "first recorded this run"
WINDOW_OPEN_STATE = "the maintenance window is open now"
EXCLUSION_ACTIVE_FORMAT = "exclusion {name} ({scope}) is active until {end}"
SKEW_OPEN_TEXT = "Version skew held: {skews}; {state}, so the roll may still complete; graded on a later run."
SKEW_YOUNG_TEXT = "Version skew: {skews}; {age}; graded once it has stood {limit} d or longer."
# A cluster with no maintenance policy has no window to be too short: row 5
# does not hold there, and the skew is the pool-skew reading the readiness
# watch's shipped rule makes (entry 10); listed, never graded or re-selected.
SKEW_NO_POLICY_TEXT = "Version skew, no maintenance policy: {skews}; entry 5 needs a window, and the readiness watch's pool-skew rule reads a standing skew before the next upgrade; not graded here."
SKEW_RESELECT_REASON = f"version skew recorded at a previous run; graded once it has stood {SKEW_STANDING_DAYS} d"
STATUS_NEW, STATUS_UPGRADED, STATUS_FORCED, STATUS_REGRADED = "new", "upgraded", "forced", "re-graded"
# A symptom against the ledger's baseline from the cluster's last review.
SINCE_FIRST_SEEN, SINCE_NEW, SINCE_BEFORE = "first seen", "new since the last review", "present before"
PREDATES_UPGRADE_TEXT = "predates the upgrade: its onset is before this window's first operation, or the previous full run already recorded it; graded Warning"
FIRST_RUN_GRADING_TEXT = "first run: graded by onset only, no previous symptom set."
# The fleet: the project set a full run records in the ledger. A scoped run
# reviews a cluster outside it but records nothing for it.
LEDGER_PROJECTS_KEY = "projects"
OUTSIDE_FLEET_TEXT = "outside the fleet; not recorded beyond the operations reviewed"
FULL_WITH_CLUSTER_TEXT = "--full names a fleet-wide run and cannot be combined with --cluster"
FULL_WITH_PROJECT_TEXT = "--full takes the scope the fleet_scope tool hands it (--scope-projects), or the listing on an install that declares no scope, and cannot be combined with --project: a run narrowed to one project is a scoped run"
# A declared project the install could not read (`--scope-unread`): its known
# clusters are listed as this run's gap and are `unreachable` in the manifest.
UNREAD_PROJECT_TEXT = "{cluster}: its project {project} is in the install's declared scope but was not read ({outcome}); ledger entry and guards kept unchanged"
# The fleet_scope tool's unresolved row (`fleet_scope_args.UNRESOLVED_SCOPE_ROW`:
# the render's answer when no snapshot answers, or a carried tick or a failed
# folder, organisation or selector lookup) names no project, so the handed set
# is not the fleet (`DeclaredScope.sweep_is_declared` is false): a full run
# prunes no project and holds every ledger project outside the set as it holds
# an unread one, its known clusters this run's gap.
SCOPE_UNRESOLVED_HOLD_TEXT = "{cluster}: its project {project} is outside the projects handed this run, and the install's declared scope is not resolved yet ({row}={outcome}); ledger entry and guards kept unchanged"
# An undeclared install's `projects list` that failed, or answered without the
# active project (a filtered listing, as `collect.discover_fleet` reads it):
# the swept set is not the fleet either, and a full run holds a ledger project
# outside it the same way. The note is the manifest's gap, as `collect.py`'s
# `discovery.partial` is.
LISTING_FAILED_NOTE_FORMAT = "gcloud projects list rc={rc}: {error}; the run swept the active project alone, and how many other projects the fleet holds is unknown"
LISTING_FILTERED_NOTE_FORMAT = "gcloud projects list rc=0 did not name the active project {project}, so it is filtered rather than complete: it returned {count} project(s), and how many other projects the fleet holds is unknown"
LISTING_INCOMPLETE_HOLD_TEXT = "{cluster}: its project {project} is outside the projects this run listed, and the listing was incomplete; ledger entry and guards kept unchanged"
# A listing with no active gcloud project to check it against cannot be called
# complete, so it holds too; one that names no project at all is the failed
# case, as `collect.discover_fleet` treats it.
LISTING_UNCHECKED_NOTE_FORMAT = "gcloud projects list rc=0 with no active gcloud project to check it against: it returned {count} project(s), and whether the listing is complete is unknown"
LISTING_EMPTY_NOTE_TEXT = "gcloud projects list rc=0 returned no project and no active gcloud project is set; nothing was swept, and how many projects the fleet holds is unknown"
# An exit after `start` without a manifest (the lock held past the wait, a
# crash record) ends with the projects the run resolved for its route, so the
# SOP's recovery `finish` lists their clusters without resolving the scope again.
EXIT_PROJECTS_FORMAT = "; this run's projects: {projects}{note}"
EXIT_PROJECTS_NONE = "none"
EXIT_NOTE_FORMAT = " ({note})"
# A `--cluster` names its project under `--project`'s rule: inside the readable scope.
FORCED_OUTSIDE_SCOPE_TEXT = "--cluster {cluster}: {error}"
# A `--cluster` naming a cluster spec.scope.exclude.clusters excludes (#3081):
# refused up front, rather than reported as a cluster its listing did not return.
FORCED_EXCLUDED_TEXT = "--cluster {cluster} names a cluster spec.scope.exclude.clusters excludes; it is not reviewed"
# A cluster key is <project>/<location>/<name>, three non-empty parts; a bare
# name would otherwise be swept as a project id.
CLUSTER_KEY_PARTS = 3
CLUSTER_SHAPE_TEXT = "--cluster {cluster}: a cluster is named <project>/<location>/<name>"
SCOPE_LOG_FORMAT = "scope: the install's declared scope, {count} project(s) from the fleet_scope tool"
FULL_WITH_SINCE_TEXT = "--full names the scheduled fleet-wide run and cannot be combined with --since (a hand run)"
NOT_FULL_TEXT = "no --full: a scoped run"
LISTING_FAILED_TEXT = "{cluster}: its project's listing failed ({error}); ledger entry and guards kept unchanged"
# A project's own listing line under Reads that failed starts with this
# after the project; the default stands in when no such line was written.
CLUSTERS_LIST_PREFIX = "clusters list"
LISTING_FAILED_DEFAULT = CLUSTERS_LIST_PREFIX + " failed"
# gcloud exits 0 when a zone did not respond and lists the clusters it
# reached, warning on stderr (`collect.ZONE_TIMEOUT_MARKER`): the siblings
# read that as an incomplete listing, and so does this collector, since a
# cluster missing from it has not left.
LISTING_INCOMPLETE_FORMAT = "clusters list incomplete: {detail}"
# A cluster whose status rules out reading it (PROVISIONING, STOPPING,
# ERROR: anything but the siblings' `AUDITABLE_STATUSES`) is listed, so it
# is never pruned, and held with its status as the reason: not selected,
# not read, its ledger entry and guards untouched.
NOT_RUNNING_REASON_FORMAT = "cluster status {status}: not read; ledger entry and guards kept unchanged"
NOT_RUNNING_TEXT = "{cluster}: " + NOT_RUNNING_REASON_FORMAT
# A crash record beside no ledger or guards file, or a guards file absent
# beside a live ledger, blocks every run until the operator restores the
# file or runs --reset-ledger, which archives the records and the live files
# and leaves `RESET_MARKER_FILENAME` until a full run has written the
# baseline: every run until then carries `after_reset` in its manifest, so
# the SOP calls no `finish` for it.
UNREADABLE_GLOB_SUFFIX = ".unreadable-*"
RESET_MARKER_FILENAME = "reset-pending.json"
RESET_MARKER_VERSION = 1
AFTER_RESET_KEY = "after_reset"
LEDGER_SKEW_FIRST_SEEN_KEY = "skews_first_seen"
GUARDS_ABSENT_TEXT = "{guards} is absent beside a live {ledger}: a crash record, not a re-baseline, since a run that started from no guards would announce every held finding resolved. Restore it from the newest {reports}/<stamp>.json's guards list, or run --reset-ledger to archive the ledger and start a baseline; until then nothing starts from empty. Nothing written."
LOCK_IS_SYMLINK_TEXT = "{path} is a symbolic link, which the lock may not be; it was not followed and is left as found. Remove it, then rerun. Nothing written."
# A store directory that is a symlink, the store root included, is refused
# before anything is read or written under it: `mkstemp`, `os.replace` and
# `unlink` all resolve a directory link, and the store's directories are the
# agent account's to replace.
STORE_IS_SYMLINK_TEXT = "{path} is a symbolic link, which a store directory may not be; it was not followed and is left as found. Remove it, then rerun. Nothing written."
CRASH_RECORD_GLOB = LEDGER_FILENAME + UNREADABLE_GLOB_SUFFIX
GUARDS_RECORD_GLOB = GUARDS_FILENAME + UNREADABLE_GLOB_SUFFIX
ARCHIVE_SUBDIR = "archive"
CRASH_RECORD_TEXT = "{path} sits beside no live {name}: a crash record, not a re-baseline. Restore it as {name} or run --reset-ledger to archive it; until then nothing starts from empty. Nothing written."
RESET_LEDGER_TEXT = "archived {count} file(s) under {archive}: {names}; the next full run is a baseline the SOP files nothing from (after_reset)."
RESET_LEDGER_NOTHING_TEXT = "nothing to archive: no crash record, and no state file is absent beside the other."
RESET_LEDGER_DRY_RUN_TEXT = "would archive {count} file(s) under {archive}: {names}; nothing moved (--dry-run)."
# The full-run refresh of every fleet cluster's symptom set: pods for the
# symptoms, nodes for the pools, owners so the keys match the review's.
REFRESH_READS = ("pods", "nodes", "owners")
BASELINE_SEPARATOR = "|"
ENTRY_BUDGET, ENTRY_CAPACITY, ENTRY_SPREAD, ENTRY_NODE_LOCAL_STATE, ENTRY_WINDOW, ENTRY_REMOVED_API, ENTRY_WEBHOOK = 1, 2, 3, 4, 5, 6, 7
ENTRY_NODE_LABEL, ENTRY_RUNTIME, ENTRY_CGROUP_V2, ENTRY_OOM_GROUP, ENTRY_NODE_AGENT = 12, 13, 14, 15, 17
ENTRY_GPU, ENTRY_IN_TREE_VOLUME, ENTRY_REGISTRY = 18, 19, 20
# Entries whose signature is text an upgrade did not necessarily cause: high
# only when a node-pool operation in the window touched the pod's pool.
POOL_GATED_ENTRIES = (ENTRY_CAPACITY, ENTRY_GPU, ENTRY_REGISTRY)
GATE_CLOSED_TEXT = "no operation in the window touched this object's pool"
# The operations listing failed: nothing can say what touched the pool.
GATE_UNKNOWN_TEXT = "operations listing failed; pool evidence unknown"
# A pod caught mid-start is not a symptom yet: these waiting reasons, younger
# than the grace, with no restart, are listed under Info as starting.
STARTING_REASONS = ("ContainerCreating", "PodInitializing")
STARTING_GRACE = timedelta(minutes=10)
OUTSIDE_FLEET_GUARD_TEXT = "no guard: the cluster is outside the fleet and is not recorded."
STARTING_TEXT = "Starting, not graded ({count} pod(s) under {minutes} min with containers still being created): {pods}."
# A pod held by scheduling gates (a queue admission controller's) is waiting
# for admission, not failing: listed under Info, never graded.
SCHEDULING_GATED_REASON = "SchedulingGated"
GATED_TEXT = "Scheduling gated, not graded ({count} pod(s) held by scheduling gates, waiting for admission): {pods}."
# `Terminating` is not a phase: deletion shows as `metadata.deletionTimestamp`,
# as `fleet_waste.py` reads it. A pod caught mid-termination (a rollout, a
# scale-down, a delete) is Running and not Ready for seconds, its container
# stopped by SIGTERM: not a symptom, listed as the starting pods are.
TERMINATING_TEXT = "Terminating, not graded ({count} pod(s) being deleted when read): {pods}."
SECONDS_PER_MINUTE = 60
# Entries whose mechanism is the control plane's; their gate is an
# UPGRADE_MASTER in the window rather than a node-pool operation.
CONTROL_PLANE_ENTRIES = (ENTRY_REMOVED_API, ENTRY_WEBHOOK)
# Reasons a Warning event is kept for, plus any message matching
# `EVENT_MESSAGE_MARKERS` whatever its reason.
EVENT_REASONS = (REASON_FAILED_SCHEDULING, "FailedMount", "FailedAttachVolume", "BackOff", "FailedCreate", "Unhealthy", "OOMKilling")
EVENT_MESSAGE_MARKERS = ("failed calling webhook", "no matches for kind")
# Event reasons a pod's own status already carries when the pod is listed.
EVENT_REASONS_IMPLIED_BY_POD = (REASON_FAILED_SCHEDULING, "BackOff")
# The node-problem-detector's kernel OOM event; charged to the OOMKilled pod
# on that node when there is one.
OOM_NODE_EVENT_REASON = "OOMKilling"
NODE_BAD_CONDITIONS = (("Ready", "False"), ("Ready", "Unknown"), ("NetworkUnavailable", "True"))
JOB_OWNER_KINDS = ("Job", "CronJob")
# Entry 6's best-effort markers for a Job pod that calls a removed API.
DEPRECATED_API_MARKERS = ("flowcontrol", "v1beta")

HIGH, MEDIUM = "high", "medium"
UNCLASSIFIED = "unclassified"

# Where a symptom's text comes from, for the signature table below. Each
# scope is a list of strings and a pattern matches any one of them.
SCOPE_REASON = "reason"  # the symptom's reason and each container's state reason, alone
SCOPE_SCHEDULING = "scheduling"  # a Pending pod's PodScheduled message or FailedScheduling event
SCOPE_CONTAINER = "container"  # each container's waiting/terminated message
SCOPE_ANY = "any"  # the symptom's reason and message together, and each container's

# The signature table: (catalogue entry, confidence, scope, pattern). One
# symptom can hit several entries and all are reported; the first hit per
# entry wins, so a `high` row goes before its `medium` sibling. Entries whose
# signature needs context the text does not carry (14/15 on cgroup mode and
# container count, 17 on a node-pool operation, 1 on a budget, 6 on a Job
# pod) are decided in `classify_symptom` from the constants above.
SIGNATURES = (
    (ENTRY_WEBHOOK, HIGH, SCOPE_ANY, re.compile(r"failed calling webhook")),
    (ENTRY_REMOVED_API, HIGH, SCOPE_ANY, re.compile(r"no matches for kind")),
    (ENTRY_IN_TREE_VOLUME, HIGH, SCOPE_ANY, re.compile(r"PersistentVolume's node affinity")),
    (ENTRY_IN_TREE_VOLUME, MEDIUM, SCOPE_REASON, re.compile(r"^FailedAttachVolume$")),
    # A FailedMount is a ConfigMap or Secret as often as a disk, and the
    # kubelet's mount-timeout sentence is the same for both; only a message
    # naming an attach or a persistent volume counts.
    (ENTRY_IN_TREE_VOLUME, MEDIUM, SCOPE_ANY, re.compile(r"^FailedMount .*(?:AttachVolume|PersistentVolume|\bpvc-|\bpv-)")),
    (ENTRY_NODE_LABEL, HIGH, SCOPE_SCHEDULING, re.compile(r"didn't match (?!PersistentVolume)[^,.]*node (?:selector|affinity)")),
    (ENTRY_GPU, HIGH, SCOPE_SCHEDULING, re.compile(r"Insufficient nvidia\.com/gpu")),
    (ENTRY_CAPACITY, HIGH, SCOPE_SCHEDULING, re.compile(r"Insufficient (?:cpu|memory)")),
    (ENTRY_REGISTRY, HIGH, SCOPE_REASON, re.compile(r"^(?:ImagePullBackOff|ErrImagePull)$")),
    # Driver and CUDA error text only: an image name containing `nvidia`
    # in a pull back-off is entry 20, not a driver mismatch.
    (ENTRY_GPU, HIGH, SCOPE_CONTAINER, re.compile(r"Error 803|CUDA driver version|unsupported display driver|NVML|nvidia-container-cli|could not select device driver")),
)
# One entry per symptom: the rows are tried in this order and the first that
# holds wins, so a finding id never flips between runs on the same evidence.
# The most specific discriminator goes first (a webhook named, a removed API
# named, a volume, a GPU, the pool's cgroup mode and the runtime floor before
# "several processes", a registry, a node, capacity on the targeted pool, a
# budget) and 12, the generic selector miss, goes last.
ENTRY_ORDER = (ENTRY_WEBHOOK, ENTRY_REMOVED_API, ENTRY_IN_TREE_VOLUME, ENTRY_GPU, ENTRY_CGROUP_V2, ENTRY_OOM_GROUP, ENTRY_REGISTRY, ENTRY_NODE_AGENT, ENTRY_SPREAD, ENTRY_CAPACITY, ENTRY_BUDGET, ENTRY_WINDOW, ENTRY_NODE_LABEL)
SIGNATURE_ROWS = {entry: [(confidence, scope, pattern) for e, confidence, scope, pattern in SIGNATURES if e == entry] for entry in ENTRY_ORDER}
OOM_SECOND_CAUSE_FORMAT = "entry {entry} may also apply: {count} containers in the pod"
# Text a namespace user can write: an Event's reason and message, and a
# container's termination message. A signature matched only there is medium
# and marked so unless a field the API server sets agrees (a pod phase, a
# container state the kubelet wrote, a condition a controller set), and the
# excerpt is quoted as the object's own text wherever it travels.
TEXT_SOURCE_EVENT = "event text"
TEXT_SOURCE_TERMINATION = "container termination message"
QUOTED_TEXT_FORMAT = '{source} reads "{text}"'
FROM_EVENT_TEXT_DETAIL = "from {source} alone: no field the API server sets agrees; graded medium"
TEXT_AGREED_FORMAT = "from {source}; {agrees} agrees"
PHASE_FAILED = "Failed"
CONTROLLER_FAILURE_CONDITIONS = (("ReplicaSet", "ReplicaFailure", "True"), ("Job", "Failed", "True"))
# The reads per cluster. `owners` is the intermediates a pod's
# ownerReferences stop at: a ReplicaSet names its Deployment and a Job its
# CronJob only in their own metadata.
KUBECTL_READS = (
    ("pods", ["kubectl", "get", "pods", "-A", "-o", "json"]),
    ("nodes", ["kubectl", "get", "nodes", "-o", "json"]),
    ("events", ["kubectl", "get", "events", "-A", "--field-selector", "type=Warning", "-o", "json"]),
    ("pdbs", ["kubectl", "get", "pdb", "-A", "-o", "json"]),
    ("owners", ["kubectl", "get", "replicasets,jobs", "-A", "-o", "json"]),
    # The four below feed the shape detectors only; a failure there loses
    # the risks table, not the review.
    ("workloads", ["kubectl", "get", "deploy,ds,sts,cronjobs", "-A", "-o", "json"]),
    ("storage", ["kubectl", "get", "pv,storageclasses", "-o", "json"]),
    ("webhooks", ["kubectl", "get", "validatingwebhookconfigurations,mutatingwebhookconfigurations", "-o", "json"]),
    ("endpointslices", ["kubectl", "get", "endpointslices", "-A", "-o", "json"]),
)
SHAPE_READS = ("workloads", "storage", "webhooks", "endpointslices")
# The reads that cost Info alone, the risks table and the next-upgrade line:
# a failure there is a "Reads that failed" line, never a limitation and never
# a partial run. Every other read decides or feeds one of the six checks
# (`CHECK_READS` names the deciding one; events, budgets, owners, workloads
# and nodes feed the pod checks, the operations listing the node check).
INFO_ONLY_READS = ("storage", "webhooks", "endpointslices")
RECHECK_ERROR_PREFIX = "re-check: "
# The checks the stream's SOP grades (governance/upgrade_retrospective_sop.md,
# section 3), each with the read that decides it. The findings document the SOP
# hands to the fleet-audit `finish` owes every cluster a `checks_run` list of
# `{check, command}`, so each review, re-check and unchanged row carries
# `commands`: the checks whose read answered, each with that read's command line
# as it ran. An unchanged cluster was decided by the operations listing alone, so
# that one command backs every check there. `test_audit_report.py` holds this
# roster to the SOP's.
CHECK_OPERATION_FAILED = "upgrade-operation-failed"
CHECK_BROKE_WORKLOAD = "upgrade-broke-workload"
CHECK_NODE_BROKEN = "node-broken-after-upgrade"
CHECK_SYMPTOM_TENTATIVE = "upgrade-symptom-tentative"
CHECK_SYMPTOM_UNCLASSIFIED = "upgrade-symptom-unclassified"
CHECK_SYMPTOM_PREDATES = "symptom-predates-upgrade"
OPERATIONS_READ = "operations"
OPERATIONS_LIST_FAILED_TEXT = OPERATIONS_READ + " list failed"
CHECK_READS = (
    (CHECK_OPERATION_FAILED, OPERATIONS_READ),
    (CHECK_BROKE_WORKLOAD, "pods"),
    (CHECK_NODE_BROKEN, "nodes"),
    (CHECK_SYMPTOM_TENTATIVE, "pods"),
    (CHECK_SYMPTOM_UNCLASSIFIED, "pods"),
    (CHECK_SYMPTOM_PREDATES, "pods"),
)
# A kubectl read ran with KUBECONFIG in its environment; the recorded command
# spells that out so it runs as published.
KUBECONFIG_ENV = "KUBECONFIG"

# The collector manifest (docs/designs/fleet-audit-collector-manifest.md), the
# contract the fleet-audit `finish` cross-checks the SOP's document against.
AUDIT_ID = "upgrade-retrospective"
# Where the harness looks for this run's manifest (`audit_report.manifest_path_for`):
# `finish`'s `--no-collector-manifest` waiver and `start`'s scrub read that path
# and no other, so the SOP passes it as `--manifest-file`.
SCRATCH_DIR_ENV = "FLEET_AUDIT_SCRATCH_DIR"
DEFAULT_SCRATCH_DIR = "/opt/data/scratch"
HARNESS_MANIFEST_FILENAME = f"manifest_{AUDIT_ID}.json"
MANIFEST_VERSION = 1
# A digest of this file, carried as `checks_revision` like the sibling
# collectors'; the same width so two sources never collide in a log line.
REVISION_DIGEST_CHARS = 12
CHECKS_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:REVISION_DIGEST_CHARS]
MANIFEST_OUTCOME_COLLECTED = "collected"
MANIFEST_OUTCOME_UNREACHABLE = "unreachable"
MANIFEST_OUTCOME_GATE_FAILED = "gate-failed"
MANIFEST_OUTCOME_OUT_OF_SCOPE = "out-of-scope"
# The target standing for the projects the run could not enumerate, named as
# the sibling collectors name it (`collect.UNENUMERATED_PROJECTS_TARGET`;
# uppercase because a project id cannot be): a `gate-failed` entry carrying
# the scope note, which the document lists under `scope.skipped`, so
# `coverage_gaps` makes the run partial whether or not the ledger knows a
# cluster of the unread projects. The harness reads no top-level
# `limitations` on a manifest; the note is kept there for a reader.
PROJECT_TARGET_PREFIX = "project/"
UNENUMERATED_PROJECTS_TARGET = PROJECT_TARGET_PREFIX + "UNENUMERATED_PROJECTS"
UNENUMERATED_TARGET_LOCATION = "global"
# The fleet-audit harness holds a finding only while a candidate carries the
# same (check, cluster, namespace, object): a guard records the check its
# finding was filed under, and a held guard the run still observes, or cannot
# re-observe, emits a candidate under that check with its persistence.
HELD_PERSISTS_FORMAT = "persists since {since}, {reviews} review(s)"
# The SOP files an Error at `major` and a Warning at `minor`, never `critical`.
MANIFEST_SEVERITY_MAJOR, MANIFEST_SEVERITY_MINOR = "major", "minor"
# A failed operation's candidate names the operation.
OPERATION_OBJECT_KIND = "Operation"
# A candidate's `impact` carries the mitigation text the report prints, which
# runs longer than an evidence excerpt.
MANIFEST_IMPACT_CHARS = 1500
# A review needs these to have answered; the shape reads are optional.
CORE_READS = ("pods", "nodes", "events", "pdbs", "owners")

# Shapes: the catalogue's before-signals, read statically. Each is a risk,
# never an incident. The labels a kubelet stopped setting (entry 12), the
# registries that stopped publishing (20), the in-tree PD provisioner (19),
# the containerd socket a node agent couples itself to (13), and the names
# that say a node-local volume holds state (4).
DEPRECATED_NODE_LABEL_PREFIXES = ("beta.kubernetes.io/", "failure-domain.beta.kubernetes.io/")
# Entry 12's after-signal grades high only when the selector names a label
# GKE's node image or kubelet stopped setting at or before the pool's minor:
# label to (major, minor), each entry with its source the way
# `removed_apis.json` beside `api_deprecation_scan.py` cites one. The table
# is empty. An entry needs a GKE release note or GKE document that names the
# label and the minor, never a kubeadm or upstream change and never memory:
# kubeadm's `node-role.kubernetes.io/master`, lost at 1.24, is a label no
# GKE node carried (the entry-12 reproduction's nodes show `ROLES <none>`
# before and after the rebuild), so it is not an entry; the beta labels
# above are deprecated but still set
# (https://kubernetes.io/docs/reference/labels-annotations-taints/), so they
# are the before-signal, not a drop. Until an entry is sourced every row-12
# match is medium, and the high branch is reached only by the test that
# injects an entry.
DROPPED_NODE_LABELS: dict[str, tuple[int, int]] = {}
LABEL_DROPPED_FORMAT = "{label} dropped in {major}.{minor}"
NO_NODE_CARRIES_FORMAT = "no node carries {labels}"
LABEL_NOT_DROPPED_TEXT = "the catalogue lists no GKE label removal"
# Row 12's discriminator is the nodes read, not the scheduler's message: the
# clause set (the comma-separated reasons after `available: ` up to the
# `preemption:` sentence) is quoted as evidence, and the clauses that are not
# selector misses name the pool's own reason in the detail.
OWN_POOL_REASON_FORMAT = "the pool's own reason: {clauses}"
CLAUSE_LIST_MARKER = "available: "
CLAUSE_SEPARATOR = ", "
RETIRED_IMAGE_HOSTS = ("k8s.gcr.io/", "gcr.io/google-containers/", "gcr.io/google_containers/", "gcr.io/kubernetes-helm/")
IN_TREE_PD_PROVISIONER = "kubernetes.io/gce-pd"
IN_TREE_PD_VOLUME_KEY = "gcePersistentDisk"
PD_CSI_ADDON_KEY = "gcePersistentDiskCsiDriverConfig"
CSI_PD_PROVISIONER = "pd.csi.storage.gke.io"
CONTAINERD_SOCKET_PATH_PREFIXES = ("/run/containerd", "/var/run/containerd")
# A cache is the acceptable use the catalogue names, so it is not here; the
# words are anchored so `nginx-cache` and `wal-e-bin` do not match on a syllable.
STATEFUL_VOLUME_NAME_RE = re.compile(r"(?<![a-z])(?:data|state|db|queue|store|journal|wal|persist)(?![a-z])", re.I)
LOCAL_SSD_HOSTPATH_PREFIXES = ("/mnt/disks", "/mnt/stateful_partition")
GPU_RESOURCE = "nvidia.com/gpu"
TPU_RESOURCE = "google.com/tpu"
# Evidence a pool carries an extended resource when its nodes' allocatable
# no longer says so (entry 18: the device plugin never registered after the
# rebuild) or no node is left: the accelerator label GKE puts on the pool's
# nodes, and the pool's accelerator config in the cluster record, whose
# `accelerators` are GPUs. A taint the pod merely tolerates is no evidence.
ACCELERATOR_LABEL_RESOURCES = {"cloud.google.com/gke-accelerator": GPU_RESOURCE, "cloud.google.com/gke-tpu-accelerator": TPU_RESOURCE}
# The cluster record writes a taint's effect as the API's enum; the nodes
# read, and `_tolerates`, use the scheduler's word.
RECORD_TAINT_EFFECTS = {"NO_SCHEDULE": "NoSchedule", "NO_EXECUTE": "NoExecute", "PREFER_NO_SCHEDULE": "PreferNoSchedule"}
# A CUDA pin in an image tag (`nvidia/cuda:12.2.0-base`) or an env value.
CUDA_IMAGE_PIN_RE = re.compile(r"cuda[:/_-]?(\d+\.\d+)", re.I)
CUDA_ENV_NAME_MARKER = "CUDA"
VERSION_IN_TEXT_RE = re.compile(r"\d+\.\d+")
WEBHOOK_FAIL_CLOSED = "Fail"
# Kubernetes' default admission timeout; a webhook at or above it with no
# namespaceSelector is the catalogue's full shape, below it a lesser one.
WEBHOOK_DEFAULT_TIMEOUT_S = 10
WEBHOOK_LONG_TIMEOUT_S = 10
# A webhook stops a drain only if its rules reach pod creation: core group,
# `pods` (or a wildcard), operation CREATE (or a wildcard); no rules match nothing.
WEBHOOK_POD_GROUPS = ("", "*")
WEBHOOK_POD_RESOURCES = ("pods", "*", "*/*", "pods/*")
WEBHOOK_POD_OPERATIONS = ("CREATE", "*")
WEBHOOK_CONFIGURATION_KINDS = ("ValidatingWebhookConfiguration", "MutatingWebhookConfiguration")
ENDPOINTSLICE_SERVICE_LABEL = "kubernetes.io/service-name"
SINGLE_REPLICA = 1
# A pod whose controller has a scale (a ReplicaSet's, a StatefulSet's) is
# counted by its workload's replicas; any other covered pod (a DaemonSet's,
# a Job's, a bare pod) by itself, as the disruption controller counts them.
SCALED_POD_OWNER_KINDS = ("ReplicaSet", "StatefulSet")
PERCENT_ALL = 100
# Namespaces whose DaemonSets are GKE's own agents: they move with the node
# image, so 13 and 17 there are counted on one line, not reported or guarded.
# `is_system_namespace` covers kube-system, gmp-system and every `gke-*`
# (gke-gmp-system, gke-managed-*); Config Sync's are the one more family.
MANAGED_AGENT_NAMESPACE_PREFIXES = ("config-management-",)
MANAGED_AGENT_ENTRIES = (ENTRY_RUNTIME, ENTRY_NODE_AGENT)
MANAGED_AGENTS_LINE = "{count} GKE-managed agents use host networking or the runtime socket; upgraded with the node image."
# Entry 14: runtimes that read their memory limit from cgroup v1 paths, by
# image repository and the first tag that reads cgroup v2, from the cgroup
# v2 page: JDK 8u372 and 11.0.16; .NET 3.1 (so 2.x and 3.0 are affected).
JAVA_IMAGE_REPOS = ("eclipse-temurin", "openjdk", "adoptopenjdk")
JAVA_TAG_RE = re.compile(r"^(?:jdk-?|jre-?)?(\d+)(?:u(\d+)|\.(\d+)\.(\d+))")
JDK8_CGROUP_V2_UPDATE = 372
JDK11_CGROUP_V2_PATCH = (0, 16)
JDK_FIRST_MAJOR_WITH_CGROUP_V2 = 15
DOTNET_IMAGE_REPO_MARKERS = ("mcr.microsoft.com/dotnet/", "microsoft/dotnet")
DOTNET_TAG_RE = re.compile(r"^(\d+)\.(\d+)")
DOTNET_CGROUP_V2_VERSION = (3, 1)
# The minor at which GKE migrates a cgroup v1 pool to v2.
CGROUP_V2_MIGRATION_MINOR = 33
# Kinds whose spec carries a pod template.
TEMPLATE_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob")
# What the risks table says it looked for when it found nothing.
# What the risks table says it looked for when it found nothing, each with
# the reads it needs; a check whose read failed is listed as not performed.
SPEC_CHECK_READS = ("pods", "owners", "workloads")
SHAPE_CHECKS = (
    ("budgets allowing no disruption and single-replica workloads behind a budget (1)", ("pdbs", "workloads")),
    ("selectors on beta.kubernetes.io / failure-domain.beta.kubernetes.io labels (12)", SPEC_CHECK_READS),
    ("images on a retired registry host (20)", SPEC_CHECK_READS),
    ("in-tree gcePersistentDisk volumes and kubernetes.io/gce-pd StorageClasses (19)", ("storage",)),
    ("DaemonSets on hostNetwork (17) or mounting the containerd socket (13)", SPEC_CHECK_READS),
    ("emptyDir or local-SSD volumes whose name suggests state (4)", SPEC_CHECK_READS),
    ("GPU workloads pinning a CUDA version (18)", SPEC_CHECK_READS),
    ("fail-closed webhooks whose Service has no ready endpoint (7)", ("webhooks", "endpointslices")),
    ("pre-cgroup-v2 runtime images on a cgroup v2 pool or a v1 pool GKE will migrate at 1.33 (14; pinned tags only, floating tags such as 8-jre are not matched)", SPEC_CHECK_READS + ("nodes",)),
    ("not checked statically: a multi-process container (15) is not visible from the spec", ()),
)
NOT_CHECKED_TEXT = "; not checked, their reads failed: "
# Guard kinds: a `failure` broke the last upgrade, a `risk` is a shape
# present before the next one.
GUARD_KIND_FAILURE, GUARD_KIND_RISK = "failure", "risk"
# Which reads can show a guard's finding again. A guard is dropped only when
# every one of them answered on the review that did not see it; a slow
# `kubectl get pods` must not erase a cluster's memory.
FAILURE_GUARD_READS = ("pods", "nodes", "events", "pdbs", "owners")
# A failure guard whose only source was an event adds the events read to its
# re-check: refreshed while an event in the API server's hour still names the
# object (or the pods read shows it failing), cleared when neither does.
GUARD_SOURCE_EVENT = CATEGORY_EVENT
# A failed upgrade operation's guard: `operation` in the entry slot, the
# operation's name as the object, the listing as its source and its start
# recorded, so the next run's listing reaches it; re-checked through the
# operations listing every run makes, so it needs no read.
ENTRY_OPERATION = "operation"
OPERATION_GUARD_TITLE = "An upgrade operation failed"
OPERATION_GUARD_START_KEY = "operation_start"
OPERATION_GUARD_TYPE_KEY = "operation_type"
OPERATION_GUARD_TARGET_KEY = "operation_target"
RECHECK_EVENT_READS = ("events",)
RECHECK_HELD_FORMAT = "{count} guard(s) held, a needed read failed"
STALE_PARTIAL_TEXT = "cluster read partially this run ({failed}); the guard below could not be re-observed and was kept."
# A review that never reached its reads (credentials failed, or the review
# raised after they answered): the review's error, not the five read names.
STALE_UNREAD_TEXT = "cluster not read this run ({errors}); the guard below could not be re-observed and was kept."
STALE_RECHECK_FAILED_TEXT = "cluster re-checked this run but the read failed ({errors}); the guard below was kept."
SPEC_SHAPE_READS = ("pods", "owners", "workloads")
SHAPE_READS_BY_ENTRY = {
    ENTRY_BUDGET: ("pdbs", "workloads"),
    ENTRY_NODE_LABEL: SPEC_SHAPE_READS,
    ENTRY_REGISTRY: SPEC_SHAPE_READS,
    ENTRY_NODE_LOCAL_STATE: SPEC_SHAPE_READS,
    ENTRY_GPU: SPEC_SHAPE_READS,
    # The owner's pools come from the node list; without it every pool counts.
    ENTRY_CGROUP_V2: SPEC_SHAPE_READS + ("nodes",),
    ENTRY_RUNTIME: SPEC_SHAPE_READS,
    ENTRY_NODE_AGENT: SPEC_SHAPE_READS,
    ENTRY_IN_TREE_VOLUME: ("storage",),
    ENTRY_WEBHOOK: ("webhooks", "endpointslices"),
}
# A pod a controller named: ReplicaSet, Job and DaemonSet pods end in a
# five-character hash, StatefulSet pods in an ordinal. Used to charge an
# event on a pod that no longer exists to its owner.
POD_HASH_SUFFIX_RE = re.compile(r"^[a-z0-9]{5}$")
POD_ORDINAL_SUFFIX_RE = re.compile(r"^\d+$")
PREFIX_RESOLVED_KINDS = ("ReplicaSet", "Job", "DaemonSet", "StatefulSet")
# An unreadable or foreign-version state file is moved here, never overwritten.
UNREADABLE_SUFFIX = ".unreadable-{ts}"
# A re-check asks whether a finding is still present, not when it began, so
# it bounds nothing by time.
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# A re-check reads only what a cluster's live guards need: a failure guard
# its pods and owners (nodes for the pool, budgets for an entry-1 guard), a
# risk guard the shape's feeding reads.
RECHECK_FAILURE_READS = ("pods", "owners", "nodes")
RECHECK_BUDGET_READS = ("pdbs",)
# An entry-3 guard is re-observed from the workload's own availability, which
# needs the workloads read; the operation it fell inside cannot be re-read.
RECHECK_SPREAD_READS = ("workloads",)
# Tenant text reaches the Markdown only through `_cell`: control characters
# go, and the three characters that could open a cell, a code span or a line.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
CELL_ESCAPES = (("|", "/"), ("`", "'"))

# The next upgrade, from the cluster record and `get-server-config`.
NO_WINDOW_TEXT = "no maintenance window: an upgrade may start at any hour"
# A maintenance window the parser rejects loses the cluster's next-upgrade
# line, recorded under its read errors with this prefix, never the run.
NEXT_UPGRADE_ERROR_PREFIX = "next upgrade: "
DEFAULT_EXCLUSION_SCOPE = "NO_UPGRADES"
RRULE_FREQ_DAILY, RRULE_FREQ_WEEKLY = "DAILY", "WEEKLY"
RRULE_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
# How far ahead the next window opening is searched: a weekly rule repeats
# inside this many days.
NEXT_WINDOW_SEARCH_DAYS = 8
DAILY_WINDOW_TIME_FORMAT = "%H:%M"
# GKE's default daily window length when the record carries none.
DEFAULT_DAILY_WINDOW = timedelta(hours=4)
SECONDS_PER_HOUR = 3600
ISO_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-gke\.(\d+))?$")
# Symptoms, (C) rows and guards are keyed by a pod's top owner so a finding
# survives the pods being replaced: Pod -> ReplicaSet -> Deployment, Pod ->
# Job -> CronJob, Pod -> StatefulSet/DaemonSet; a bare pod stays a Pod. The
# kinds here are the ones with an owner of their own worth one more hop.
OWNER_INTERMEDIATE_KINDS = ("ReplicaSet", "Job")
OWNER_MAX_HOPS = 3
# Owner kinds that control nothing: a static pod's mirror (`kube-proxy-<node>`)
# carries an ownerReference to its Node. Such an owner is not followed, so the
# pod stays a Pod rather than keying its incident and guard on a node name
# the next pool operation recreates.
NON_CONTROLLER_OWNER_KINDS = ("Node",)
# How a pod-backed symptom's evidence names the pods behind it.
POD_EVIDENCE_FORMAT = "{count} of {total} pods: {evidence}; e.g. {example}"
PRE_EXISTING_PODS_FORMAT = "; {count} pre-existing since {earliest} (e.g. {example})"
# The joint between a pod row's core evidence and its example pod; the notes
# (pre-existing, age proof, rollout) follow and are never cut.
POD_EVIDENCE_EXAMPLE_FORMAT = "; e.g. {example}"
NEW_PODS_FORMAT = "; {count} new since the operation (e.g. {example})"
# Where a pod symptom's onset came from: the owner's dated failure
# condition (or its current ReplicaSet's creation when it has none), or the
# pod's own evidence. A pool upgrade recreates every pod on the pool, so a
# pod-sourced onset on a pod created inside a pool operation's window says
# nothing about when the failure began.
ONSET_FROM_OWNER, ONSET_FROM_POD = "owner", "pod"
OWNER_FAILURE_CONDITIONS = ("Available", "Progressing")
AGE_PROOF_FORMAT = "; owner proves age: {proof} since {since}"
ROLLOUT_IN_WINDOW_FORMAT = "; rollout during the window: current ReplicaSet created {created}"
# The stored-set key of an operation-bound symptom (a budget hold, a node
# not back) names the operation, so a hold against this upgrade is never
# "already recorded" from the last one.
OPERATION_KEY_PREFIX = "op="
RECREATED_DETAIL = "the pod was recreated by the upgrade; the failure may predate it"
RECREATED_TEXT = "the pod was recreated by the upgrade; the failure may predate it: graded medium, a Warning, until a later full run settles it by owner."
# Image references without a host are Docker Hub's.
DEFAULT_IMAGE_HOST = "docker.io"
OOM_REASON = "OOMKilled"
# The scheduler's clause set ends where its preemption sentence begins, not at
# the first period: taint keys (`{cloud.google.com/gke-spot: true}`) and
# `Insufficient nvidia.com/gpu` carry dots of their own. Row 12 quotes the
# set as evidence; the nodes read decides the row.
SCHEDULING_TAIL_RE = re.compile(r"\. (?:no new claims to deallocate, )?preemption:")
WEBHOOK_NAME_RE = re.compile(r'failed calling webhook "([^"]+)"')
IMAGE_HOST_RE = re.compile(r"^([^/]+\.[^/]+|localhost(?::\d+)?)/")

# Section C's table: one row per catalogue entry, condensed from
# docs/designs/upgrade-failure-catalogue.md "The scenarios". `read_today`
# is the reader that covers the entry on `main` now.
# `read_today` is the first sentence of the catalogue's "Read today" line,
# verbatim; the test parses the catalogue to hold the two together.
MITIGATIONS = {
    1: {
        "title": "A PodDisruptionBudget forbids the eviction",
        "before": "A budget whose disruptionsAllowed is 0 for a reason that will not clear (maxUnavailable 0, minAvailable at the replica count, or a singleton behind a budget).",
        "read_today": "the readiness mode of `fleet-upgrade-verification` grades it `blocked`, and the obtainability audit reports it as `blocking-pdb`",
        "mitigate_before": "Give the budget room (maxUnavailable at least 1 or minAvailable below the replica count) and a second replica, or accept the outage inside a window.",
        "mitigate_after": "Fix the budget and the stalled drain resumes; never delete the budget without replacing it.",
    },
    2: {
        "title": "No spare capacity for the displaced pods",
        "before": "A pool whose upgrade settings let a node go before its replacement exists, with requests near allocatable, the autoscaler at its ceiling or quota exhausted.",
        "read_today": "the readiness mode of `fleet-upgrade-verification` grades a pool whose upgrade settings remove a node before its replacement exists by placing the pods of each of its nodes onto the nodes their selectors, affinity and tolerations admit, `blocked` when the pool cannot grow and a pod has no node to take it, a risk when a pod is stranded but the pool keeps some surge or can grow, a note when every pod is placed; the same rule blocks a pool with accelerators that surges with no reservation affinity, and a Pod range whose free per-node blocks are fewer than the surge nodes its pools need; the stockout-prevention audit flags regional GPU, TPU and CPU quota near exhaustion and pools near `autoscaling.maxNodeCount`; quota is unread by the readiness rule",
        "mitigate_before": "Keep the pool on maxSurge at least 1 and maxUnavailable 0, with one node of headroom and quota for the extra node.",
        "mitigate_after": "Add a node or raise the ceiling and the Pending pods schedule.",
    },
    3: {
        "title": "Every replica in one zone or on one node",
        "before": "Replicas of one Deployment on one node or in one zone, with no spread or anti-affinity.",
        "read_today": "the obtainability audit's spread and pinning checks",
        "mitigate_before": "topologySpreadConstraints across zones and hosts, or anti-affinity, and a budget so the drain waits between replicas.",
        "mitigate_after": "The next rollout re-spreads the pods once the constraints are in place.",
    },
    4: {
        "title": "Data on the node is gone",
        "before": "Pods keeping state they cannot rebuild on Local SSD or emptyDir.",
        "read_today": "the readiness report's data-on-the-node rule (`readiness_rules/node_local_state.py`), on every readiness run, as a risk; the fleet waste audit reads `emptyDir` and `hostPath` volumes only as scale-down blockers",
        "mitigate_before": "State on PersistentVolumes or object storage; node-local disk only for what can be rebuilt.",
        "mitigate_after": "Restore from the source of truth.",
    },
    5: {
        "title": "Maintenance window too short, or an exclusion ends mid-roll",
        "before": "Window length against node count times drain time; an exclusion ending inside the planned change.",
        "read_today": "the readiness mode grades the covering exclusion; the security-patch orchestrator reads the window and the exclusions (`no-maintenance-window`, `blocking-exclusion`)",
        "mitigate_before": "A window long enough for node count times drain time, exclusions that end outside the change, blue-green where the window is tight.",
        "mitigate_after": "Extend the window or finish the upgrade by hand so the pool stops running two versions.",
    },
    6: {
        "title": "A served API version is removed",
        "before": "Clients still calling an API version the target minor removes (the deprecation insight, audit entries labelled k8s.io/removed-release, declared apiVersions).",
        "read_today": "the readiness report's `removed-api-callers` rule reads each cluster's audit log for `k8s.io/removed-release` stamps (a paged `gcloud logging read`, seven days, writes only) and blocks on an operator caller of an API the target removes while it still writes; the deprecation scan in `fleet-upgrade-verification` reads the `apiVersion`s a linked GitOps repository declares in raw YAML and JSON, skipping Helm templates",
        "mitigate_before": "Migrate the callers: bump client libraries and kubectl, rewrite manifests and Helm release state to the new version.",
        "mitigate_after": "The stored objects still exist; re-apply them through the new version and the clients recover.",
    },
    7: {
        "title": "A fail-closed webhook whose backend is not up",
        "before": "A webhook with failurePolicy Fail, a long timeout, a Service with no endpoints and no namespaceSelector exempting kube-system.",
        "read_today": "the readiness mode of `fleet-upgrade-verification` grades a fail-closed webhook whose backend the API server cannot reach now, `blocked` when its rules reach the upgrade's path or a bootstrap RBAC write a new master reconciles (ClusterRoles and ClusterRoleBindings always; Roles and RoleBindings in `kube-system` or `kube-public` when its namespace selector admits that namespace), and an outage otherwise; the backend that would fail once drained, and the object selector, are unread ([the readiness checks](upgrade-readiness-checks.md))",
        "mitigate_before": "Exempt kube-system, a timeout of a few seconds, failurePolicy Ignore for webhooks that are not security controls, two backend replicas behind a budget.",
        "mitigate_after": "Set failurePolicy Ignore or remove the configuration to unwedge the cluster, then restore it once the backend is up.",
    },
    8: {
        "title": "A default changes in the new minor",
        "before": "The target minor's release notes read against the cluster: admission enforcement, seccomp defaults, feature gates that flip on.",
        "read_today": "the readiness report's `changed-defaults` rule reads each namespace's Pod Security Admission labels and flags a namespace at `latest` whose level a crossed minor tightens, and workloads with `gitRepo` volumes, from a per-minor table with sources; the rest of the compatibility check stays with the operator",
        "mitigate_before": "Read the target minor's notes, run Pod Security Admission in warn and audit before enforce, rehearse on staging at the target version.",
        "mitigate_after": "The audit log names the rejecting rule; relabel the namespace or adjust the pod.",
    },
    9: {
        "title": "A feature is deprecated but still served",
        "before": "Deprecation warnings in API responses and audit logs (k8s.io/deprecated) for a feature with a removal date.",
        "read_today": "the readiness report's `deprecated-api-callers` rule reads the audit log's `k8s.io/deprecated` stamps (a paged `gcloud logging read` per cluster, seven days, writes only), on request and under the daily readiness watch, and names each operator caller with its principal, user agent and API",
        "mitigate_before": "Plan the migration while the feature still works.",
        "mitigate_after": "None needed yet.",
    },
    10: {
        "title": "Add-on and client skew",
        "before": "Installed add-on versions against their support matrices for the target minor; a node pool further behind the control plane than the skew policy allows.",
        "read_today": "the readiness mode grades node-pool skew, and the security-patch orchestrator's `pool-skew` check flags a pool too far behind its control plane every Monday; the readiness mode's `client-addon-skew` rule grades kubectl clients (audit-log user agents and kubectl images) against the one-minor window and third-party add-on images against a sourced support table; GKE-managed add-ons expose no version and are listed, not graded",
        "mitigate_before": "Upgrade add-ons to a version whose matrix includes the target before the cluster moves; keep node pools inside the skew window.",
        "mitigate_after": "Upgrade the add-on.",
    },
    11: {
        "title": "The control plane is unreachable for minutes on a zonal cluster",
        "before": "The cluster is zonal; whether its clients retry is not readable from the cluster.",
        "read_today": "the readiness mode of `fleet-upgrade-verification` carries a zonal control plane as a risk on every report, never a blocker, with the retry advice; the cluster inventory audit, run once per cluster by the onboarding sweep, records each control plane's location, and the upgrade skill recommends regional control planes when asked",
        "mitigate_before": "A regional cluster for anything automation depends on, and retries with backoff in the clients.",
        "mitigate_after": "Wait for the control plane; GitOps resyncs on its own.",
    },
    12: {
        "title": "A node label is removed",
        "before": "nodeSelector and affinity terms naming a label the target minor's kubelet or node image stops setting.",
        "read_today": "the readiness report's removed-node-label rule (`readiness_rules/removed_node_label.py`), on every readiness run, against the Node list: a drop a pool crosses in this upgrade blocks, an uncarried or deprecated label is a risk",
        "mitigate_before": "Replace deprecated labels in selectors with their GA names (kubernetes.io/arch, topology.kubernetes.io/zone).",
        "mitigate_after": "Patch the selector; the pods schedule.",
    },
    13: {
        "title": "The container runtime changes",
        "before": "Node agents on the CRI v1alpha2 API, images in the Docker v1 schema, DaemonSets shipping containerd 1.x configuration.",
        "read_today": "the readiness mode of `fleet-upgrade-verification` pairs the containerd major each pool's nodes report with the major the target's node image ships (`upgrade_shape_tables.py`, by minor and image family) and the pods mounting the runtime socket, a risk when the major changes under such a pod; the security-patch orchestrator flags a pool whose `config.imageType` the location no longer offers or that names a pre-containerd variant, and the compliance audit's `hostpath-mount` check flags a pod mounting the containerd socket as a security finding; image schemas and containerd configuration are unread as an upgrade risk",
        "mitigate_before": "Move agents to the CRI v1 API, rebuild v1-schema images, drop containerd 1.x overrides.",
        "mitigate_after": "The same changes under pressure; a completed pool can be downgraded in place while GKE still offers the previous version.",
    },
    14: {
        "title": "cgroup v2 under a runtime that cannot read it",
        "before": "A pool whose effectiveCgroupMode is v2 (or will be migrated at 1.33) running images with a JDK older than 8u372 or 11.0.16 or another runtime that reads cgroup v1 paths.",
        "read_today": "the readiness report's cgroup-v2-runtime rule (`readiness_rules/cgroup_v2_runtime.py`), on every readiness run, from the image tag and the pool's `effectiveCgroupMode`; the node's cgroup filesystem and the heap flags stay unread",
        "mitigate_before": "A runtime that reads cgroup v2 or explicit heap flags; until 1.35 a pool can be pinned to cgroup v1 to buy time.",
        "mitigate_after": "The same, plus a temporary limit increase.",
    },
    15: {
        "title": "The OOM killer starts killing the whole container",
        "before": "A kubelet at 1.28 or later on a cgroup v2 node and containers running more than one process.",
        "read_today": "the readiness report's group-oom-kill rule (`readiness_rules/group_oom_kill.py`), on every readiness run, as a heuristic on the command and the image; `memory.oom.group` and the process list stay unread",
        "mitigate_before": "Raise the limit for multi-process containers, split workers into their own containers, or set singleProcessOOMKill in the pool's node system config.",
        "mitigate_after": "The same; the container's own logs before the upgrade show which worker used to die.",
    },
    16: {
        "title": "The network dataplane changes",
        "before": "Dataplane and DNS provider, policy count, the known issues for the target version.",
        "read_today": "the readiness mode of `fleet-upgrade-verification` flags a legacy-dataplane cluster whose network policy add-on is off and whose namespaces carry NetworkPolicies, naming the namespaces whose policies take effect once enforcement does; the fleet-consistency drift audit reads each cluster's `datapathProvider` and its network-policy settings across the cohort, so a member whose dataplane differs from its peers is reported, and the compliance audit's `netpol-missing` check flags a namespace with no protecting policy without asking whether the cluster enforces any; how a policy behaves, and the DNS provider, are unread",
        "mitigate_before": "Rehearse the target version on a staging cluster with the same dataplane; keep NetworkPolicy explicit.",
        "mitigate_after": "A completed pool can be downgraded in place while GKE still offers the previous version; the control plane cannot go back.",
    },
    17: {
        "title": "A node networking agent fails on the new image",
        "before": "The target node image's known issues; the CNI's dependence on node labels or kernel modules the image changes.",
        "read_today": "the readiness report's node-image-coupled-agent rule (`readiness_rules/node_image_coupled_agent.py`), on every readiness run, as a risk; the node image's release notes stay unread, and the compliance audit flags an agent's host networking and host-path mount as security findings",
        "mitigate_before": "Upgrade a canary pool first and watch Service routing from inside the cluster; surge with maxUnavailable 0.",
        "mitigate_after": "Downgrade the pool to the previous version while it is offered, and fix what the CNI selected on.",
    },
    18: {
        "title": "GPU driver mismatch",
        "before": "The driver the target node image ships against the CUDA version the images need, and whether the images carry forward-compatibility libraries.",
        "read_today": "the readiness report's gpu-driver rule (`readiness_rules/gpu_driver_mismatch.py`), on every readiness run, from the image tag and GKE's driver table; forward-compatibility libraries inside the image stay unread, and the upgrade plan still hands that check to the operator",
        "mitigate_before": "Match the driver to the images before the upgrade (GKE's driver table per version against NVIDIA's minimum-driver matrix); upgrade a canary GPU pool first.",
        "mitigate_after": "Recreate the pool with the driver version the images need.",
    },
    19: {
        "title": "In-tree volumes lose their CSI path",
        "before": "PersistentVolumes with an in-tree gcePersistentDisk spec while the PD CSI driver add-on is disabled; StorageClasses naming a retired provisioner.",
        "read_today": "the readiness mode of `fleet-upgrade-verification` reads every in-tree `gcePersistentDisk` PersistentVolume against the PD CSI driver add-on, `blocked` for a volume a workload mounts while the add-on is off and a risk otherwise; the fleet waste audit reads disks for cost (orphaned volumes, unattached disks), not for how they are attached; StorageClass provisioners are unread",
        "mitigate_before": "Enable the PD CSI driver add-on and move StorageClasses to pd.csi.storage.gke.io.",
        "mitigate_after": "Enable the add-on; the volumes attach.",
    },
    20: {
        "title": "Images on a retired registry",
        "before": "Image references on a registry hostname that has stopped publishing (k8s.gcr.io) or that an egress allowlist no longer admits.",
        "read_today": "the readiness report's retired-registry rule (`readiness_rules/retired_registry.py`), on every readiness run, as a risk for the image references in use, since the hosts still answer a pull; the GitOps repository and the egress allowlists stay unread, and the compliance audit reads image references for floating tags only",
        "mitigate_before": "Mirror every image into a registry you own and keep egress allowlists in step.",
        "mitigate_after": "Retag or redirect the reference; the new nodes pull.",
    },
}
# When the cgroup mode is unknown an OOMKilled single-container pod is "14 or
# 15"; the report says so, and the guard carries the lower number.
OOM_UNDECIDED_ENTRIES = (ENTRY_CGROUP_V2, ENTRY_OOM_GROUP)

REPORT_TITLE = "# Upgrade retrospective {date}"
# The report's three sections. An incident under Errors or Warnings is one
# owner object (or budget) on one cluster, with the four parts inline.
SECTION_ERRORS = "## Errors"
SECTION_WARNINGS = "## Warnings"
SECTION_INFO = "## Info"
PART_WHAT_HAPPENED = "**What happened.**"
PART_WHAT_FAILED = "**What failed.**"
PART_MITIGATE = "**Detect and mitigate next time.**"
PART_MITIGATION_SET_UP = "**Mitigation set up.**"
PART_NEXT_UPGRADE = "**Next upgrade.**"
PART_RISKS = "**Risks present.**"
PART_BASELINE = "**Baseline recorded.**"
NO_SHAPE_TEXT = "no catalogue shape found; checked: "
# What an incident says when it has no catalogue entry to cite. `_none_` is
# reserved for an empty Errors or Warnings section.
UNCLASSIFIED_MITIGATION_TEXT = "no catalogue entry matched this symptom; it is reported for a reader to classify, and the entries' rows above do not apply."
NO_GUARD_TEXT = "no guard written."
OPERATION_GUARD_FORMAT = "guard `{id}` first seen {first_seen}, {reviews} review(s); re-checked through the operations listing each run, cleared by a later {type} on {target} that reaches DONE without error."
OPERATION_NO_GUARD_TEXT = "no guard written: the cluster is outside the fleet."
OPERATION_GUARD_RETRIED_FORMAT = "a later {type} on {target} reached DONE without error ({name}, ended {end})"
OPERATION_GUARD_UNLISTED_FORMAT = "the operations listing no longer returns {name}; GKE's record retention is not a retry, so the guard stands until a later clean operation or --reset-ledger"
OPERATION_GUARD_CLEAN_FORMAT = "the operations listing returns {name} without an error"
OPERATION_GUARD_POOL_GONE_FORMAT = "the cluster no longer has a node pool {target}"
INFO_OPERATION_GUARDS = "Operation guards, re-checked through the operations listing:"
OPERATION_GUARDS_SUMMARY_FORMAT = "{refreshed} still failing per the listing; {held} held, the listing failed; {unlisted} held, the operation no longer listed"
OPERATION_GUARD_CLEARED_FORMAT = "guard `{id}` cleared: {reason}"
OPERATION_GUARD_HELD_FORMAT = "guard `{id}` held: {reason}"
# A failed operation's mitigation: GKE's own error text names the cause; the
# capacity entry's repair is quoted only when the error is capacity-shaped.
OPERATION_MITIGATE_TEXT = "GKE's error text names the cause; the catalogue's entry 2 (capacity) and 5 (window) are the usual ones for an operation that did not complete."
CAPACITY_ERROR_MARKERS = ("resources available", "insufficient", "quota", "stockout", "resource_pool_exhausted")
INFO_UNCHANGED = "Unchanged clusters:"
INFO_REMOVED = "Removed clusters:"
INFO_RECHECKED = "Re-checked for live guards and refreshed symptom sets:"
PARTIAL_READ_TEXT = "Partially read ({failed}): a guard whose finding could not be re-observed was kept, and the cluster is re-read next run."
INFO_FAILED_READS = "Reads that failed:"
NONE_LINE = "_none_"
SEVERITY_ERROR, SEVERITY_WARNING = "error", "warning"
# Incident kinds: a symptom on an object, an operation GKE reported failed,
# a guard from an earlier run on a cluster this run did not review.
INCIDENT_SYMPTOM, INCIDENT_OPERATION, INCIDENT_STALE_GUARD = "symptom", "operation", "stale-guard"
# GKE operation statuses: only a DONE operation with an end time counts for
# a review; one still PENDING, RUNNING or ABORTING holds its cluster back.
OPERATION_TERMINAL_STATUS = "DONE"
OPERATION_IN_FLIGHT_STATUSES = ("PENDING", "RUNNING", "ABORTING")
INFO_UPGRADING = "Upgrading now:"
INFO_SCOPED = "Scoped run: {reason}. Nothing outside the scope was pruned, no stale guard outside it is reported, and the fleet link was not moved."
# The scope note on the human report too: a Sunday report whose fleet the tool
# could not hand, or the listing could not name, in full says so where a reader
# looks, not in the manifest alone.
INFO_SCOPE_NOTE = "Scope: {note}"
UPGRADING_LINE = "{cluster}: upgrading now ({operation} {target} since {start}); reviewed on the next run"
NO_OPERATION_LINE = "no upgrade operation in the window"
# The after-upgrade route (--after-upgrade): the clusters with an
# UPGRADE_MASTER or UPGRADE_NODES that reached DONE at least this long before
# the run and that the ledger does not list as reviewed, as a scoped run; a
# fresher operation waits for the next wake so its replacement pods settle,
# and its cluster's settled operations wait with it, so one review covers
# the whole upgrade.
AFTER_UPGRADE_SETTLE = timedelta(minutes=15)
AFTER_UPGRADE_WITH_FULL_TEXT = "--after-upgrade is a scoped run and cannot be combined with --full"
AFTER_UPGRADE_WITH_CLUSTER_TEXT = "--after-upgrade finds its clusters from the operations listing and the ledger and cannot be combined with --cluster"
# The route selects by the reviewed-operations record, which a full run prunes
# at its own window; a wider window would outrun the record and review again.
AFTER_UPGRADE_WITH_SINCE_TEXT = "--after-upgrade selects by the ledger's reviewed operations inside each cluster's own window and cannot be combined with --since (a hand run; --cluster re-reviews a cluster)"
AFTER_UPGRADE_SCOPE_TEXT = "--after-upgrade reviews the clusters with an upgrade operation not yet reviewed"
AFTER_UPGRADE_REASON_FORMAT = "{count} upgrade operation(s) not yet reviewed, ended by {floor}"
AFTER_UPGRADE_QUIET_LOG = "after-upgrade: no settled operation awaits review; nothing written"
# A wake whose operations listing failed cannot know whether anything settled:
# blind, not quiet, so the run goes on with nothing to review and writes the
# failure where the SOP reads it.
AFTER_UPGRADE_BLIND_LOG = "after-upgrade: the operations listing failed for {projects}; nothing settled can be found, and the failure is written for the recovery"
AFTER_UPGRADE_HELD_LOG = "after-upgrade: nothing settled is reviewable yet; nothing written"
# The route diffs against a baseline a full run established; without one it
# says so and writes nothing, so an install that predates the job gets its
# first report from a full run, never from a sweep over a fortnight.
NO_BASELINE_TEXT = "no baseline yet; the first full run establishes it"
SETTLING_LINE = "{cluster}: {operation} {target} ended {end}, under {minutes} min ago; reviewed on the next after-upgrade wake or the next full run"
SETTLED_WAITING_FORMAT = ", with {count} settled operation(s) not yet reviewed"
# The ledger lists the operations each cluster's reviews covered, with the
# route that reviewed them; a run that meets one again reports it as such.
LEDGER_OPERATIONS_KEY = "operations_reviewed"
ROUTE_FULL, ROUTE_SCOPED, ROUTE_AFTER_UPGRADE = "full", "scoped", "after-upgrade"
ALREADY_REVIEWED_FORMAT = "already reviewed by the {route} route at {at}"
NODE_AFTER_POOL_UPGRADE_ENTRY = ENTRY_NODE_AGENT


def log(msg: str) -> None:
    print(f"[upgrade_retrospective] {msg}", file=sys.stderr, flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_ts(text: str | None) -> datetime | None:
    """RFC 3339 as GKE and Kubernetes write it, with or without fractional seconds."""
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp (`--since 2026-10-01T00:00:00`) is UTC, which is
    # what the pod runs in; reading it in a laptop's zone would move the window.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def hermes_home() -> Path:
    return Path(os.environ.get(HERMES_HOME_ENV) or DEFAULT_HERMES_HOME)


def data_dir() -> Path:
    return Path(os.environ.get(STORE_HOME_ENV) or DEFAULT_STORE_DIR)


def cluster_key(project: str, location: str, name: str) -> str:
    return CLUSTER_KEY_SEPARATOR.join((project, location, name))


def is_system_namespace(namespace: str) -> bool:
    return namespace in SYSTEM_NAMESPACES or namespace.startswith(SYSTEM_NAMESPACE_PREFIXES)


# --------------------------------------------------------------------------- #
# The one subprocess seam.
# --------------------------------------------------------------------------- #


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


def default_run(argv: list[str], *, timeout: int = GCLOUD_TIMEOUT_S, env: dict | None = None) -> Run:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        return Run(argv, TIMEOUT_RC, _text(exc.stdout), _text(exc.stderr), time.monotonic() - t0)
    except Exception as exc:  # a missing binary, a bad env
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def _excerpt(result: Run) -> str:
    return result.stderr.strip()[:ERROR_EXCERPT_CHARS] or "no stderr"


def run_json(argv: list[str], *, run: RunFn, timeout: int = GCLOUD_TIMEOUT_S, env: dict | None = None) -> tuple[object | None, str | None]:
    """Parsed JSON stdout, or the error that stands for it."""
    result = run(argv, timeout=timeout, env=env)
    if result.rc != 0:
        return None, f"{argv[0]} {argv[1]} {argv[2]} rc={result.rc}: {_excerpt(result)}"
    try:
        return json.loads(result.stdout or "null"), None
    except json.JSONDecodeError as exc:
        return None, f"{argv[0]} {argv[1]} {argv[2]} returned non-JSON: {exc}"


# --------------------------------------------------------------------------- #
# Projects, clusters, operations.
# --------------------------------------------------------------------------- #


def discover_fleet(base_project: str | None, *, run: RunFn, forced: set[str] | frozenset[str] = frozenset()) -> tuple[list[str], list[str], str | None]:
    """The projects to enumerate, the reads that failed on the way, and, on an
    undeclared install, the note that the listing was incomplete (it failed,
    or did not name the active project, so it is filtered), under which the
    swept set is not the fleet. Resolved as the sibling collectors resolve
    it: `--project` names one project and is a hand run's override
    (`validate_args` refused one outside a declared scope); else the
    declared scope the SOP handed from the fleet_scope tool, swept exactly
    as handed with nothing listed; else the active gcloud project plus every
    project `gcloud projects list` returns."""
    if base_project:
        return [base_project], [], None
    if forced:
        # A hand run that names clusters reads their projects alone, listed
        # or declared or not: the override's rule, cluster by cluster
        # (`validate_args` admitted each inside a declared scope).
        return sorted({key.split(CLUSTER_KEY_SEPARATOR)[0] for key in forced}), [], None
    if declared_scope.declared:
        log(SCOPE_LOG_FORMAT.format(count=len(declared_scope.projects or [])))
        return list(declared_scope.projects or []), [], None
    errors: list[str] = []
    projects: set[str] = set()
    result = run(["gcloud", "config", "get-value", "project"])
    base = result.stdout.strip() if result.rc == 0 else ""
    if base:
        projects.add(base)
    note = None
    result = run(["gcloud", "projects", "list", "--format", "value(projectId)"])
    if result.rc != 0:
        note = LISTING_FAILED_NOTE_FORMAT.format(rc=result.rc, error=_excerpt(result))
    else:
        listed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        projects |= listed
        if not base:
            # No active project to check the listing against: whether it is
            # complete is unknown, and an empty one is the failed case
            # (`collect.discover_fleet`); either holds a full run.
            note = LISTING_UNCHECKED_NOTE_FORMAT.format(count=len(listed)) if listed else LISTING_EMPTY_NOTE_TEXT
        elif base not in listed:
            # rc 0 and the active project absent from its own output: the
            # listing is filtered rather than complete (`collect.discover_fleet`).
            note = LISTING_FILTERED_NOTE_FORMAT.format(project=base, count=len(listed))
    if not projects:
        errors.append("project discovery named no project: no `--project` and no active gcloud project")
    return sorted(projects), errors, note


def exit_projects_text(discovery: tuple[list[str], list[str], str | None], forced: set[str]) -> str:
    """The tail of an exit line after `start` without a manifest: the projects
    this run resolved for its route, `--cluster`'s included, and the
    listing's note when it was incomplete, so the SOP's recovery `finish`
    lists their clusters rather than resolving the scope again."""
    projects, _, note = discovery
    named = sorted(set(projects) | {c.split(CLUSTER_KEY_SEPARATOR)[0] for c in forced})
    return EXIT_PROJECTS_FORMAT.format(projects=", ".join(named) or EXIT_PROJECTS_NONE, note=EXIT_NOTE_FORMAT.format(note=note) if note else "")


def harness_manifest_path() -> str:
    """Where the harness reads this stream's manifest, as `audit_report.manifest_path_for` names it."""
    return f"{os.environ.get(SCRATCH_DIR_ENV) or DEFAULT_SCRATCH_DIR}/{HARNESS_MANIFEST_FILENAME}"


def enumerate_clusters(project: str, *, run: RunFn) -> tuple[list[dict], str | None]:
    """Every cluster in `project` as the raw `clusters list` item plus
    `project`; a project whose GKE API is off is empty rather than failed. A
    listing a zone did not answer returns the clusters it reached *and* an
    error (`LISTING_INCOMPLETE_FORMAT`), so those are reviewed while the
    project is held, not pruned, as the sibling collectors read it."""
    result = run(["gcloud", "container", "clusters", "list", "--project", project, "--format", "json"])
    if result.rc != 0:
        if any(marker in result.stderr for marker in API_DISABLED_MARKERS):
            from collect import refusal_owner  # noqa: PLC0415 -- the one rule for an API refusal's owner, in collect.py

            ours, why_not = refusal_owner(project, result.stderr, run=run)
            if ours:
                return [], None
            return [], API_REFUSED_OTHER_FORMAT.format(listing="clusters list", why=why_not, error=f"rc={result.rc}: {_excerpt(result)}")
        return [], f"clusters list rc={result.rc}: {_excerpt(result)}"
    try:
        clusters = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], f"clusters list returned non-JSON: {exc}"
    if not isinstance(clusters, list):
        return [], "clusters list returned JSON that is not a list"
    clusters = declared_scope.drop_excluded(project, clusters)
    for cluster in clusters:
        cluster["project"] = project
        cluster["location"] = cluster.get("location") or cluster.get("zone") or ""
    from collect import ZONE_TIMEOUT_MARKER  # noqa: PLC0415 -- the siblings' marker for a zone that did not answer

    incomplete = [line.strip() for line in result.stderr.splitlines() if ZONE_TIMEOUT_MARKER in line]
    if incomplete:
        return clusters, LISTING_INCOMPLETE_FORMAT.format(detail="; ".join(incomplete)[:ERROR_EXCERPT_CHARS])
    return clusters, None


def operations_argv(project: str, since: datetime) -> list[str]:
    flt = OPERATIONS_FILTER.format(types=" OR ".join(UPGRADE_OPERATION_TYPES), since=fmt_ts(since), done=OPERATION_TERMINAL_STATUS)
    return ["gcloud", "container", "operations", "list", "--project", project, "--filter", flt, "--format", "json"]


def list_operations(project: str, since: datetime, *, run: RunFn) -> tuple[list[dict], str | None]:
    """The upgrade operations in `project` that ended at or after `since`, and
    every one still in flight whatever its start: a long operation starts
    before the window it ends in. The filter is also applied here, so a
    gcloud that ignores it changes nothing."""
    parsed, error = run_json(operations_argv(project, since), run=run)
    if error:
        # The same API that made `clusters list` an empty project refuses this
        # listing with the same words: empty, not a failed read, when the
        # refusal is this project's own.
        if any(marker in error for marker in API_DISABLED_MARKERS):
            from collect import refusal_owner  # noqa: PLC0415 -- the one rule for an API refusal's owner, in collect.py

            ours, why_not = refusal_owner(project, error, run=run)
            if ours:
                return [], None
            return [], API_REFUSED_OTHER_FORMAT.format(listing="operations list", why=why_not, error=error)
        return [], error
    if not isinstance(parsed, list):
        return [], "operations list returned JSON that is not a list"
    ops = []
    for op in parsed:
        if op.get("operationType") not in UPGRADE_OPERATION_TYPES:
            continue
        end = _op_end(op)
        if end is not None and end < since:
            continue
        ops.append(op)
    return ops, None


def parse_target_link(link: str) -> tuple[str, str, str | None] | None:
    m = TARGET_LINK_RE.search(link or "")
    if not m:
        return None
    return m.group("location"), m.group("cluster"), m.group("pool")


def operation_summary(op: dict) -> dict:
    start, end = parse_ts(op.get("startTime")), parse_ts(op.get("endTime"))
    target = parse_target_link(op.get("targetLink", ""))
    error = op.get("error") or {}
    error_text = error.get("message") if isinstance(error, dict) else str(error)
    return {
        "name": op.get("name"),
        "type": op.get("operationType"),
        "target": target[2] if target and target[2] else CONTROL_PLANE_TARGET,
        "start": fmt_ts(start) if start else None,
        "end": fmt_ts(end) if end else None,
        "duration_s": int((end - start).total_seconds()) if start and end else None,
        "status": op.get("status"),
        "error": error_text or op.get("statusMessage") or None,
    }


def versions_of(cluster: dict) -> dict:
    return {
        "control_plane": cluster.get("currentMasterVersion") or "",
        "node_pools": {p.get("name", ""): p.get("version", "") for p in cluster.get("nodePools") or []},
        "channel": (cluster.get("releaseChannel") or {}).get("channel") or "",
    }


def pool_cgroup_modes(cluster: dict) -> dict[str, str]:
    return {p.get("name", ""): ((p.get("config") or {}).get("effectiveCgroupMode") or "") for p in cluster.get("nodePools") or []}


def pool_records_of(cluster: dict) -> dict[str, dict]:
    """Per pool, what the cluster record says of it that a node's allocatable
    cannot once the resource is gone from it or the node is: the extended
    resources its config carries (`accelerators` are GPUs; a config taint
    keyed by a resource name, GKE's `nvidia.com/gpu=present`, names its
    resource) and its config taints in the scheduler's shape."""
    out: dict[str, dict] = {}
    for pool in cluster.get("nodePools") or []:
        config = pool.get("config") or {}
        taints = [{"key": t.get("key") or "", "value": t.get("value") or "", "effect": RECORD_TAINT_EFFECTS.get(t.get("effect") or "", t.get("effect") or "")} for t in config.get("taints") or []]
        resources = {t["key"] for t in taints if RESOURCE_DOMAIN_SEPARATOR in t["key"] and not t["key"].startswith(BUILTIN_RESOURCE_DOMAIN)}
        if config.get("accelerators"):
            resources.add(GPU_RESOURCE)
        out[pool.get("name", "")] = {"resources": resources, "taints": taints}
    return out


# --------------------------------------------------------------------------- #
# Ledger and selection.
# --------------------------------------------------------------------------- #


class StateUnreadable(Exception):
    """A ledger or guards file exists but cannot be used. The run stops: a
    set-aside ledger or guards file is a crash record, not a re-baseline, and
    a run proceeds as a first run only when neither file nor a record of it
    exists."""


def _set_aside(path: Path, why: str, now: datetime, move: bool = True) -> None:
    """Move a state file this run cannot use out of the way, so nothing
    overwrites it, and stop. A dry run moves nothing: it only reports."""
    if not move:
        raise StateUnreadable(STATE_UNREADABLE_DRY_RUN_TEXT.format(path=path, why=why))
    aside = path.with_name(path.name + UNREADABLE_SUFFIX.format(ts=now.strftime("%Y%m%dT%H%M%SZ")))
    try:
        os.replace(path, aside)
    except OSError as exc:
        raise StateUnreadable(STATE_NOT_MOVED_TEXT.format(path=path, why=why, error=exc)) from exc
    raise StateUnreadable(STATE_UNREADABLE_TEXT.format(path=path, why=why, aside=aside))


def load_json(path: Path, default: dict, *, version: int | None = None, now: datetime | None = None, move_aside: bool = True) -> dict:
    now = now or now_utc()
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return default
    except PermissionError:
        # A file this uid cannot read is an ownership problem, not a crash record: it stays where
        # it is, and the caller's handler prints the store's denied line with the owner named.
        raise
    except (OSError, ValueError) as exc:  # ValueError covers JSONDecodeError and a non-UTF-8 byte
        _set_aside(path, f"is unreadable ({exc})", now, move_aside)
    if not isinstance(loaded, dict):
        _set_aside(path, "is not a JSON object", now, move_aside)
    if version is not None and loaded.get("version") != version:
        _set_aside(path, f"has version {loaded.get('version')!r}, this collector writes {version}", now, move_aside)
    return loaded


def empty_ledger() -> dict:
    return {"version": LEDGER_VERSION, "updated_at": None, LEDGER_PROJECTS_KEY: [], "clusters": {}}


def empty_guards() -> dict:
    return {"version": GUARDS_VERSION, "updated_at": None, "guards": []}


def _adopt_owner(descriptor: int, owner: os.stat_result) -> None:
    """Give the open file or directory to `owner`'s uid and gid when this run
    is root, as `audit_report._adopt_owner` does for the report store: root
    is the one writer that can hand what it creates to the store's owner,
    and any other uid creates what it owns. Through the descriptor, never
    the path: a root run must not follow a symlink the agent's own account
    could plant in the store under a name it can predict."""
    if os.geteuid() == ROOT_UID:
        os.fchown(descriptor, owner.st_uid, owner.st_gid)


def make_store_dir(path: Path) -> None:
    """A store directory and its missing parents at `STORE_DIR_MODE` (set by
    chmod rather than through the umask, which is process-wide and would race
    the worker threads), owned like the nearest directory that already
    existed when this run is root (`_adopt_owner`): the fleet-audit report
    store's rule (`audit_report._make_store_dirs`), which touches only the
    directories it makes. One that already exists is left as found, mode and
    owner alike: a root run that tightened a root-owned directory an operator
    had opened for the tick would lock the tick out of a store it could write
    before. A store this uid cannot write is met by the write, and is
    `store_denied_line`'s one line; a symlink at any component from the
    store root down is `STORE_IS_SYMLINK_TEXT`'s, and nothing is written."""
    _refuse_symlinked_store_path(path)
    missing: list[Path] = []
    existing = path
    while not existing.exists():
        missing.append(existing)
        existing = existing.parent
    try:
        owner: os.stat_result | None = existing.stat()
    except OSError:
        owner = None
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            continue  # a rival run made it, and chose its ownership then
        # Set through a descriptor opened without following: a name swapped
        # for a symlink between the mkdir and here is refused, not chased.
        try:
            opened = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            with contextlib.suppress(OSError):
                os.fchmod(opened, STORE_DIR_MODE)
                if owner is not None:
                    _adopt_owner(opened, owner)
        finally:
            os.close(opened)


def store_denied_line(exc: PermissionError) -> str:
    """One line for a store path this uid cannot write, with the owner named
    when it is another uid's, as the report store's `_ownership_note` names
    it: the residual a root run from before `_adopt_owner`, or a directory
    made by hand, leaves, which only an operator can repair."""
    note = ""
    if exc.filename:
        with contextlib.suppress(OSError):
            where = Path(exc.filename)
            where = where if where.exists() else where.parent
            owner = where.stat().st_uid
            if owner != os.geteuid():
                note = STORE_OWNER_NOTE_FORMAT.format(where=where, owner=owner, uid=os.geteuid())
    return STORE_DENIED_FORMAT.format(path=exc.filename or STORE_DENIED_PATH_UNKNOWN, error=exc.strerror or exc, note=note)


def _under_store(path: Path) -> bool:
    """Whether `path` is the store's, compared lexically: resolving it would
    follow a link planted under the store out of it, and the store's own
    rule (`make_store_dir`) is what refuses such a link."""
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(data_dir())))
    except ValueError:
        return False
    return True


def _refuse_symlinked_store_path(path: Path) -> None:
    """Every component from the store root down to `path` must be a real
    directory: a symlink at any of them is refused with one line, never
    followed, since the writes that follow (`mkstemp`, `os.replace`,
    `unlink`) resolve a directory link and the store's directories are the
    agent account's to replace."""
    root = data_dir()
    try:
        parts = path.relative_to(root).parts
        components = [root] + [root.joinpath(*parts[: i + 1]) for i in range(len(parts))]
    except ValueError:
        components = [path]
    for component in components:
        if os.path.islink(component):
            raise StateUnreadable(STORE_IS_SYMLINK_TEXT.format(path=component))


def output_parent(path: Path) -> None:
    """The directory an output file is written into: the store's own are
    made as the store's (`make_store_dir`); one the caller named (`--report`,
    `--output`, `--manifest-file`, `--ledger`, `--guards` outside the store)
    is made under the caller's umask, never world-writable."""
    if _under_store(path.parent):
        make_store_dir(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)


def write_json_atomically(path: Path, doc: object) -> None:
    """A temporary file beside `path`, fsynced, renamed over it: a reader sees
    the whole document or the previous one, never a splice. The file is
    `STORE_FILE_MODE` wherever `path` is (the store's rule for every file it
    writes, stated at the constant); only the directory follows the caller."""
    output_parent(path)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=TEMP_SUFFIX)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            # Mode and owner through the open descriptor: the random name
            # was made with O_EXCL, and nothing at a path is followed.
            os.fchmod(out.fileno(), STORE_FILE_MODE)
            _adopt_owner(out.fileno(), path.parent.stat())
            json.dump(doc, out, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _op_end(op: dict) -> datetime | None:
    return parse_ts(op.get("endTime"))


def _op_in_flight(op: dict) -> bool:
    return (op.get("status") or "") in OPERATION_IN_FLIGHT_STATUSES or ((op.get("status") or "") != OPERATION_TERMINAL_STATUS and _op_end(op) is None)


class Selection(NamedTuple):
    cluster: dict
    key: str
    status: str  # STATUS_NEW | STATUS_UPGRADED | STATUS_FORCED
    reasons: list[str]
    window_start: datetime
    operations: list[dict]


def select_clusters(clusters: list[dict], ledger: dict, operations: list[dict], *, since: datetime, forced: set[str], widen: bool = False, after_upgrade: bool = False, now: datetime | None = None) -> tuple[list[Selection], list[dict], list[dict]]:
    """Which clusters this run reviews, and why; the rest as unchanged rows;
    and the clusters an operation still in flight, or one that reached DONE
    inside the settle floor, holds back until the next run. Only an operation
    that ended, inside the window, counts, and the floor applies on every
    route. The after-upgrade route selects by operations alone: one that
    reached DONE at least `AFTER_UPGRADE_SETTLE` before the run and that the
    ledger does not list as reviewed."""
    by_target: dict[tuple[str, str, str], list[dict]] = {}
    for op in operations:
        target = parse_target_link(op.get("targetLink", ""))
        if target and op.get("operationType") in UPGRADE_OPERATION_TYPES:
            by_target.setdefault((op.get("project") or "", target[0], target[1]), []).append(op)
    settle_floor = (now or now_utc()) - AFTER_UPGRADE_SETTLE
    selected, unchanged, upgrading = [], [], []
    for cluster in clusters:
        key = cluster_key(cluster["project"], cluster["location"], cluster["name"])
        cluster_ops = by_target.get((cluster["project"], cluster["location"], cluster["name"]), [])
        in_flight = [op for op in cluster_ops if _op_in_flight(op)]
        if in_flight:
            op = operation_summary(sorted(in_flight, key=lambda o: o.get("startTime") or "")[0])
            upgrading.append({"cluster": key, "operation": op})
            continue
        record = (ledger.get("clusters") or {}).get(key) or {}
        # Enumerated once but never reviewed (its reads failed, or a scoped
        # run recorded only the operations it reviewed): still new.
        entry = record if record.get("last_run") else None
        current = versions_of(cluster)
        reasons: list[str] = []
        window_start = since
        last_run = since
        if entry is not None:
            last_run = parse_ts(entry.get("last_run")) or since
            window_start = min(last_run, since) if (key in forced or widen) else last_run
        ops = sorted(
            (op for op in cluster_ops if (op.get("status") or "") == OPERATION_TERMINAL_STATUS and _op_end(op) and _op_end(op) >= window_start),
            key=lambda op: op.get("startTime") or "",
        )
        unchanged_row = {"cluster": key, "control_plane": current["control_plane"], "last_run": entry.get("last_run") if entry else None}
        reviewed_ids = {o.get("id") for o in record.get(LEDGER_OPERATIONS_KEY) or []}
        settled = [op for op in ops if _op_end(op) <= settle_floor]
        fresh = [op for op in settled if op.get("name") not in reviewed_ids]
        settling = [op for op in ops if _op_end(op) > settle_floor and op.get("name") not in reviewed_ids]
        if settling:
            # The settle floor applies on every route and per cluster: an
            # operation that reached DONE inside it still shows the drain's
            # own signatures (a Pending replacement, a budget at zero), so
            # the cluster waits whole, its settled operations with it, and
            # one review covers the upgrade on the next wake or full run. A
            # review of the settled half would grade the settling pool's
            # displaced pods against a window that does not name their
            # operation, then file them again under another check.
            latest = max(settling, key=lambda op: _op_end(op) or settle_floor)
            upgrading.append({"cluster": key, "operation": operation_summary(latest), "settling": True, "waiting": len(fresh)})
            continue
        if after_upgrade:
            if fresh:
                reasons.append(AFTER_UPGRADE_REASON_FORMAT.format(count=len(fresh), floor=fmt_ts(settle_floor)))
                selected.append(Selection(cluster, key, STATUS_NEW if entry is None else STATUS_UPGRADED, reasons, window_start, settled))
            else:
                unchanged.append(unchanged_row)
            continue
        if entry is None:
            status = STATUS_NEW
            reasons.append("first seen")
        else:
            status = STATUS_UPGRADED
            if entry.get("control_plane") != current["control_plane"]:
                reasons.append(f"control plane {entry.get('control_plane')} -> {current['control_plane']}")
            for pool, version in current["node_pools"].items():
                previous = (entry.get("node_pools") or {}).get(pool)
                # A pool created since the last run is new, not upgraded.
                if previous is not None and previous != version:
                    reasons.append(f"node pool {pool} {previous} -> {version}")
            partial = parse_ts(entry.get("partial_read"))
            if partial and partial >= last_run:
                reasons.append(f"previous review at {entry['partial_read']} read the cluster partially")
            # Only an operation that ended since the last run makes a known
            # cluster "upgraded"; a forced review widens the window without that.
            recent = [op for op in ops if _op_end(op) >= last_run]
            if recent:
                reasons.append(f"{len(recent)} upgrade operation(s) since {fmt_ts(last_run)}")
            if any(BASELINE_SEPARATOR + CATEGORY_VERSION_SKEW + BASELINE_SEPARATOR in key_text for key_text in entry.get("symptoms") or []) and ((cluster.get("maintenancePolicy") or {}).get("window")):
                # A skew the previous run recorded: the review grades it now.
                # A cluster with no maintenance policy is never graded for
                # one (row 5 needs a window), so it is not re-selected for it.
                if not reasons:
                    status = STATUS_REGRADED
                reasons.append(SKEW_RESELECT_REASON)
        if key in forced:
            status = status if reasons else STATUS_FORCED
            reasons.append("forced by --cluster")
        if not reasons:
            unchanged.append(unchanged_row)
            continue
        selected.append(Selection(cluster, key, status, reasons, window_start, ops))
    unchanged.sort(key=lambda row: row["cluster"])
    upgrading.sort(key=lambda row: row["cluster"])
    return selected, unchanged, upgrading


def what_happened(selection: Selection, ledger: dict) -> dict:
    cluster, current = selection.cluster, versions_of(selection.cluster)
    entry = (ledger.get("clusters") or {}).get(selection.key) or {}
    # An entry a scoped run created holds only the operations it reviewed;
    # the before side needs the versions a full run recorded.
    before = {"control_plane": entry.get("control_plane"), "node_pools": entry.get("node_pools") or {}} if entry.get("control_plane") else None
    reviewed = {o.get("id"): o for o in entry.get(LEDGER_OPERATIONS_KEY) or []}
    operations = []
    for op in selection.operations:
        summary = operation_summary(op)
        seen = reviewed.get(summary["name"])
        if seen:
            summary["already_reviewed"] = {"route": seen.get("route") or ROUTE_FULL, "at": seen.get("reviewed_at")}
        operations.append(summary)
    return {
        "status": selection.status,
        "reasons": selection.reasons,
        "window_start": fmt_ts(selection.window_start),
        "channel": current["channel"],
        "versions_before": before,
        "versions_after": {"control_plane": current["control_plane"], "node_pools": current["node_pools"]},
        "cluster_status": cluster.get("status"),
        "operations": operations,
    }


# --------------------------------------------------------------------------- #
# The cluster read.
# --------------------------------------------------------------------------- #


def kubeconfig_dir() -> Path:
    return hermes_home() / KUBECONFIG_SUBDIR


def kubeconfig_path(project: str, cluster: str, location: str) -> Path:
    return kubeconfig_dir() / KUBECONFIG_FILENAME.format(project=project, cluster=cluster, location=location)


def fetch_credentials(cluster: dict, *, run: RunFn) -> tuple[Path, str | None]:
    """The cluster's kubeconfig through `get-credentials`, or the error. The
    shared directory is made usable first by the one rule the fleet-audit
    collectors share (`collect.prepare_kubeconfig`); a directory that cannot
    be made usable is this read's error, with the reason."""
    kc = kubeconfig_path(cluster["project"], cluster["name"], cluster["location"])
    from collect import prepare_kubeconfig  # noqa: PLC0415 -- the one rule for the shared directory

    note = prepare_kubeconfig(kc)
    if note:
        return kc, f"get-credentials: {note}"
    env = {**os.environ, "KUBECONFIG": str(kc)}
    result = run(["gcloud", "container", "clusters", "get-credentials", cluster["name"], "--location", cluster["location"], "--project", cluster["project"]], env=env)
    if result.rc != 0:
        return kc, f"get-credentials rc={result.rc}: {_excerpt(result)}"
    return kc, None


def command_text(argv: list[str], kubeconfig: Path | None = None) -> str:
    """`argv` as a shell line, with the kubeconfig the read ran under spelled out."""
    text = shlex.join(argv)
    return f"{KUBECONFIG_ENV}={shlex.quote(str(kubeconfig))} {text}" if kubeconfig else text


def checks_run(commands_by_read: dict[str, str], fallback: str | None = None, failed: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """The SOP's `checks_run` entries for one cluster: each check whose read
    answered, with that read's command; `fallback` (the operations listing
    that decided an unchanged cluster) backs a check whose read did not run,
    never one whose read ran and failed (`failed`): that check is unevaluated."""
    out = []
    for check, read in CHECK_READS:
        if read in failed:
            continue
        command = commands_by_read.get(read) or fallback
        if command:
            out.append({"check": check, "command": command})
    return out


def read_cluster(kubeconfig: Path, *, run: RunFn) -> tuple[dict[str, list], list[str]]:
    """The four lists, each `[]` when its read failed, plus the failures."""
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    out: dict[str, list] = {}
    errors: list[str] = []
    for name, argv in KUBECTL_READS:
        parsed, error = run_json(argv, run=run, timeout=KUBECTL_TIMEOUT_S, env=env)
        if error:
            errors.append(f"{name}: {error}")
            out[name] = []
            continue
        out[name] = (parsed.get("items") or []) if isinstance(parsed, dict) else []
    return out, errors


# --------------------------------------------------------------------------- #
# Symptoms: pure functions over the parsed lists.
# --------------------------------------------------------------------------- #


def _object_ref(namespace: str, kind: str, name: str) -> str:
    return f"{namespace}/{kind}/{name}" if namespace else f"{kind}/{name}"


def _condition(obj: dict, kind: str) -> dict:
    for cond in (obj.get("status") or {}).get("conditions") or []:
        if cond.get("type") == kind:
            return cond
    return {}


def _api_marker_hits(pod: dict) -> list[str]:
    """Which of entry 6's best-effort markers the pod names, in its names,
    images, command, args and env values. Only the marker names leave this
    function: the text itself (an inline env value can be a credential) is
    never stored or written."""
    text = _pod_text(pod)
    return [marker for marker in DEPRECATED_API_MARKERS if marker in text]


def _api_marker_sources(pod: dict) -> dict[str, list[str]]:
    """The same markers split by where they matched: the pod's or its
    owners' names, or the spec (images, command, args, env values)."""
    names = " ".join([pod["metadata"].get("name", "")] + [o.get("name", "") for o in pod["metadata"].get("ownerReferences") or []])
    spec_text = " ".join(part for part in _pod_text(pod).split(" ") if part not in names.split(" "))
    return {
        "name": [m for m in DEPRECATED_API_MARKERS if m in names],
        "spec": [m for m in DEPRECATED_API_MARKERS if m in spec_text],
    }


def _pod_text(pod: dict) -> str:
    """The text entry 6's best-effort markers are searched in: names, images,
    command, args and env values of every container."""
    parts = [pod["metadata"].get("name", "")]
    for owner in pod["metadata"].get("ownerReferences") or []:
        parts.append(owner.get("name", ""))
    for container in (pod.get("spec") or {}).get("containers") or []:
        parts.extend([container.get("name", ""), container.get("image", "")])
        parts.extend(container.get("command") or [])
        parts.extend(container.get("args") or [])
        parts.extend(str(e.get("value") or "") for e in container.get("env") or [])
    return " ".join(parts)


def _controller_of(obj: dict) -> tuple[str, str] | None:
    for owner in (obj.get("metadata") or {}).get("ownerReferences") or []:
        if owner.get("kind") and owner.get("name") and owner["kind"] not in NON_CONTROLLER_OWNER_KINDS:
            return owner["kind"], owner["name"]
    return None


class Resolver:
    """Maps any pod, ReplicaSet or Job to its top owner, from the pod list
    and the `owners` read. An intermediate the read did not return is itself
    the top: the hop is unknown rather than absent. A pod the list holds
    without a controller is a bare pod and stays one, a static pod's mirror
    (owned by its Node, which controls nothing) among them; the name-prefix
    match serves only a pod that is gone and known by the name an event
    carries."""

    def __init__(self, pods: list[dict], owners: list[dict], workloads: list[dict] | None = None):
        self.parent: dict[tuple[str, str, str], tuple[str, str]] = {}
        # Controllers by namespace, longest name first, for a pod that is
        # gone and known only by the name an event carries.
        self.controllers: dict[str, list[tuple[str, str]]] = {}
        # Every pod the list holds: one of these is never resolved by prefix.
        self.listed: set[tuple[str, str]] = set()
        for pod in pods:
            controller = _controller_of(pod)
            meta = pod.get("metadata") or {}
            self.listed.add((meta.get("namespace", ""), meta.get("name", "")))
            if controller:
                self.parent[(meta.get("namespace", ""), "Pod", meta.get("name", ""))] = controller
        for obj in list(owners) + list(workloads or []):
            meta = obj.get("metadata") or {}
            kind, namespace, name = obj.get("kind") or "", meta.get("namespace", ""), meta.get("name", "")
            controller = _controller_of(obj)
            if controller and kind in OWNER_INTERMEDIATE_KINDS:
                self.parent[(namespace, kind, name)] = controller
            if kind in PREFIX_RESOLVED_KINDS and name:
                self.controllers.setdefault(namespace, []).append((kind, name))
        for names in self.controllers.values():
            names.sort(key=lambda kn: -len(kn[1]))
        # Which pods each top owner has, for "n of m pods".
        self.pods_of: dict[str, list[str]] = {}
        for pod in pods:
            meta = pod.get("metadata") or {}
            kind, name = self.resolve(meta.get("namespace", ""), "Pod", meta.get("name", ""))
            self.pods_of.setdefault(_object_ref(meta.get("namespace", ""), kind, name), []).append(meta.get("name", ""))

    def _controller_by_prefix(self, namespace: str, pod_name: str) -> tuple[str, str] | None:
        for kind, name in self.controllers.get(namespace) or []:
            if not pod_name.startswith(name + "-"):
                continue
            suffix = pod_name[len(name) + 1:]
            if (kind == "StatefulSet" and POD_ORDINAL_SUFFIX_RE.match(suffix)) or (kind != "StatefulSet" and POD_HASH_SUFFIX_RE.match(suffix)):
                return kind, name
        return None

    def resolve(self, namespace: str, kind: str, name: str) -> tuple[str, str]:
        if kind == "Pod" and (namespace, kind, name) not in self.parent and (namespace, name) not in self.listed:
            by_prefix = self._controller_by_prefix(namespace, name)
            if by_prefix:
                kind, name = by_prefix
        for _ in range(OWNER_MAX_HOPS):
            parent = self.parent.get((namespace, kind, name))
            if parent is None:
                break
            kind, name = parent
        return kind, name

    def pod_total(self, owner_object: str) -> int:
        return len(self.pods_of.get(owner_object) or [])


def _pod_detail(pod: dict) -> dict:
    meta, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
    containers = spec.get("containers") or []
    return {
        "node": spec.get("nodeName"),
        "phase": status.get("phase"),
        "container_count": len(containers),
        "images": [c.get("image", "") for c in containers],
        "labels": meta.get("labels") or {},
        "node_selector": spec.get("nodeSelector") or {},
        "node_pins": pod_pins(spec),
        "node_affinity": required_node_terms(spec),
        "tolerations": pod_tolerations(spec),
        "extended_resources": pod_extended_resources(spec),
        "api_markers": _api_marker_hits(pod),
        "api_marker_sources": _api_marker_sources(pod),
        "started": status.get("startTime") or meta.get("creationTimestamp"),
    }


def _pod_last_activity(pod: dict) -> datetime | None:
    """When the pod last did something a symptom can date: the latest
    container termination, else the pod's start, else its creation."""
    status = pod.get("status") or {}
    stamps = []
    for cs in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
        for key in ("state", "lastState"):
            terminated = (cs.get(key) or {}).get("terminated") or {}
            if terminated.get("finishedAt"):
                stamps.append(parse_ts(terminated["finishedAt"]))
    # A pod that lost readiness without restarting is dated by that transition.
    ready = _condition(pod, "Ready")
    if ready.get("status") != "True" and ready.get("lastTransitionTime"):
        stamps.append(parse_ts(ready["lastTransitionTime"]))
    stamps = [t for t in stamps if t]
    if stamps:
        return max(stamps)
    return parse_ts(status.get("startTime")) or parse_ts((pod.get("metadata") or {}).get("creationTimestamp"))


def owner_rollouts(owners: list[dict]) -> dict[str, str]:
    """Each Deployment's newest ReplicaSet creation: evidence that a rollout
    happened, deciding nothing about a failure's age (a pod can run for
    months on an old ReplicaSet and fail only on the rebuilt node)."""
    newest_rs: dict[str, datetime] = {}
    for obj in owners:
        if obj.get("kind") != "ReplicaSet":
            continue
        controller = _controller_of(obj)
        created = parse_ts((obj.get("metadata") or {}).get("creationTimestamp"))
        if controller and created:
            key = _object_ref((obj.get("metadata") or {}).get("namespace", ""), controller[0], controller[1])
            newest_rs[key] = max(created, newest_rs.get(key, created))
    return {key: fmt_ts(created) for key, created in newest_rs.items()}


def owner_age_proofs(workloads: list[dict]) -> dict[str, tuple[str, str]]:
    """What a Deployment can prove about a failure's age: its earliest
    `Available=False` or `Progressing=False` transition, as (what, when).
    Consulted only for a container-level failure on a pod a drain
    recreated; a transition inside the window proves nothing, and the
    caller decides that. A ReplicaSet's age is no proof, and StatefulSets,
    DaemonSets and Jobs carry no such date; all are absent."""
    out: dict[str, tuple[str, str]] = {}
    for workload in workloads:
        if workload.get("kind") != "Deployment":
            continue
        meta = workload.get("metadata") or {}
        key = _object_ref(meta.get("namespace", ""), "Deployment", meta.get("name", ""))
        failures = [(parse_ts(c.get("lastTransitionTime")), f"{c.get('type')}=False") for c in (workload.get("status") or {}).get("conditions") or [] if c.get("type") in OWNER_FAILURE_CONDITIONS and c.get("status") == "False"]
        failures = [(t, what) for t, what in failures if t]
        if failures:
            earliest, what = min(failures)
            out[key] = (what, fmt_ts(earliest))
    return out


def pool_operation_windows(operations: list[dict]) -> dict[str, list[tuple[datetime, datetime]]]:
    """Each pool's UPGRADE_NODES spans in the window."""
    spans: dict[str, list[tuple[datetime, datetime]]] = {}
    for op in operations:
        target = parse_target_link(op.get("targetLink", ""))
        start, end = parse_ts(op.get("startTime")), parse_ts(op.get("endTime"))
        if op.get("operationType") == OP_UPGRADE_NODES and target and target[2] and start and end:
            spans.setdefault(target[2], []).append((start, end))
    return spans


def _pod_recreated_by_upgrade(pod: dict, pool: str, spans: dict[str, list[tuple[datetime, datetime]]]) -> bool:
    created = parse_ts((pod.get("metadata") or {}).get("creationTimestamp"))
    if not created:
        return False
    candidates = spans.get(pool, []) if pool else [span for pool_spans in spans.values() for span in pool_spans]
    return any(start <= created <= end for start, end in candidates)


def _pod_onset(pod: dict, activity: datetime | None) -> datetime | None:
    """When the symptom began, read from the object: a Pending pod's
    scheduling transition or start; for a pod that is not Ready, the Ready
    condition's transition to False, else the pod's start -- and for a pod
    that has restarted, the earlier of the two, because kubelet rewrites the
    transition at every crash. The last termination is the latest crash,
    never the onset (`activity` dates the window bound, not the symptom)."""
    status = pod.get("status") or {}
    meta = pod.get("metadata") or {}
    if status.get("phase") == PHASE_PENDING:
        scheduled = _condition(pod, "PodScheduled")
        return parse_ts(scheduled.get("lastTransitionTime")) or parse_ts(status.get("startTime")) or parse_ts(meta.get("creationTimestamp"))
    ready = _condition(pod, "Ready")
    transition = parse_ts(ready.get("lastTransitionTime")) if ready.get("status") == "False" else None
    started = parse_ts(status.get("startTime")) or parse_ts(meta.get("creationTimestamp"))
    restarted = any((cs.get("restartCount") or 0) > 0 or (cs.get("lastState") or {}).get("terminated") for cs in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []))
    if restarted:
        # A probe-less crash loop flips Ready at every restart, so the
        # transition trails the latest crash: the earliest evidence is the
        # pod's own start, or the transition if that is older.
        candidates = [t for t in (started, transition) if t]
        return min(candidates) if candidates else None
    return transition or started


def _pod_is_gated(pod: dict) -> bool:
    """A Pending pod the scheduler holds on its scheduling gates (a queue
    admission controller's): waiting for admission, whatever its age."""
    scheduled = _condition(pod, "PodScheduled")
    return (pod.get("status") or {}).get("phase") == PHASE_PENDING and scheduled.get("status") == "False" and scheduled.get("reason") == SCHEDULING_GATED_REASON


def _pod_is_starting(pod: dict, now: datetime | None) -> bool:
    """A pod younger than the grace whose containers are still being created
    or initialised, with no restart: not a symptom yet."""
    status = pod.get("status") or {}
    statuses = (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or [])
    if not now:
        return False
    started = parse_ts(status.get("startTime")) or parse_ts((pod.get("metadata") or {}).get("creationTimestamp"))
    young = started is not None and now - started < STARTING_GRACE
    if not statuses:
        # Bound, with no kubelet report yet: starting while younger than the grace.
        return young and status.get("phase") == PHASE_PENDING and _condition(pod, "PodScheduled").get("status") == "True"
    reasons = {((cs.get("state") or {}).get("waiting") or {}).get("reason") for cs in statuses}
    restarted = any((cs.get("restartCount") or 0) > 0 or (cs.get("lastState") or {}).get("terminated") for cs in statuses)
    return bool(reasons & set(STARTING_REASONS)) and not (reasons - set(STARTING_REASONS) - {None}) and not restarted and young


def pod_symptoms(pods: list[dict], resolver: Resolver, window_start: datetime | None = None, age_proofs: dict[str, tuple[str, str]] | None = None, pool_spans: dict[str, list[tuple[datetime, datetime]]] | None = None, node_pool: dict[str, str] | None = None, rollouts: dict[str, str] | None = None, now: datetime | None = None, starting: list[str] | None = None, gated: list[str] | None = None, terminating: list[str] | None = None) -> list[dict]:
    """One row per (top owner, category, reason), carrying the pods behind
    it; the row's detail (node, containers, images) is the example pod's.
    A pod whose last activity predates `window_start` is not the upgrade's
    and is not graded again; a guard its object already carries is held by
    the merge while the API shows the object failing (`failing_objects` on
    the review), as a re-check holds it."""
    rows: dict[tuple, dict] = {}
    for pod in pods:
        meta, status = pod.get("metadata") or {}, pod.get("status") or {}
        phase = status.get("phase")
        ready = _condition(pod, "Ready").get("status") == "True"
        if phase == PHASE_SUCCEEDED or (phase == PHASE_RUNNING and ready):
            continue
        # A Pending pod is a present condition; a pod whose last container
        # exit or start predates the window is an older story.
        if _pod_is_starting(pod, now):
            if starting is not None:
                starting.append(f"{meta.get('namespace', '')}/{meta.get('name', '')}")
            continue
        if _pod_is_gated(pod):
            # Held by scheduling gates: waiting for admission, not failing.
            if gated is not None:
                gated.append(f"{meta.get('namespace', '')}/{meta.get('name', '')}")
            continue
        if meta.get("deletionTimestamp"):
            # Being deleted: not Ready and stopped by design, not failing.
            if terminating is not None:
                terminating.append(f"{meta.get('namespace', '')}/{meta.get('name', '')}")
            continue
        activity = _pod_last_activity(pod)
        if window_start and activity and activity < window_start and phase != PHASE_PENDING:
            continue
        namespace, pod_name = meta.get("namespace", ""), meta.get("name", "")
        kind, name = resolver.resolve(namespace, "Pod", pod_name)
        obj = _object_ref(namespace, kind, name)
        # The pod's own evidence is the onset; the owner is proof of age only.
        onset = _pod_onset(pod, activity)
        source = ONSET_FROM_POD
        pool = (node_pool or {}).get((pod.get("spec") or {}).get("nodeName") or "", "")
        recreated = _pod_recreated_by_upgrade(pod, pool, pool_spans or {})
        proof = (age_proofs or {}).get(obj)
        base = {
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "object": obj,
            "system": is_system_namespace(namespace),
            "owner_kind": kind,
            "pods": [pod_name],
            "pod_count": 1,
            "pod_total": resolver.pod_total(obj),
            "example_pod": pod_name,
            "onset": fmt_ts(onset) if onset else None,
            "pod_onsets": {pod_name: fmt_ts(onset) if onset else None},
            "onset_source": source,
            "recreated_pods": [pod_name] if recreated else [],
            "age_proof": {"what": proof[0], "since": proof[1]} if proof else None,
            "rollout_at": (rollouts or {}).get(obj),
            "pod_nodes": {pod_name: (pod.get("spec") or {}).get("nodeName")},
            **_pod_detail(pod),
        }
        scheduled = _condition(pod, "PodScheduled")
        if phase == PHASE_PENDING and scheduled.get("status") == "False":
            _merge_pod_row(rows, {**base, "category": CATEGORY_PENDING, "reason": scheduled.get("reason") or REASON_UNSCHEDULABLE, "message": (scheduled.get("message") or "")[:MESSAGE_EXCERPT_CHARS]})
            continue
        container_reasons = []
        for cs in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
            for key in ("state", "lastState"):
                for state_name, state in (cs.get(key) or {}).items():
                    if state_name == "running":
                        continue
                    reason = state.get("reason") or ""
                    # A clean exit (`Completed`, exit 0) is not a failure: an init
                    # container that finished must not headline a not-Ready pod.
                    if (reason and reason != REASON_COMPLETED) or (state_name == "terminated" and state.get("exitCode") not in (None, 0)):
                        container_reasons.append({"container": cs.get("name"), "where": key, "reason": reason or state_name, "message": (state.get("message") or "")[:MESSAGE_EXCERPT_CHARS], "exit_code": state.get("exitCode"), "finished_at": state.get("finishedAt")})
        # The catalogue's reasons lead, so the row's headline is the one a
        # signature reads rather than whichever container came first.
        container_reasons.sort(key=lambda c: c["reason"] not in CONTAINER_FAILURE_REASONS)
        reason = container_reasons[0]["reason"] if container_reasons else (status.get("reason") or ("NotReady" if phase == PHASE_RUNNING else phase or "NotReady"))
        message = container_reasons[0]["message"] if container_reasons else (status.get("message") or "")[:MESSAGE_EXCERPT_CHARS]
        oom_nodes = {pod_name: (pod.get("spec") or {}).get("nodeName")} if any(c["reason"] == OOM_REASON for c in container_reasons) else {}
        _merge_pod_row(rows, {**base, "category": CATEGORY_NOT_READY, "reason": reason, "message": message, "containers": container_reasons, "oom_nodes": oom_nodes})
    return list(rows.values())


def _merge_pod_row(rows: dict[tuple, dict], row: dict) -> None:
    key = (row["object"], row["category"], row["reason"])
    existing = rows.get(key)
    if existing is None:
        rows[key] = row
        return
    existing["pod_nodes"].update(row.get("pod_nodes") or {})
    existing.setdefault("oom_nodes", {}).update(row.get("oom_nodes") or {})
    existing["recreated_pods"] = sorted(set(existing.get("recreated_pods") or []) | set(row.get("recreated_pods") or []))
    # Every replica's containers and images speak for the row, whatever order
    # kubectl listed them: the classifier reads them all.
    for container in row.get("containers") or []:
        if container not in (existing.get("containers") or []):
            existing.setdefault("containers", []).append(container)
    for image in row.get("images") or []:
        if image not in (existing.get("images") or []):
            existing.setdefault("images", []).append(image)
    existing["container_count"] = max(existing.get("container_count", 1), row.get("container_count", 1))
    existing["pods"].append(row["example_pod"])
    existing["pods"].sort()
    existing["pod_count"] = len(existing["pods"])
    existing["example_pod"] = existing["pods"][0]
    # The row keeps every pod's onset; its own is the earliest, the grading
    # in `mark_since` looks at each pod.
    existing["pod_onsets"].update(row.get("pod_onsets") or {})
    onsets = [o for o in existing["pod_onsets"].values() if o]
    existing["onset"] = min(onsets) if onsets else None


def node_symptoms(nodes: list[dict]) -> list[dict]:
    out = []
    for node in nodes:
        meta, conditions = node.get("metadata") or {}, {c.get("type"): c for c in (node.get("status") or {}).get("conditions") or []}
        for kind, bad in NODE_BAD_CONDITIONS:
            cond = conditions.get(kind)
            if cond and cond.get("status") == bad:
                name = meta.get("name", "")
                out.append({
                    "kind": "Node",
                    "namespace": "",
                    "name": name,
                    "object": _object_ref("", "Node", name),
                    "system": True,
                    "category": CATEGORY_NODE,
                    "node": name,
                    "pool": (meta.get("labels") or {}).get(NODEPOOL_LABEL, ""),
                    "reason": f"{kind}={bad}" if kind != "Ready" else ("NotReady" if bad == "False" else "Unknown"),
                    "message": (cond.get("message") or cond.get("reason") or "")[:MESSAGE_EXCERPT_CHARS],
                    "onset": fmt_ts(parse_ts(cond.get("lastTransitionTime"))) if parse_ts(cond.get("lastTransitionTime")) else None,
                })
    return out


def _event_time(event: dict) -> datetime | None:
    """When the event last fired. A series-shaped event (events.k8s.io read
    back through core/v1) carries its first observation in `eventTime` and
    its last in `series.lastObservedTime`, so the series comes first."""
    series = event.get("series") or {}
    for value in (series.get("lastObservedTime"), event.get("lastTimestamp"), event.get("eventTime"), (event.get("metadata") or {}).get("creationTimestamp")):
        ts = parse_ts(value)
        if ts:
            return ts
    return None


def _event_count(event: dict) -> int:
    series = event.get("series") or {}
    return int(series.get("count") or event.get("count") or 1)


def event_symptoms(events: list[dict], window_start: datetime, resolver: Resolver) -> list[dict]:
    """Warning events since `window_start`, one row per (top owner, reason);
    a pod's or ReplicaSet's event is charged to the owner that outlives it."""
    rows: dict[tuple, dict] = {}
    for event in events:
        if event.get("type") and event.get("type") != "Warning":
            continue
        message = event.get("message") or ""
        reason = event.get("reason") or ""
        if reason not in EVENT_REASONS and not any(marker in message for marker in EVENT_MESSAGE_MARKERS):
            continue
        last = _event_time(event)
        if last is None or last < window_start:
            continue
        obj = event.get("involvedObject") or {}
        namespace, involved_kind, involved_name = obj.get("namespace") or "", obj.get("kind") or "", obj.get("name") or ""
        kind, name = resolver.resolve(namespace, involved_kind, involved_name)
        key = (namespace, kind, name, reason)
        row = rows.get(key)
        if row is None:
            rows[key] = row = {
                "kind": kind,
                "namespace": namespace,
                "name": name,
                "object": _object_ref(namespace, kind, name),
                "system": is_system_namespace(namespace) if namespace else True,
                "category": CATEGORY_EVENT,
                "reason": reason,
                "message": message[:MESSAGE_EXCERPT_CHARS],
                "count": 0,
                "first_seen": fmt_ts(parse_ts(event.get("firstTimestamp")) or parse_ts(event.get("eventTime")) or last),
                "last_seen": fmt_ts(last),
            }
            rows[key]["onset"] = rows[key]["first_seen"]
            if involved_kind == "Pod" and (kind, name) != (involved_kind, involved_name):
                row.update(pods=[], pod_count=0, pod_total=resolver.pod_total(row["object"]), example_pod=involved_name)
        if "pods" in row and involved_name not in row["pods"]:
            row["pods"].append(involved_name)
            row["pods"].sort()
            row["pod_count"], row["example_pod"] = len(row["pods"]), row["pods"][0]
        # The earliest first observation across the merged events dates the row.
        first = fmt_ts(parse_ts(event.get("firstTimestamp")) or parse_ts(event.get("eventTime")) or last)
        if first < row["first_seen"]:
            row["first_seen"] = row["onset"] = first
        row["count"] += _event_count(event)
        if fmt_ts(last) > row["last_seen"]:
            row["last_seen"], row["message"] = fmt_ts(last), message[:MESSAGE_EXCERPT_CHARS]
    return list(rows.values())


def _selector_matches(selector: dict, labels: dict) -> bool:
    """A label selector evaluated in full: `matchLabels` and `matchExpressions`
    (In, NotIn, Exists, DoesNotExist). As `policy/v1` reads a budget's: an
    empty selector matches every pod in the namespace; the callers keep an
    absent (null) selector, which matches none, away from here."""
    match = (selector or {}).get("matchLabels") or {}
    expressions = (selector or {}).get("matchExpressions") or []
    if not all(labels.get(k) == v for k, v in match.items()):
        return False
    for expr in expressions:
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


def required_node_terms(spec: dict) -> list[dict]:
    """A pod spec's required node-affinity terms as the API holds them."""
    return list((((spec.get("affinity") or {}).get("nodeAffinity") or {}).get(AFFINITY_REQUIRED_KEY) or {}).get("nodeSelectorTerms") or [])


def pod_tolerations(spec: dict) -> list[dict]:
    return [t for t in spec.get("tolerations") or [] if isinstance(t, dict)]


def pod_extended_resources(spec: dict) -> list[str]:
    """The extended resources the pod requests: domain-qualified names outside
    kubernetes.io, such as `nvidia.com/gpu`."""
    names: set[str] = set()
    for container in spec.get("containers") or []:
        for key in ((container.get("resources") or {}).get("requests") or {}):
            if RESOURCE_DOMAIN_SEPARATOR in key and not key.startswith(BUILTIN_RESOURCE_DOMAIN):
                names.add(key)
    return sorted(names)


def pod_pins(spec: dict) -> list[dict[str, list[str]]]:
    """A pod spec's placement pins as the scheduler reads them: one mapping of
    label to allowed values per required node-affinity term (terms are
    alternatives), each merged with the nodeSelector (a conjunction). A spec
    with no required term is one term; a spec that pins nothing is []."""
    base = {key: [value] for key, value in sorted((spec.get("nodeSelector") or {}).items())}
    required = ((spec.get("affinity") or {}).get("nodeAffinity") or {}).get(AFFINITY_REQUIRED_KEY) or {}
    pins: list[dict[str, list[str]]] = []
    for term in list(required.get("nodeSelectorTerms") or []) or [{}]:
        pin = {key: list(values) for key, values in base.items()}
        for expr in term.get("matchExpressions") or []:
            key, values = expr.get("key"), expr.get("values") or []
            if expr.get("operator") != AFFINITY_OPERATOR_IN or not key or not values:
                continue
            pin[key] = sorted(set(values) & set(pin[key])) if key in pin else sorted(set(values))
        if pin:
            pins.append(pin)
    return pins


def pins_of_selector(selector: dict | None) -> list[dict[str, list[str]]]:
    return pod_pins({"nodeSelector": selector or {}})


def _pinned_pools(pins: list[dict[str, list[str]]], label_pools: dict[tuple[str, str], set[str]]) -> set[str]:
    """The pools a pod's pins narrow it to, out of the pools a pod could land
    on: every pool a node reports (not every pool the cluster record lists; a
    labelled pool at zero nodes carries no node label). A pin by the pool
    label itself always counts, on one pool as on many. A pin by any other
    label counts only when it narrows that set, so a label every node carries
    (`kubernetes.io/os`) is no preference. The union over the terms."""
    universe: set[str] = set().union(*(found for (key, _), found in label_pools.items() if key == NODEPOOL_LABEL))
    pools: set[str] = set()
    for pin in pins:
        term: set[str] | None = None
        for key, values in pin.items():
            matching = set(values) if key == NODEPOOL_LABEL else {pool for value in values for pool in label_pools.get((key, value), set())}
            term = matching if term is None else term & matching
        term = term or set()
        # Narrowing: anything short of covering every pool with nodes (a
        # labelled pool at zero nodes included).
        if NODEPOOL_LABEL in pin or not universe or not term >= universe:
            pools |= term
    return pools


def _requirement_holds(expr: dict, values: dict) -> bool:
    """One NodeSelectorRequirement against a node's labels (or its fields):
    the four label operators as `_selector_matches` reads them, `Gt` and
    `Lt` by an integer compare."""
    key, op = str(expr.get("key") or ""), str(expr.get("operator") or "")
    listed = [str(v) for v in expr.get("values") or []]
    if op in LABEL_SELECTOR_OPERATORS:
        return _selector_matches({"matchExpressions": [{"key": key, "operator": op, "values": listed}]}, values)
    if op in (AFFINITY_OPERATOR_GT, AFFINITY_OPERATOR_LT):
        try:
            have, want = int(values.get(key)), int(listed[0])
        except (TypeError, ValueError, IndexError):
            return False
        return have > want if op == AFFINITY_OPERATOR_GT else have < want
    return False


def _node_satisfies(selector: dict, terms: list[dict], name: str, labels: dict) -> bool:
    """The API's own predicate: the node carries every `nodeSelector` label
    with its value and satisfies any one required term, every expression of
    the term by its operator, `matchFields` on the node's name."""
    if any(labels.get(k) != v for k, v in (selector or {}).items()):
        return False
    if not terms:
        return True
    fields = {NODE_NAME_FIELD: name}
    return any(all(_requirement_holds(e, labels) for e in term.get("matchExpressions") or []) and all(_requirement_holds(e, fields) for e in term.get("matchFields") or []) for term in terms)


def _expression_text(expr: dict) -> str:
    key, op = expr.get("key") or "", expr.get("operator") or ""
    values = [str(v) for v in expr.get("values") or []]
    if op in (AFFINITY_OPERATOR_GT, AFFINITY_OPERATOR_LT):
        return f"{key} {op} {values[0] if values else '?'}"
    return f"{key} {op} [{'|'.join(values)}]" if values else f"{key} {op}"


def _placement_text(selector: dict, terms: list[dict]) -> str:
    """The pins as text: `key=value` for the nodeSelector, each required term's
    expressions by operator, terms joined by ` or `."""
    pairs = ",".join(f"{k}={v}" for k, v in sorted((selector or {}).items()))
    rendered = [",".join(_expression_text(e) for e in (term.get("matchExpressions") or []) + (term.get("matchFields") or [])) for term in terms or []]
    rendered = [r for r in rendered if r]
    if pairs and rendered:
        return f"{pairs} and ({' or '.join(rendered)})"
    return pairs or " or ".join(rendered)


def pool_nodes_of(nodes: list[dict]) -> dict[str, list[tuple[list[dict], set[str]]]]:
    """Per pool, each node's taints and the extended resources it offers."""
    out: dict[str, list[tuple[list[dict], set[str]]]] = {}
    for node in nodes:
        labels = (node.get("metadata") or {}).get("labels") or {}
        pool = labels.get(NODEPOOL_LABEL, "")
        if not pool:
            continue
        taints = [t for t in (node.get("spec") or {}).get("taints") or [] if isinstance(t, dict)]
        offered = {k for k in (node.get("status") or {}).get("allocatable") or {} if RESOURCE_DOMAIN_SEPARATOR in k}
        out.setdefault(pool, []).append((taints, offered))
    return out


def _tolerates(tolerations: list[dict], taint: dict) -> bool:
    """Whether the pod's tolerations cover a taint; a taint whose effect does
    not refuse scheduling needs none."""
    if (taint.get("effect") or "") not in BLOCKING_TAINT_EFFECTS:
        return True
    for t in tolerations:
        key_ok = (t.get("key") or "") == (taint.get("key") or "") or (not t.get("key") and t.get("operator") == TOLERATION_EXISTS)
        value_ok = t.get("operator") == TOLERATION_EXISTS or (t.get("value") or "") == (taint.get("value") or "")
        effect_ok = not t.get("effect") or t.get("effect") == taint.get("effect")
        if key_ok and value_ok and effect_ok:
            return True
    return False


def _placeable_pools(symptom: dict, ctx: Context) -> set[str] | None:
    """The pools the scheduler could place the pod on, pins aside: those with
    a node whose taints the pod tolerates and that offers every extended
    resource it requests. An upgraded pool is placeable too on evidence the
    resource was there before the upgrade's own mechanism (entry 18) took it
    out of allocatable: a blocking taint keyed by the resource on the node
    (GKE's `nvidia.com/gpu=present`), the accelerator label on the pool's
    nodes, or the pool's accelerator config in the cluster record -- never a
    taint the pod merely tolerates, which says nothing of what the pool
    offered, and never for a pool no operation touched, where allocatable
    still speaks. An upgraded pool with no node left counts when its
    operation says it had nodes to upgrade (a failed surge, not an idle
    pool), the record carries what the pod requests and the pod tolerates
    the record's taints. None without a nodes read."""
    if ctx.pool_nodes is None:
        return None
    tolerations = symptom.get("tolerations") or []
    requested = set(symptom.get("extended_resources") or [])
    labelled: dict[str, set[str]] = {}
    for (label, _), pools_with in ctx.label_pools.items():
        if label in ACCELERATOR_LABEL_RESOURCES:
            for pool in pools_with:
                labelled.setdefault(pool, set()).add(ACCELERATOR_LABEL_RESOURCES[label])

    def recorded(pool: str) -> dict:
        return ctx.pool_records.get(pool) or {}

    def carried(pool: str, taints: list[dict]) -> set[str]:
        keyed = {taint.get("key") for taint in taints if (taint.get("effect") or "") in BLOCKING_TAINT_EFFECTS}
        return keyed | labelled.get(pool, set()) | set(recorded(pool).get("resources") or ())

    def placeable(pool: str, taints: list[dict], offered: set[str]) -> bool:
        if not all(_tolerates(tolerations, taint) for taint in taints):
            return False
        return requested <= offered or (pool in ctx.upgraded_pools and requested <= carried(pool, taints))

    pools = {pool for pool, nodes in ctx.pool_nodes.items() if any(placeable(pool, taints, offered) for taints, offered in nodes)}
    for pool, row in ctx.upgraded_pools.items():
        if pool in ctx.pool_nodes or not (row.get("nodes") or 0) > 0:
            continue
        if requested <= set(recorded(pool).get("resources") or ()) and all(_tolerates(tolerations, taint) for taint in recorded(pool).get("taints") or []):
            pools.add(pool)
    return pools


def _covered_pod_pools(pod: dict, node_pool: dict[str, str], owner_pins: list[dict[str, list[str]]], pool_spans: dict[str, list[tuple[datetime, datetime]]], label_pools: dict[tuple[str, str], set[str]]) -> set[str]:
    """The pools a budget's pod belongs to: where it sits, else the pools its
    own or its owner's pins name (nodeSelector or required node affinity, by
    the pool label or any pool label; a label every node carries names
    none), else, for a Pending pod, the pool whose drain was running when it
    was created (the replica the drain displaced)."""
    spec = pod.get("spec") or {}
    placed = node_pool.get(spec.get("nodeName") or "", "")
    if placed:
        return {placed}
    for pins in (pod_pins(spec), owner_pins or []):
        pools = _pinned_pools(pins, label_pools)
        if pools:
            return pools
    return _drain_pools_at_creation(pod, pool_spans)


def _drain_pools_at_creation(pod: dict, pool_spans: dict[str, list[tuple[datetime, datetime]]]) -> set[str]:
    """For a Pending pod with no node, the pools whose drain was running when
    it was created: the replica the drain displaced. Empty otherwise."""
    created = parse_ts((pod.get("metadata") or {}).get("creationTimestamp"))
    if not created or (pod.get("status") or {}).get("phase") != PHASE_PENDING or (pod.get("spec") or {}).get("nodeName"):
        return set()
    return {candidate for candidate, spans in pool_spans.items() if any(start <= created <= end for start, end in spans)}


def pdb_symptoms(pdbs: list[dict], pods: list[dict], nodes: list[dict], upgraded_pools: dict[str, dict], pool_spans: dict[str, list[tuple[datetime, datetime]]] | None = None, owner_pins: dict[str, list[dict[str, list[str]]]] | None = None, resolver: Resolver | None = None, label_pools: dict[tuple[str, str], set[str]] | None = None) -> list[dict]:
    """Entry 1's after-signal: a budget allowing no disruption whose pods
    belong to a pool an `UPGRADE_NODES` touched in the window -- where they
    sit, or, for the replicas the drain displaced, where they came from. A
    budget whose spec allows disruption and sits at zero only because its
    covered pods are not ready is marked downstream: the hold is real, the
    thing to fix is the pods' own incident."""
    node_pool = {(n.get("metadata") or {}).get("name"): ((n.get("metadata") or {}).get("labels") or {}).get(NODEPOOL_LABEL, "") for n in nodes}
    out = []
    for pdb in pdbs:
        meta, status = pdb.get("metadata") or {}, pdb.get("status") or {}
        if status.get("disruptionsAllowed") != 0:
            continue
        namespace, name = meta.get("namespace", ""), meta.get("name", "")
        # policy/v1: an empty selector covers every pod in the namespace, an
        # absent one covers none.
        selector = (pdb.get("spec") or {}).get("selector")
        covered = [] if selector is None else [p for p in pods if (p.get("metadata") or {}).get("namespace") == namespace and _selector_matches(selector, (p.get("metadata") or {}).get("labels") or {})]
        pools = set()
        for p in covered:
            meta = p.get("metadata") or {}
            pins: list[dict[str, list[str]]] = []
            if resolver is not None:
                kind, oname = resolver.resolve(meta.get("namespace", ""), "Pod", meta.get("name", ""))
                pins = (owner_pins or {}).get(_object_ref(meta.get("namespace", ""), kind, oname), [])
            pools |= _covered_pod_pools(p, node_pool, pins, pool_spans or {}, label_pools or {})
        pools = sorted(pools - {""})
        touched = [pool for pool in pools if pool in upgraded_pools]
        if not touched:
            # A budget nothing drained in the window is the readiness
            # report's before-signal, not this report's failure.
            continue
        spec = {k: v for k, v in (pdb.get("spec") or {}).items() if k != "selector"}
        expected, healthy = status.get("expectedPods"), status.get("currentHealthy")
        if isinstance(expected, int) and not isinstance(expected, bool) and expected == 0:
            # The controller counts no pod (a workload scaled to zero, its
            # pods still terminating): the budget blocks no drain and is
            # graded on neither leg, as `budget_shapes` grades it.
            continue
        forbids = _budget_allows_no_disruption(spec, expected if isinstance(expected, int) else (len(covered) or None))
        out.append({
            "spec": spec,
            "spec_forbids": forbids,
            "budget_downstream": not forbids,
            "expected_pods": expected,
            "not_ready": (expected - healthy) if isinstance(expected, int) and isinstance(healthy, int) else None,
            "kind": "PodDisruptionBudget",
            "namespace": namespace,
            "name": name,
            "object": _object_ref(namespace, "PodDisruptionBudget", name),
            "system": is_system_namespace(namespace),
            "category": CATEGORY_PDB,
            "reason": "disruptionsAllowed=0",
            "message": f"spec={json.dumps({k: v for k, v in (pdb.get('spec') or {}).items() if k != 'selector'}, sort_keys=True)} pods={len(covered)} pools={pools or 'none'}",
            "pools": pools,
            "upgraded_pools": touched,
            "operation": ",".join(sorted(str(upgraded_pools[pool]["operation"]) for pool in touched)),
        })
    return out


def operation_metrics(op: dict) -> dict[str, int]:
    """The integer metrics an operation's `progress` carries, by name."""
    out: dict[str, int] = {}
    for metric in (op.get("progress") or {}).get("metrics") or []:
        name, value = metric.get("name"), metric.get("intValue")
        if not name or value is None:
            continue
        try:
            out[name] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def upgraded_pools_of(operations: list[dict], nodes: list[dict]) -> dict[str, dict]:
    """Node pools an `UPGRADE_NODES` touched in the window, with the longest
    operation's duration, the nodes it upgraded (`NODES_TOTAL` when it
    carries progress metrics, else the pool's current node count) and the
    delay its disruption budgets caused (`NODE_PDB_DELAY_SECONDS`; None
    without metrics)."""
    counts: dict[str, int] = {}
    for node in nodes:
        pool = ((node.get("metadata") or {}).get("labels") or {}).get(NODEPOOL_LABEL, "")
        counts[pool] = counts.get(pool, 0) + 1
    pools: dict[str, dict] = {}
    for op in operations:
        if op.get("operationType") != OP_UPGRADE_NODES:
            continue
        target = parse_target_link(op.get("targetLink", ""))
        if not target or not target[2]:
            continue
        summary = operation_summary(op)
        metrics = operation_metrics(op)
        row = pools.setdefault(target[2], {"nodes": counts.get(target[2], 0), "longest_s": 0, "operation": summary["name"], "start": summary["start"], "pdb_delay_s": None})
        if (summary["duration_s"] or 0) > row["longest_s"]:
            row.update(longest_s=summary["duration_s"] or 0, operation=summary["name"], start=summary["start"], nodes=metrics.get(OPERATION_METRIC_NODES_TOTAL) or counts.get(target[2], 0), pdb_delay_s=metrics.get(OPERATION_METRIC_PDB_DELAY))
    return pools


# --------------------------------------------------------------------------- #
# The classifier.
# --------------------------------------------------------------------------- #


class Context(NamedTuple):
    cgroup_modes: dict[str, str]  # pool -> effectiveCgroupMode
    node_pool: dict[str, str]  # node -> pool
    upgraded_pools: dict[str, dict]  # pool -> what `upgraded_pools_of` returns
    master_upgraded: bool = False  # an UPGRADE_MASTER ended in the window
    images_on_untouched_pools: frozenset = frozenset()  # images Running on a pool no operation touched
    operations_known: bool = True  # False when the project's operations listing failed
    label_pools: dict = {}  # (label, value) -> the pools whose nodes carry it
    nodes_view: tuple | None = None  # (name, labels) per node; None when the nodes read did not answer
    pool_nodes: dict | None = None  # pool -> [(taints, extended resources offered)] per node; None without the nodes read
    failing_pods: dict = {}  # (namespace, pod) -> the API-set field that shows it failing
    failing_objects: dict = {}  # owner object -> the API-set field that shows it failing
    pool_versions: dict = {}  # pool -> its version from the cluster describe
    pool_records: dict = {}  # pool -> what `pool_records_of` returns: the record's resources and config taints
    control_plane: str = ""


def pool_labels(cluster: dict, nodes: list[dict]) -> dict[tuple[str, str], set[str]]:
    """Which pools carry each label: from each pool's `config.labels` in the
    cluster record, and from the nodes' own labels, so a pod's preference on
    a custom label resolves to a pool."""
    out: dict[tuple[str, str], set[str]] = {}
    for pool in cluster.get("nodePools") or []:
        for key, value in ((pool.get("config") or {}).get("labels") or {}).items():
            out.setdefault((key, value), set()).add(pool.get("name", ""))
    for node in nodes:
        labels = (node.get("metadata") or {}).get("labels") or {}
        pool = labels.get(NODEPOOL_LABEL, "")
        if pool:
            for key, value in labels.items():
                out.setdefault((key, value), set()).add(pool)
    return out


def _selector_pools(selector: dict, label_pools: dict[tuple[str, str], set[str]]) -> set[str]:
    """`_pinned_pools` for a bare nodeSelector."""
    return _pinned_pools(pins_of_selector(selector), label_pools)


def _classification(entry: int | None, confidence: str, evidence: str, detail: str = "") -> dict:
    return {
        "entry": entry,
        "title": MITIGATIONS[entry]["title"] if entry else UNCLASSIFIED,
        "confidence": confidence,
        "evidence": evidence[:MESSAGE_EXCERPT_CHARS],
        "detail": detail,
    }


def _container_text_is_tenant(container: dict) -> bool:
    """A terminated state's message is the container's termination message,
    which the workload writes; a waiting state's message is the kubelet's."""
    return container.get("exit_code") is not None or bool(container.get("finished_at"))


def _scopes_of(symptom: dict) -> dict[str, list[tuple[str, bool, str | None]]]:
    """The texts a signature is searched in, per scope: each with whether the
    API server set it (a condition, a container state the kubelet wrote) or
    a namespace user could have (an Event, a termination message), and the
    container it belongs to."""
    reason, message = symptom.get("reason") or "", symptom.get("message") or ""
    containers = symptom.get("containers") or []
    event = symptom["category"] == CATEGORY_EVENT
    # A not-ready row's own message is its headline container's and is graded
    # with it; an event's is tenant text; a Pending pod's is the scheduler's.
    own_api = not event and not (containers and _container_text_is_tenant(containers[0]))
    own_container = containers[0]["container"] if containers else None
    scopes = {
        SCOPE_REASON: [(reason, not event, own_container)] + [(c["reason"], True, c["container"]) for c in containers],
        SCOPE_ANY: [(f"{reason} {message}".strip(), own_api, own_container)] + [(f"{c['reason']} {c['message']}".strip(), not _container_text_is_tenant(c), c["container"]) for c in containers],
    }
    if symptom["category"] == CATEGORY_PENDING or (event and reason == REASON_FAILED_SCHEDULING):
        scopes[SCOPE_SCHEDULING] = [(message, not event, None)]
    if symptom["category"] == CATEGORY_NOT_READY:
        scopes[SCOPE_CONTAINER] = [(c["message"], not _container_text_is_tenant(c), c["container"]) for c in containers if c["message"]]
    return scopes

def _image_host(image: str) -> str:
    m = IMAGE_HOST_RE.match(image)
    return m.group(1) if m else DEFAULT_IMAGE_HOST


def _image_host_detail(symptom: dict) -> str:
    hosts = sorted({_image_host(image) for image in symptom.get("images") or []})
    return "image host " + ", ".join(hosts) if hosts else ""


def _gate_open(symptom: dict, entry: int, ctx: Context) -> bool:
    """Whether an operation in the window reached the object: its pool for a
    node-side mechanism, an UPGRADE_MASTER for 6 and 7. A Pending pod is
    placed by its nodeSelector's pool label when it names one, else any
    upgraded pool counts."""
    if entry in CONTROL_PLANE_ENTRIES:
        return ctx.master_upgraded
    # Every pod of a merged row speaks: any replica on a touched pool opens.
    nodes = [symptom.get("node")] + list((symptom.get("pod_nodes") or {}).values())
    pools = {symptom.get("pool") or ""} | {ctx.node_pool.get(n or "", "") for n in nodes}
    pools.discard("")
    if pools:
        return any(pool in ctx.upgraded_pools for pool in pools)
    # No node: a Pending pod names its pool through its pins (nodeSelector or
    # required node affinity) -- the pool label, or any label the pools' nodes
    # carry; only a pod with no pool preference counts any upgraded pool, and
    # an event row with no pool evidence at all stays closed.
    pins = symptom.get("node_pins")
    if pins is None:
        pins = pins_of_selector(symptom.get("node_selector"))
    preferred = _pinned_pools(pins, ctx.label_pools)
    if preferred:
        return any(pool in ctx.upgraded_pools for pool in preferred)
    if symptom["category"] == CATEGORY_EVENT:
        return False
    # No pin: the pools the pod could be evaluated on are those whose taints
    # it tolerates and that offer the extended resources it requests, not
    # every pool; a CPU pod does not land on a tainted GPU pool, nor a GPU
    # pod on a CPU pool, so those pools' operations do not open its gate.
    placeable = _placeable_pools(symptom, ctx)
    if placeable is None:
        return bool(ctx.upgraded_pools)
    return any(pool in ctx.upgraded_pools for pool in placeable)


def _api_agreement(symptom: dict, container: str | None, ctx: Context) -> str:
    """The field the API server set that agrees with text a namespace user
    could have written, or "" when none does: for a termination message the
    same container's current state, else the pod's phase; for an event, a
    pod of the row or the row's object that the pods and owners reads show
    failing."""
    if symptom["category"] == CATEGORY_EVENT:
        namespace = symptom.get("namespace") or ""
        pods = list(symptom.get("pods") or []) or ([symptom["name"]] if symptom.get("kind") == "Pod" else [])
        for name in pods:
            if (namespace, name) in ctx.failing_pods:
                return f"pod {name} {ctx.failing_pods[(namespace, name)]}"
        return ctx.failing_objects.get(symptom["object"], "")
    for c in symptom.get("containers") or []:
        if c.get("container") == container and c.get("where") == "state":
            return f"container {container} state {c['reason']}"
    phase = symptom.get("phase")
    if phase in (PHASE_PENDING, PHASE_FAILED):
        return f"pod phase {phase}"
    return ""


def _dropped_label(symptom: dict, ctx: Context) -> str:
    """The selector label the catalogue lists as dropped at or before the
    minor of the pool the pod targets (its control plane's when it names no
    pool), or "" when it names none."""
    pins = symptom.get("node_pins")
    if pins is None:
        pins = pins_of_selector(symptom.get("node_selector"))
    # The pool to grade against is named by the pins other than the dropped
    # label itself: a label no node carries any more resolves to no pool.
    placing = [{key: values for key, values in pin.items() if key not in DROPPED_NODE_LABELS} for pin in pins]
    pool = next(iter(sorted(_pinned_pools(placing, ctx.label_pools))), "")
    parsed = parse_version(ctx.pool_versions.get(pool) or ctx.control_plane or "")
    for label in sorted({key for pin in pins for key in pin}):
        dropped_at = DROPPED_NODE_LABELS.get(label)
        if dropped_at and parsed and (parsed[0], parsed[1]) >= dropped_at:
            return LABEL_DROPPED_FORMAT.format(label=label, major=dropped_at[0], minor=dropped_at[1])
    return ""


def _match_signature(symptom: dict, entry: int, ctx: Context) -> dict | None:
    """The entry's first signature row that matches, preferring text the API
    server set over text a namespace user could have written; None when no
    row holds."""
    scopes = _scopes_of(symptom)
    for confidence, scope, pattern in SIGNATURE_ROWS.get(entry) or []:
        hits = [(index, hit) for index, hit in enumerate(scopes.get(scope) or []) if pattern.search(hit[0])]
        if not hits:
            continue
        index, (text, api_set, container) = next(((i, hit) for i, hit in hits if hit[1]), hits[0])
        m = pattern.search(text)
        detail = ""
        clauses = ""
        if entry == ENTRY_NODE_LABEL:
            # The discriminator is the nodes read, not the message. The
            # scheduler writes a selector-miss clause for every node group a
            # pinned pod does not target, so on a cluster with more than one
            # pool every pinned Pending pod carries one. When a node
            # satisfies the pins by the API's own predicate (the
            # nodeSelector and any one required term, every expression by
            # its operator), the pod's own pool evaluated it and refused it
            # for the reason in that pool's clause: rows 2 and 18 were tried
            # first, and what remains is unclassified, never 12. Row 12
            # holds only when no node satisfies the pins, and is medium even
            # then (a label the pool never carried, a deleted pool or a pool
            # at zero nodes read the same) unless the label is in
            # `DROPPED_NODE_LABELS`. An event row's pins are its owner's
            # template's; with no pins, or no nodes read, nothing can say.
            selector = symptom.get("node_selector") or {}
            terms = [t for t in symptom.get("node_affinity") or [] if t.get("matchExpressions") or t.get("matchFields")]
            if not (selector or terms) or ctx.nodes_view is None or any(_node_satisfies(selector, terms, name, labels) for name, labels in ctx.nodes_view):
                continue
            clauses = SCHEDULING_TAIL_RE.split(text, 1)[0].rstrip(".")
            others = [clause.strip() for clause in clauses.split(CLAUSE_LIST_MARKER, 1)[-1].split(CLAUSE_SEPARATOR) if clause.strip() and not pattern.search(clause)]
            dropped = _dropped_label(symptom, ctx)
            detail = f"{NO_NODE_CARRIES_FORMAT.format(labels=_placement_text(selector, terms))}; {dropped or LABEL_NOT_DROPPED_TEXT}"
            if others:
                detail += f"; {OWN_POOL_REASON_FORMAT.format(clauses=', '.join(others))}"
            if not dropped:
                confidence = MEDIUM
        elif entry == ENTRY_WEBHOOK:
            name = WEBHOOK_NAME_RE.search(text)
            detail = f"webhook {name.group(1)}" if name else ""
        elif entry == ENTRY_REGISTRY:
            detail = _image_host_detail(symptom)
        # High only when the signature names the entry's own mechanism and the
        # object's pool had an operation in the window (for a control-plane
        # mechanism, an UPGRADE_MASTER); otherwise medium.
        if confidence == HIGH and not ctx.operations_known:
            # The listing failed: nothing can say what touched the pool.
            confidence = MEDIUM
            detail = (detail + "; " if detail else "") + GATE_UNKNOWN_TEXT
        elif confidence == HIGH and not _gate_open(symptom, entry, ctx):
            confidence = MEDIUM
            detail = (detail + "; " if detail else "") + GATE_CLOSED_TEXT
        if entry == ENTRY_REGISTRY and confidence == HIGH:
            # The mechanism is a rebuilt node that cannot pull what the old
            # ones had cached: high needs the same image still running on a
            # pool no operation touched.
            elsewhere = [img for img in symptom.get("images") or [] if img in ctx.images_on_untouched_pools]
            if elsewhere:
                detail = (detail + "; " if detail else "") + f"same image running on an untouched pool ({elsewhere[0]})"
            else:
                confidence = MEDIUM
                detail = (detail + "; " if detail else "") + "no pod with this image running on an untouched pool"
        # A reason-only hit is evidenced by that container's reason with its
        # message: the reason and any scopes list the row's own entry first
        # and then each container in order, so the index names the container.
        # Row 12 quotes the clause set whole; it decided nothing.
        if clauses:
            evidence = clauses
        elif scope == SCOPE_REASON:
            evidence = scopes[SCOPE_ANY][index][0]
        else:
            evidence = text if scope == SCOPE_ANY else m.group(0)
        if not api_set:
            # Text a namespace user can write: quoted as the object's own, and
            # never high on its own.
            source = TEXT_SOURCE_EVENT if symptom["category"] == CATEGORY_EVENT else TEXT_SOURCE_TERMINATION
            evidence = QUOTED_TEXT_FORMAT.format(source=source, text=evidence)
            agrees = _api_agreement(symptom, container, ctx)
            if agrees:
                detail = (detail + "; " if detail else "") + TEXT_AGREED_FORMAT.format(source=source, agrees=agrees)
            else:
                confidence = MEDIUM
                detail = (detail + "; " if detail else "") + FROM_EVENT_TEXT_DETAIL.format(source=source)
        return _classification(entry, confidence, evidence, detail)
    return None


def _match_removed_api(symptom: dict, entry: int, ctx: Context) -> dict | None:
    """Entry 6: the removed API named in text, else a Job pod in Error whose
    spec names one, best effort."""
    verdict = _match_signature(symptom, entry, ctx)
    if verdict is not None:
        return verdict
    containers = symptom.get("containers") or []
    if symptom["category"] == CATEGORY_NOT_READY and symptom.get("owner_kind") in JOB_OWNER_KINDS:
        hits = symptom.get("api_markers") or []
        if hits and any(c["reason"] == REASON_ERROR for c in containers):
            sources = symptom.get("api_marker_sources") or {}
            where = " and ".join(place for place in ("name", "spec") if sources.get(place)) or "name"
            return _classification(entry, MEDIUM, f"{symptom['owner_kind']} pod in Error; {where} mentions {', '.join(hits)}", "best effort: a name, not an API call")
    return None


def _oom_reading(symptom: dict, ctx: Context) -> dict | None:
    """What an OOM-killed row says, read from one replica: among the
    OOM-killed replicas the strongest decides, and the gate and the cgroup
    grade are read from that one replica's pool (one on a touched cgroup v2
    pool, else any v2 pool, else the example's)."""
    oom = [c for c in symptom.get("containers") or [] if c["reason"] == OOM_REASON]
    if not oom:
        return None
    candidates = [ctx.node_pool.get(n or "", "") for n in (symptom.get("oom_nodes") or {}).values()] or [ctx.node_pool.get(symptom.get("node") or "", "")]
    v2 = [p for p in candidates if ctx.cgroup_modes.get(p, "") == CGROUP_V2_MODE]
    pool = next((p for p in v2 if p in ctx.upgraded_pools), v2[0] if v2 else candidates[0])
    mode = ctx.cgroup_modes.get(pool, "")
    return {
        "evidence": f"container {oom[0]['container']} {OOM_REASON} exit {oom[0]['exit_code']} (pool {pool or '?'} {mode or 'cgroup mode unknown'})",
        "mode": mode,
        "count": int(symptom.get("container_count") or 1),
        "count_text": f"{symptom.get('container_count', 1)} container(s)",
        "runtime": next((f"{img} ({cgroup_v1_runtime(img)})" for img in symptom.get("images") or [] if cgroup_v1_runtime(img)), None),
        # The gate is the same replica's pool as the cgroup grade; only a pod
        # with no known node counts any upgraded pool.
        "own_pool_touched": ctx.operations_known and (pool in ctx.upgraded_pools if pool else bool(ctx.upgraded_pools)),
    }


def _match_cgroup_runtime(symptom: dict, entry: int, ctx: Context) -> dict | None:
    """Entry 14: an OOM kill on a cgroup v2 pool with a runtime image below
    the catalogue's floor; several containers name entry 15 as a second cause."""
    reading = _oom_reading(symptom, ctx)
    if reading is None or reading["mode"] != CGROUP_V2_MODE or not reading["runtime"]:
        return None
    if ctx.operations_known and not reading["own_pool_touched"]:
        # The pool the OOM-killed replica sits on had no operation: the
        # runtime floor names nothing the upgrade did, and row 15 reads it.
        return None
    detail = f"cgroup v1 runtime on cgroup v2; {reading['count_text']}"
    if reading["count"] > 1:
        detail += "; " + OOM_SECOND_CAUSE_FORMAT.format(entry=ENTRY_OOM_GROUP, count=reading["count"])
    evidence = f"{reading['evidence']}; runtime image {reading['runtime']}"
    if not ctx.operations_known:
        return _classification(entry, MEDIUM, evidence, f"{detail}; {GATE_UNKNOWN_TEXT}")
    return _classification(entry, HIGH, evidence, detail)


def _match_oom_group(symptom: dict, entry: int, ctx: Context) -> dict | None:
    """Entry 15, which no read confirms: a multi-process container is not
    visible from a spec, so an OOM kill on cgroup v2 (or an unknown mode) is
    "14 or 15" under the lower number, medium; on cgroup v1 it is neither."""
    reading = _oom_reading(symptom, ctx)
    if reading is None:
        return None
    undecided = f"{OOM_UNDECIDED_ENTRIES[0]} or {OOM_UNDECIDED_ENTRIES[1]}"
    if reading["mode"] == CGROUP_V2_MODE:
        return _classification(OOM_UNDECIDED_ENTRIES[0], MEDIUM, reading["evidence"], f"{undecided} on cgroup v2; {reading['count_text']}")
    if reading["mode"] == CGROUP_V1_MODE:
        return _classification(None, MEDIUM, reading["evidence"], f"OOMKilled on cgroup v1: neither {undecided}")
    return _classification(OOM_UNDECIDED_ENTRIES[0], MEDIUM, reading["evidence"], f"{undecided}: cgroup mode unknown; {reading['count_text']}")


def _match_node_agent(symptom: dict, entry: int, ctx: Context) -> dict | None:
    """Entry 17: a node NotReady / NetworkUnavailable, high when its pool was
    upgraded in the window."""
    if symptom["category"] != CATEGORY_NODE:
        return None
    pool = symptom.get("pool") or ""
    touched = ctx.upgraded_pools.get(pool)
    if touched:
        symptom["operation"] = str(touched["operation"])
    transition = f" since {symptom['onset']}" if symptom.get("onset") else ""
    node_detail = f"pool {pool}" + ("" if ctx.operations_known else f"; {GATE_UNKNOWN_TEXT}")
    return _classification(entry, HIGH if touched else MEDIUM, f"node {symptom['name']} {symptom['reason']}{transition}" + (f" after UPGRADE_NODES on {pool} at {touched['start']}" if touched else ""), node_detail)


def _match_budget(symptom: dict, entry: int, ctx: Context) -> dict | None:
    """Entry 1: a budget allowing no disruption on an upgraded pool. Every
    touched pool counts, since the budget allowed nothing on a pool that was
    drained; the pool whose drain visibly stalled, if any, is named as detail."""
    if symptom["category"] != CATEGORY_PDB or not symptom.get("upgraded_pools"):
        return None
    touched = {pool: ctx.upgraded_pools[pool] for pool in symptom["upgraded_pools"]}
    # The hold is read from the operation's own metrics when it carries them;
    # the hour-per-node estimate from its duration is the fallback.
    held: dict[str, str] = {}
    for pool, info in touched.items():
        delay = info.get("pdb_delay_s")
        if delay is not None:
            if delay > 0:
                held[pool] = DRAIN_HELD_MEASURED_FORMAT.format(pool=pool, metric=OPERATION_METRIC_PDB_DELAY, seconds=delay)
        elif info["nodes"] > 0 and info["longest_s"] > info["nodes"] * DRAIN_HOLD_PER_NODE.total_seconds():
            held[pool] = DRAIN_HELD_ESTIMATED_FORMAT.format(pool=pool, minutes=info["longest_s"] // SECONDS_PER_MINUTE, nodes=info["nodes"])
    named = next(iter(held)) if held else next(iter(touched))
    info = touched[named]
    source = f"{OPERATION_METRIC_PDB_DELAY} {info['pdb_delay_s']} s" if info.get("pdb_delay_s") is not None else NO_PROGRESS_METRICS_TEXT
    detail = f"budget {symptom['name']}"
    if symptom.get("budget_downstream"):
        detail += ": " + BUDGET_DOWNSTREAM_DETAIL_FORMAT.format(spec=json.dumps(symptom.get("spec") or {}, sort_keys=True), not_ready="?" if symptom.get("not_ready") is None else symptom["not_ready"], expected="?" if symptom.get("expected_pods") is None else symptom["expected_pods"])
    detail += f"; {DRAIN_HELD_PREFIX}{', '.join(held.values())}" if held else ""
    return _classification(entry, HIGH, f"disruptionsAllowed=0 with pods on {', '.join(touched)}; UPGRADE_NODES {info['operation']} on {named} took {info['longest_s'] // SECONDS_PER_MINUTE} min over {info['nodes']} node(s); {source}", detail)


def _match_spread(symptom: dict, entry: int, ctx: Context) -> dict | None:
    """Entry 3: every replica unavailable during one pool operation; high
    when the pods' nodes were all in the operation's pool, medium when they
    were all in the drained zone."""
    if symptom["category"] != CATEGORY_UNAVAILABLE:
        return None
    count, pool = len(symptom.get("pods") or []), symptom.get("pool") or ""
    start, end = symptom.get("operation_span") or ("?", "?")
    if symptom.get("all_in_pool"):
        where, confidence, detail = f"all {count} pod(s) on pool {pool}", HIGH, SPREAD_POOL_DETAIL
    else:
        where, confidence, detail = f"all {count} pod(s) in zone {symptom.get('zone') or '?'} (pools {', '.join(sorted(symptom.get('pod_pools') or [])) or '?'})", MEDIUM, SPREAD_ZONE_DETAIL
    evidence = f"{symptom['reason']} since {symptom.get('onset') or '?'} during UPGRADE_NODES {symptom.get('operation') or '?'} on {pool} ({start} to {end}); {where}"
    return _classification(entry, confidence, evidence, detail)


def _match_window(symptom: dict, entry: int, ctx: Context) -> dict | None:
    """Entry 5: a pool more than one minor behind its control plane, standing
    `SKEW_STANDING` or longer since the run that first recorded it; medium,
    from the cluster describe and the ledger's record."""
    if symptom["category"] != CATEGORY_VERSION_SKEW:
        return None
    return _classification(entry, MEDIUM, symptom.get("message") or "", symptom.get("reference") or "")


ENTRY_MATCHERS: dict[int, Callable[[dict, int, Context], dict | None]] = {
    ENTRY_WEBHOOK: _match_signature,
    ENTRY_REMOVED_API: _match_removed_api,
    ENTRY_IN_TREE_VOLUME: _match_signature,
    ENTRY_GPU: _match_signature,
    ENTRY_CGROUP_V2: _match_cgroup_runtime,
    ENTRY_OOM_GROUP: _match_oom_group,
    ENTRY_REGISTRY: _match_signature,
    ENTRY_NODE_AGENT: _match_node_agent,
    ENTRY_SPREAD: _match_spread,
    ENTRY_CAPACITY: _match_signature,
    ENTRY_BUDGET: _match_budget,
    ENTRY_WINDOW: _match_window,
    ENTRY_NODE_LABEL: _match_signature,
}


def classify_symptom(symptom: dict, ctx: Context) -> list[dict]:
    """Exactly one classification: the rows of `ENTRY_ORDER` are tried in
    order and the first that holds wins. A row that read the symptom and
    found only an unclassified verdict (an OOM kill on cgroup v1) is kept as
    the fallback; a symptom no row holds for is reported unclassified."""
    fallback = None
    for entry in ENTRY_ORDER:
        verdict = ENTRY_MATCHERS[entry](symptom, entry, ctx)
        if verdict is None:
            continue
        if verdict["entry"] is None:
            fallback = fallback or verdict
            continue
        return [verdict]
    return [fallback or _classification(None, MEDIUM, f"{symptom.get('reason') or ''} {symptom.get('message') or ''}".strip())]

def compose_pod_evidence(core: str, count: int, total: int, example: str, notes: list[str]) -> str:
    """A pod row's evidence from its parts, cut once: the notes (pre-existing
    pods, the owner's proof of age, a rollout) always fit, the core message
    gives way."""
    head = POD_EVIDENCE_FORMAT.format(count=count, total=max(total, count), evidence="", example="").rsplit(": ", 1)[0] + ": "
    tail = POD_EVIDENCE_EXAMPLE_FORMAT.format(example=example) + "".join(notes)
    room = max(MESSAGE_EXCERPT_CHARS - len(head) - len(tail), 0)
    return head + core[:room] + tail


def symptom_key(symptom: dict) -> str:
    """The baseline's name for a symptom: owner, category and reason, no
    tenant text; an operation-bound symptom (a budget hold, a node not back)
    also names its operation, so this upgrade's hold is never "already
    recorded" from the last one."""
    parts = [symptom["object"], symptom["category"], symptom.get("reason") or ""]
    if symptom.get("operation"):
        parts.append(OPERATION_KEY_PREFIX + symptom["operation"])
    return BASELINE_SEPARATOR.join(parts)


def mark_since(symptoms: list[dict], baseline: list[str] | None, first_operation: datetime | None = None) -> None:
    """Against the stored symptom set and each symptom's onset. `baseline`
    None means no previous full run: everything is first seen and graded by
    onset alone. A symptom predates the upgrade when its onset is before the
    window's first operation, or the stored set already has it; with no
    readable onset the stored set decides."""
    previous = set(baseline or [])
    for symptom in symptoms:
        recorded = baseline is not None and symptom_key(symptom) in previous
        pod_onsets = {pod: parse_ts(ts) for pod, ts in (symptom.get("pod_onsets") or {}).items()}
        if pod_onsets and first_operation:
            # Graded per pod: a row with any pod whose onset is inside the
            # window is new (the displaced replicas), whatever the stored
            # set says of the row; the older pods are noted as pre-existing.
            new_pods = sorted(pod for pod, onset in pod_onsets.items() if onset and onset >= first_operation)
            old_pods = sorted(pod for pod, onset in pod_onsets.items() if onset and onset < first_operation)
            symptom["new_pods"], symptom["pre_existing_pods"] = new_pods, old_pods
            notes: list[str] = []
            # The example names a displaced replica when there is one, never
            # the pre-existing pod the row also reports.
            if new_pods:
                symptom["example_pod"] = new_pods[0]
            recreated = set(symptom.get("recreated_pods") or [])
            # A recreation carries a container-level failure over (crash
            # loop, OOM kill, image pull); a Pending replica created inside
            # the window is the one the drain displaced and is simply new.
            container_level = symptom["category"] == CATEGORY_NOT_READY
            ambiguous = bool(new_pods) and container_level and set(new_pods) <= recreated
            proof = symptom.get("age_proof") or {}
            proof_at = parse_ts(proof.get("since"))
            proven_old = bool(ambiguous and proof_at and proof_at < first_operation)
            rollout_at = parse_ts(symptom.get("rollout_at"))
            if ambiguous and rollout_at and rollout_at >= first_operation:
                # Evidence that a rollout happened during the window; it decides nothing.
                notes.append(ROLLOUT_IN_WINDOW_FORMAT.format(created=symptom["rollout_at"]))
            if new_pods and not ambiguous:
                symptom["since"] = SINCE_FIRST_SEEN if baseline is None else SINCE_NEW
                symptom["predates_upgrade"] = False
            elif ambiguous and old_pods:
                # A sibling replica whose own onset predates the window is the
                # proof of age: the recreated replicas carried the failure over.
                symptom["since"] = SINCE_FIRST_SEEN if baseline is None else (SINCE_BEFORE if recorded else SINCE_NEW)
                symptom["predates_upgrade"] = True
                notes.append(NEW_PODS_FORMAT.format(count=len(new_pods), example=new_pods[0]))
            elif proven_old:
                # The owner proves the failure predates the window.
                symptom["since"] = SINCE_FIRST_SEEN if baseline is None else (SINCE_BEFORE if recorded else SINCE_NEW)
                symptom["predates_upgrade"] = True
                notes.append(AGE_PROOF_FORMAT.format(proof=proof["what"], since=proof["since"]))
            elif ambiguous and baseline is None:
                # No proof of age on a first run: medium, a Warning, until a
                # later full run settles it through the stored set.
                symptom["since"] = SINCE_FIRST_SEEN
                symptom["predates_upgrade"] = False
                symptom["recreated_only"] = True
                for c in symptom["classifications"]:
                    if c["confidence"] == HIGH:
                        c["confidence"] = MEDIUM
                        c["detail"] = (c["detail"] + "; " if c.get("detail") else "") + RECREATED_DETAIL
            elif ambiguous:
                symptom["since"] = SINCE_BEFORE if recorded else SINCE_NEW
                symptom["predates_upgrade"] = recorded
            else:
                symptom["since"] = SINCE_FIRST_SEEN if baseline is None else (SINCE_BEFORE if recorded else SINCE_NEW)
                symptom["predates_upgrade"] = recorded or bool(old_pods)
            if old_pods:
                earliest = min(pod_onsets[pod] for pod in old_pods)
                notes.append(PRE_EXISTING_PODS_FORMAT.format(count=len(old_pods), earliest=fmt_ts(earliest), example=old_pods[0]))
            for c in symptom["classifications"]:
                c["evidence"] = compose_pod_evidence(c.get("evidence_core", c["evidence"]), symptom["pod_count"], symptom["pod_total"], symptom["example_pod"], notes)
            continue
        symptom["since"] = SINCE_FIRST_SEEN if baseline is None else (SINCE_BEFORE if recorded else SINCE_NEW)
        onset = parse_ts(symptom.get("onset"))
        before_operation = bool(first_operation and onset and onset < first_operation)
        # A version skew is graded by persistence, never by onset: a recorded
        # one is the same finding on a week with an operation and without.
        symptom["predates_upgrade"] = symptom["category"] != CATEGORY_VERSION_SKEW and (recorded or before_operation)


def _unavailable_workloads(workloads: list[dict]) -> dict[str, tuple[datetime | None, str, int]]:
    """Deployments whose `Available` condition is False, with the transition
    and message, and StatefulSets with no ready replica (no condition and no
    date: the pods date those), by object, each with its replica count."""
    out: dict[str, tuple[datetime | None, str, int]] = {}
    for workload in workloads:
        kind, meta, status = workload.get("kind") or "", workload.get("metadata") or {}, workload.get("status") or {}
        if kind not in SPREAD_KINDS:
            continue
        obj = _object_ref(meta.get("namespace", ""), kind, meta.get("name", ""))
        replicas = (workload.get("spec") or {}).get("replicas")
        replicas = int(replicas if replicas is not None else status.get("replicas") or 0)
        if kind == "Deployment":
            cond = _condition(workload, "Available")
            if cond.get("status") == "False":
                out[obj] = (parse_ts(cond.get("lastTransitionTime")), (cond.get("message") or cond.get("reason") or "")[:MESSAGE_EXCERPT_CHARS], replicas)
        elif (status.get("replicas") or 0) > 0 and not (status.get("readyReplicas") or 0):
            out[obj] = (None, f"readyReplicas 0 of {status.get('replicas')}", replicas)
    return out


def unavailable_symptoms(workloads: list[dict], pods: list[dict], nodes: list[dict], operations: list[dict], resolver: Resolver) -> list[dict]:
    """Entry 3's after-signal: a Deployment or StatefulSet whose availability
    fell to zero inside one pool operation's span, every pod of which sits on
    that pool or in the zone it drained. Keyed by the operation, like a
    budget hold, so the next upgrade's outage is a new finding."""
    spans: list[tuple[str, str, datetime, datetime]] = []
    for op in operations:
        target = parse_target_link(op.get("targetLink", ""))
        start, end = parse_ts(op.get("startTime")), parse_ts(op.get("endTime"))
        if op.get("operationType") == OP_UPGRADE_NODES and target and target[2] and start and end:
            spans.append((target[2], op.get("name") or "", start, end))
    if not spans:
        return []
    node_pool, node_zone = {}, {}
    for node in nodes:
        meta = node.get("metadata") or {}
        labels = meta.get("labels") or {}
        node_pool[meta.get("name")] = labels.get(NODEPOOL_LABEL, "")
        node_zone[meta.get("name")] = labels.get(ZONE_LABEL, "")
    pool_zones: dict[str, set[str]] = {}
    for name, pool in node_pool.items():
        if pool and node_zone.get(name):
            pool_zones.setdefault(pool, set()).add(node_zone[name])
    owned: dict[str, list[dict]] = {}
    for pod in pods:
        meta = pod.get("metadata") or {}
        kind, name = resolver.resolve(meta.get("namespace", ""), "Pod", meta.get("name", ""))
        owned.setdefault(_object_ref(meta.get("namespace", ""), kind, name), []).append(pod)
    rows = []
    for obj, (onset, message, replicas) in _unavailable_workloads(workloads).items():
        pods_of = owned.get(obj) or []
        if not pods_of or replicas < SPREAD_MIN_REPLICAS:
            # The catalogue's floor, on the spec's count alone: one replica
            # lost to a drain is entry 2 (its Pending replacement) or 1 (the
            # budget), never 3, and the evicted pod still listed beside the
            # replacement (Terminating, or Failed and not yet collected) does
            # not make it two.
            continue
        if onset is None:
            # A StatefulSet carries no date: the earliest pod the drain recreated.
            created = [parse_ts((p.get("metadata") or {}).get("creationTimestamp")) for p in pods_of]
            inside = [c for c in created if c and any(start <= c <= end for _, _, start, end in spans)]
            onset = min(inside) if inside else None
        if onset is None:
            continue
        hit = next(((pool, name, start, end) for pool, name, start, end in spans if start <= onset <= end), None)
        if hit is None:
            continue
        pool, op_name, start, end = hit
        pod_nodes = {(p.get("metadata") or {}).get("name", ""): (p.get("spec") or {}).get("nodeName") for p in pods_of}
        # A replacement still Pending sits nowhere yet; the drain running
        # when it was created places it on the pool it was evicted from.
        span_map: dict[str, list[tuple[datetime, datetime]]] = {}
        for span_pool, _, s0, e0 in spans:
            span_map.setdefault(span_pool, []).append((s0, e0))
        pools, zones = set(), set()
        for p in pods_of:
            node = (p.get("spec") or {}).get("nodeName")
            if node:
                pools.add(node_pool.get(node, ""))
                zones.add(node_zone.get(node, ""))
                continue
            displaced = pool if pool in _drain_pools_at_creation(p, span_map) else ""
            pools.add(displaced)
            zones |= pool_zones.get(displaced, set()) if displaced and len(pool_zones.get(displaced, set())) == 1 else {""}
        all_in_pool = pools == {pool}
        all_in_zone = len(zones) == 1 and "" not in zones and zones <= pool_zones.get(pool, set())
        if not (all_in_pool or all_in_zone):
            continue
        namespace, kind, name = obj.split("/", 2)
        rows.append({
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "object": obj,
            "system": is_system_namespace(namespace),
            "category": CATEGORY_UNAVAILABLE,
            "reason": UNAVAILABLE_REASON,
            "message": message,
            "onset": fmt_ts(onset),
            "operation": op_name,
            "operation_span": (fmt_ts(start), fmt_ts(end)),
            "pool": pool,
            "pods": sorted(pod_nodes),
            "pod_nodes": pod_nodes,
            "pod_pools": sorted(p for p in pools if p),
            "zone": next(iter(zones)) if len(zones) == 1 else "",
            "all_in_pool": all_in_pool,
        })
    return rows


def _last_closing(window: dict, now: datetime) -> tuple[bool, datetime | None]:
    """Whether the maintenance window is open now and, when it is not, when
    its most recent occurrence closed; (False, None) for a rule this does
    not parse, or none at all."""
    daily, recurring = window.get("dailyMaintenanceWindow"), window.get("recurringWindow")
    try:
        if daily:
            start_t = datetime.strptime(daily.get("startTime") or "00:00", DAILY_WINDOW_TIME_FORMAT).time().replace(tzinfo=timezone.utc)
            length, days = _parse_iso_duration(daily.get("duration") or "") or DEFAULT_DAILY_WINDOW, []
        elif recurring:
            _, freq, start, length, days = recurring_window_rule(recurring)
            if start is None or freq is None:
                return False, None
            start_t = start.timetz()
        else:
            return False, None
    except ValueError:
        return False, None
    open_now = False
    for offset in range(NEXT_WINDOW_SEARCH_DAYS + 1):
        candidate = datetime.combine((now - timedelta(days=offset)).date(), start_t)
        if candidate > now or (days and candidate.weekday() not in days):
            continue
        if now < candidate + length:
            # Open now; the previous occurrence's close is still the reference.
            open_now = True
            continue
        return open_now, candidate + length
    return open_now, None


def version_skew(cluster: dict, now: datetime, first_seen: dict[str, str] | None = None) -> dict:
    """Entry 5's after-signal from the cluster describe: every pool
    `SKEW_MINORS` or more minors behind its control plane, each with its age
    from the ledger's first-seen record (`first_seen`, key to timestamp; a
    skew not recorded yet is first seen this run). A skew stands once it has
    been recorded `SKEW_STANDING` or longer; while a maintenance window is
    open or an exclusion active the roll may still complete, which is Info,
    and a cluster with no maintenance policy is never graded."""
    versions = versions_of(cluster)
    master = parse_version(versions["control_plane"] or "")
    skews: list[tuple[str, str]] = []
    for pool, version in sorted(versions["node_pools"].items()):
        parsed = parse_version(version or "")
        if master and parsed and master[0] == parsed[0] and master[1] - parsed[1] >= SKEW_MINORS:
            skews.append((pool, SKEW_CONTROL_PLANE_FORMAT.format(pool=pool, pool_version=version, master=versions["control_plane"], minors=master[1] - parsed[1])))
    policy = ((cluster.get("maintenancePolicy") or {}).get("window") or {})
    no_policy = not policy
    open_now, _ = _last_closing(policy, now)
    state = WINDOW_OPEN_STATE if open_now else ""
    for name, exclusion in sorted((policy.get("maintenanceExclusions") or {}).items()):
        start, end = parse_ts(exclusion.get("startTime")), parse_ts(exclusion.get("endTime"))
        if start and end and start <= now <= end:
            state = state or EXCLUSION_ACTIVE_FORMAT.format(name=name, scope=((exclusion.get("maintenanceExclusionOptions") or {}).get("scope")) or DEFAULT_EXCLUSION_SCOPE, end=fmt_ts(end))
    keys = [BASELINE_SEPARATOR.join((_object_ref("", "NodePool", pool), CATEGORY_VERSION_SKEW, SKEW_REASON)) for pool, _ in skews]
    recorded = {key: parse_ts((first_seen or {}).get(key)) for key in keys}
    standing = {key: bool(since) and now - since >= SKEW_STANDING for key, since in recorded.items()}
    ages = {key: SKEW_AGE_FORMAT.format(days=(now - since).days, since=fmt_ts(since)) if since else SKEW_AGE_FIRST_FORMAT for key, since in recorded.items()}
    return {
        "skews": skews,
        "state": state,
        "no_policy": no_policy,
        "keys": keys,
        "standing": standing,
        "ages": ages,
        "overdue": bool(skews) and not state and not no_policy and any(standing.values()),
    }


def skew_keys(cluster: dict, now: datetime | None) -> list[str]:
    """The stored symptom set's names for every skew that stands, graded or
    not, so a later run can grade one that has stood long enough."""
    return version_skew(cluster, now)["keys"] if now is not None else []


def version_skew_symptoms(cluster: dict, now: datetime | None, first_seen: dict[str, str] | None = None) -> list[dict]:
    """One row per skewed pool once its skew has stood `SKEW_STANDING` or
    longer since the run that first recorded it; nothing inside that, while
    a window is open or an exclusion active, on a cluster with no policy, or
    without a clock."""
    if now is None:
        return []
    reading = version_skew(cluster, now, first_seen)
    if not reading["skews"] or reading["no_policy"] or reading["state"]:
        # No policy: no window to be too short, so row 5 never holds; the
        # review lists the skew (`SKEW_NO_POLICY_TEXT`) and the stored set
        # carries it. An open window or an active exclusion: the roll may
        # still complete (`SKEW_OPEN_TEXT`).
        return []
    rows = []
    for (pool, text), key in zip(reading["skews"], reading["keys"]):
        if not reading["standing"][key]:
            continue
        rows.append({
            "kind": "NodePool",
            "namespace": "",
            "name": pool,
            "object": _object_ref("", "NodePool", pool),
            "system": True,
            "category": CATEGORY_VERSION_SKEW,
            "reason": SKEW_REASON,
            "message": text,
            "onset": None,
            "reference": reading["ages"][key],
        })
    return rows


def _api_set_failures(pods: list[dict], owners: list[dict], resolver: Resolver, now: datetime | None = None) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """What the API server itself says is failing: a pod whose phase, or a
    container state the kubelet wrote, shows it, and an owner whose
    controller set a failure condition -- by pod and by top owner. These are
    the fields that let text a namespace user wrote grade high."""
    failing_pods: dict[tuple[str, str], str] = {}
    failing_objects: dict[str, str] = {}
    for pod in pods:
        meta, status = pod.get("metadata") or {}, pod.get("status") or {}
        if _pod_is_gated(pod) or meta.get("deletionTimestamp") or _pod_is_starting(pod, now):
            # Waiting for admission, being deleted, or still starting inside
            # the grace: not a failure anything agrees with, so event text on
            # such a pod keeps its own grade.
            continue
        what = ""
        for cs in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
            state = cs.get("state") or {}
            waiting, terminated = state.get("waiting") or {}, state.get("terminated") or {}
            if waiting.get("reason") in CONTAINER_FAILURE_REASONS:
                what = f"container {cs.get('name')} state {waiting['reason']}"
                break
            if terminated and terminated.get("exitCode") not in (None, 0):
                what = f"container {cs.get('name')} state terminated exit {terminated['exitCode']}"
                break
        if not what and status.get("phase") in (PHASE_PENDING, PHASE_FAILED):
            what = f"phase {status['phase']}"
        if what:
            namespace, name = meta.get("namespace", ""), meta.get("name", "")
            failing_pods[(namespace, name)] = what
            kind, owner = resolver.resolve(namespace, "Pod", name)
            failing_objects.setdefault(_object_ref(namespace, kind, owner), f"pod {name} {what}")
    for owner in owners:
        meta, kind = owner.get("metadata") or {}, owner.get("kind") or ""
        for cond in (owner.get("status") or {}).get("conditions") or []:
            if (kind, cond.get("type"), cond.get("status")) in CONTROLLER_FAILURE_CONDITIONS:
                namespace, name = meta.get("namespace", ""), meta.get("name", "")
                what = f"{kind} condition {cond.get('type')}={cond.get('status')}"
                top_kind, top_name = resolver.resolve(namespace, kind, name)
                failing_objects.setdefault(_object_ref(namespace, top_kind, top_name), what)
                failing_objects.setdefault(_object_ref(namespace, kind, name), what)
    return failing_pods, failing_objects


def collect_symptoms(cluster: dict, reads: dict[str, list], operations: list[dict], window_start: datetime, *, operations_known: bool = True, now: datetime | None = None, starting: list[str] | None = None, baseline: list[str] | None = None, gated: list[str] | None = None, skew_first_seen: dict[str, str] | None = None, terminating: list[str] | None = None) -> list[dict]:
    """Every symptom in the read, each with its classifications, user
    namespaces first."""
    nodes = reads.get("nodes") or []
    upgraded = upgraded_pools_of(operations, nodes)
    node_pool = {(n.get("metadata") or {}).get("name"): ((n.get("metadata") or {}).get("labels") or {}).get(NODEPOOL_LABEL, "") for n in nodes}
    untouched_images = set()
    for pod in reads.get("pods") or []:
        pool = node_pool.get((pod.get("spec") or {}).get("nodeName"), "")
        if pool and pool not in upgraded and (pod.get("status") or {}).get("phase") == PHASE_RUNNING and _condition(pod, "Ready").get("status") == "True":
            untouched_images.update(c.get("image") or "" for c in (pod.get("spec") or {}).get("containers") or [])
    resolver = Resolver(reads.get("pods") or [], reads.get("owners") or [], reads.get("workloads") or [])
    failing_pods, failing_objects = _api_set_failures(reads.get("pods") or [], reads.get("owners") or [], resolver, now=now)
    ctx = Context(
        cgroup_modes=pool_cgroup_modes(cluster),
        node_pool=node_pool,
        upgraded_pools=upgraded,
        master_upgraded=any(op.get("operationType") == OP_UPGRADE_MASTER for op in operations),
        images_on_untouched_pools=frozenset(untouched_images),
        operations_known=operations_known,
        label_pools=pool_labels(cluster, nodes),
        nodes_view=None if reads.get("nodes") is None else tuple(((n.get("metadata") or {}).get("name") or "", (n.get("metadata") or {}).get("labels") or {}) for n in nodes),
        pool_nodes=None if reads.get("nodes") is None else pool_nodes_of(nodes),
        failing_pods=failing_pods,
        failing_objects=failing_objects,
        pool_versions=versions_of(cluster)["node_pools"],
        pool_records=pool_records_of(cluster),
        control_plane=versions_of(cluster)["control_plane"] or "",
    )
    node_pool_of = {(n.get("metadata") or {}).get("name"): ((n.get("metadata") or {}).get("labels") or {}).get(NODEPOOL_LABEL, "") for n in nodes}
    pods = pod_symptoms(reads.get("pods") or [], resolver, window_start, owner_age_proofs(reads.get("workloads") or []), pool_operation_windows(operations), node_pool_of, owner_rollouts(reads.get("owners") or []), now, starting, gated, terminating)
    pod_objects = {s["object"] for s in pods}
    # An event on a pod that is gone carries no pool; its owner's template
    # nodeSelector is the pool evidence it can still have.
    specs_by_owner = pod_specs_by_owner(reads.get("workloads") or [], reads.get("pods") or [], resolver)
    owner_selectors = {obj["object"]: (spec.get("nodeSelector") or {}) for obj, spec, _ in specs_by_owner.values()}
    owner_pins = {obj["object"]: pod_pins(spec) for obj, spec, _ in specs_by_owner.values()}
    owner_specs = {obj["object"]: spec for obj, spec, _ in specs_by_owner.values()}
    # A FailedScheduling or BackOff event on a workload the pod list already
    # reports as Pending or crash-looping says the same thing twice.
    oom_nodes = {node for s in pods for node in (s.get("oom_nodes") or {}).values() if node}
    events = [
        e for e in event_symptoms(reads.get("events") or [], window_start, resolver)
        if not (e["reason"] in EVENT_REASONS_IMPLIED_BY_POD and e["object"] in pod_objects)
        and not (e["reason"] == OOM_NODE_EVENT_REASON and e["kind"] == "Node" and e["name"] in oom_nodes)
    ]
    for e in events:
        e.setdefault("node_selector", owner_selectors.get(e["object"], {}))
        e.setdefault("node_pins", owner_pins.get(e["object"], []))
        template = owner_specs.get(e["object"]) or {}
        e.setdefault("node_affinity", required_node_terms(template))
        e.setdefault("tolerations", pod_tolerations(template))
        e.setdefault("extended_resources", pod_extended_resources(template))
    symptoms = pods + node_symptoms(nodes) + events + pdb_symptoms(reads.get("pdbs") or [], reads.get("pods") or [], nodes, upgraded, pool_operation_windows(operations), owner_pins, resolver, ctx.label_pools)
    symptoms += unavailable_symptoms(reads.get("workloads") or [], reads.get("pods") or [], nodes, operations, resolver) + version_skew_symptoms(cluster, now, skew_first_seen)
    for symptom in symptoms:
        symptom["classifications"] = classify_symptom(symptom, ctx)
        if symptom.get("pod_count"):
            for c in symptom["classifications"]:
                c["evidence_core"] = c["evidence"]
                c["evidence"] = compose_pod_evidence(c["evidence_core"], symptom["pod_count"], symptom["pod_total"], symptom["example_pod"], [])
    symptoms.sort(key=lambda s: (s["system"], s["namespace"], s["kind"], s["name"], s.get("reason") or ""))
    return symptoms


# --------------------------------------------------------------------------- #
# Sections C and D.
# --------------------------------------------------------------------------- #


def mitigation_lines(symptom: dict, classification: dict) -> dict:
    entry = classification["entry"]
    row = MITIGATIONS[entry]
    subject = symptom["object"] + (f" ({classification['detail']})" if classification.get("detail") else "")
    before, mitigate_before, mitigate_after = row["before"], row["mitigate_before"], row["mitigate_after"]
    if entry == ENTRY_BUDGET and symptom.get("budget_downstream"):
        # The catalogue's row describes a budget whose spec forbids disruption;
        # this one allows it and is at zero behind its covered pods.
        before, mitigate_before, mitigate_after = BUDGET_DOWNSTREAM_BEFORE, BUDGET_DOWNSTREAM_MITIGATE_BEFORE, BUDGET_DOWNSTREAM_MITIGATE_AFTER
    return {
        "entry": entry,
        "title": row["title"],
        "object": symptom["object"],
        "before_signal": f"For {subject}: {before}",
        "read_today": row["read_today"],
        "mitigate_before": mitigate_before,
        "mitigate_after": mitigate_after,
    }


def guard_id(cluster: str, entry: int | str | None, obj: str, kind: str = GUARD_KIND_FAILURE) -> str:
    return f"{cluster}#{kind}#{UNCLASSIFIED if entry is None else entry}#{obj}"


def _guard(key: str, kind: str, entry: int | str | None, title: str, obj: str, confidence: str, evidence: str, seen_at: str, reads: tuple[str, ...], check: str | None = None) -> dict:
    guard = {
        "id": guard_id(key, entry, obj, kind),
        "kind": kind,
        "reads": list(reads),
        "cluster": key,
        "entry": entry,
        "title": title,
        "object": obj,
        "confidence": confidence,
        "evidence": evidence,
        "first_seen": seen_at,
        "last_seen": seen_at,
        "reviews": 1,
    }
    if check:
        guard["check"] = check
    return guard


def _guard_check(guard: dict) -> str:
    """The check a guard's finding was last filed under; for a guard written
    before checks were recorded, the nearest reading of its grade."""
    if guard.get("check"):
        return str(guard["check"])
    if guard.get("entry") is None:
        return CHECK_SYMPTOM_UNCLASSIFIED
    return CHECK_BROKE_WORKLOAD if guard.get("confidence") == HIGH else CHECK_SYMPTOM_TENTATIVE


def _guard_entry_text(guard: dict) -> str:
    return UNCLASSIFIED if guard.get("entry") is None else str(guard["entry"])


def _symptom_group_check(symptoms: list[dict], upgrade_context: bool) -> tuple[str, str, bool]:
    """The check id, severity and predates flag an object's symptoms file
    under: the one rule `triage` applies per incident and `guards_for`
    records per guard, so a held guard's candidate keeps its finding's
    identity. `upgrade_context` is whether the review saw an upgrade (the
    status or an operation), without which "predates" means nothing. The
    flag is the group's verdict: once a symptom that does not predate wins
    the Error, the incident does not predate, whatever its other rows say
    (each row's `since` still says which predates)."""
    severity, predates = SEVERITY_WARNING, False
    for symptom in symptoms:
        symptom_predates = bool(symptom.get("predates_upgrade")) and upgrade_context
        predates = predates or symptom_predates
        if _symptom_severity(symptom) == SEVERITY_ERROR and not symptom_predates and not symptom.get("recreated_only"):
            severity = SEVERITY_ERROR
    if severity == SEVERITY_ERROR:
        return (CHECK_NODE_BROKEN if any(s["category"] == CATEGORY_NODE for s in symptoms) else CHECK_BROKE_WORKLOAD), severity, False
    if predates:
        return CHECK_SYMPTOM_PREDATES, severity, predates
    if all(c["entry"] is None for s in symptoms for c in s["classifications"]):
        return CHECK_SYMPTOM_UNCLASSIFIED, severity, predates
    return CHECK_SYMPTOM_TENTATIVE, severity, predates


def guards_for(key: str, symptoms: list[dict], seen_at: str, upgrade_context: bool = False) -> list[dict]:
    """One `failure` guard per (entry, object), the unclassified symptom's
    among them with `unclassified` in the entry slot: the guard is what makes
    the next run re-check the cluster for it and re-emit its candidate while
    it stands. Each records its source and the check its finding filed under."""
    out: dict[str, dict] = {}
    by_object: dict[str, list[dict]] = {}
    for symptom in symptoms:
        by_object.setdefault(symptom["object"], []).append(symptom)
    checks = {obj: _symptom_group_check(group, upgrade_context)[0] for obj, group in by_object.items()}
    for symptom in symptoms:
        for c in symptom["classifications"]:
            reads = FAILURE_GUARD_READS + RECHECK_SPREAD_READS if c["entry"] == ENTRY_SPREAD else FAILURE_GUARD_READS
            guard = _guard(key, GUARD_KIND_FAILURE, c["entry"], c["title"], symptom["object"], c["confidence"], c["evidence"], seen_at, reads, check=checks[symptom["object"]])
            guard["source"] = symptom["category"]
            existing = out.get(guard["id"])
            if existing is None:
                out[guard["id"]] = guard
            elif existing["source"] == GUARD_SOURCE_EVENT and symptom["category"] != GUARD_SOURCE_EVENT:
                # A pod or node showed the same finding: the guard is not event-only.
                existing["source"] = symptom["category"]
    return list(out.values())


def operation_guards_for(key: str, operations: list[dict], seen_at: str) -> list[dict]:
    """One `failure` guard per failed upgrade operation (`ENTRY_OPERATION` in
    the entry slot, `Operation/<name>` as the object, the listing as its
    source), under `upgrade-operation-failed`, with the operation's start so
    the next run's listing reaches it. It needs no read: the listing every
    run makes re-checks it (`recheck_operation_guards`)."""
    out = []
    for op in operations:
        if not op.get("name") or not _operation_failed(op):
            continue
        guard = _guard(key, GUARD_KIND_FAILURE, ENTRY_OPERATION, OPERATION_GUARD_TITLE, f"{OPERATION_OBJECT_KIND}/{op['name']}", HIGH, f"{op['type']} on {op['target']} ended {op['status'] or '?'}: {op['error'] or 'no error text'}", seen_at, (), check=CHECK_OPERATION_FAILED)
        guard["source"] = OPERATIONS_READ
        guard[OPERATION_GUARD_START_KEY] = op.get("start")
        guard[OPERATION_GUARD_TYPE_KEY] = op.get("type")
        guard[OPERATION_GUARD_TARGET_KEY] = op.get("target")
        out.append(guard)
    return out


def risk_guards_for(key: str, shapes: list[dict], seen_at: str) -> list[dict]:
    out: dict[str, dict] = {}
    for shape in shapes:
        guard = _guard(key, GUARD_KIND_RISK, shape["entry"], shape["title"], shape["object"], shape["confidence"], shape["evidence"], seen_at, SHAPE_READS_BY_ENTRY.get(shape["entry"], SPEC_SHAPE_READS))
        out.setdefault(guard["id"], guard)
    return list(out.values())


def merge_guards(existing: dict, fresh: list[dict], reviewed: set[str], seen_at: str, answered: dict[str, set[str]] | None = None, removed: set[str] | None = None) -> dict:
    """Keep what was there, refresh what is seen again, drop what a reviewed
    cluster no longer shows -- but only when every read that could show it
    answered (`answered` is per cluster; absent means all did). A cluster
    this run did not review keeps its guards; a cluster its project no longer
    lists (`removed`) loses them."""
    by_id = {g["id"]: {"kind": GUARD_KIND_FAILURE, **g} for g in existing.get("guards") or [] if isinstance(g, dict) and g.get("id")}
    fresh_ids = {g["id"] for g in fresh}
    merged = []
    for gid, guard in by_id.items():
        cluster = guard.get("cluster")
        if cluster in (removed or set()):
            continue
        if cluster in reviewed and gid not in fresh_ids and guard.get("entry") != ENTRY_OPERATION:
            # An operation guard is decided by the listing, not by a
            # review's reads (`recheck_operation_guards`).
            needed = set(guard.get("reads") or FAILURE_GUARD_READS)
            if answered is None or needed <= answered.get(cluster, set()):
                continue
        merged.append(guard)
    for guard in fresh:
        old = by_id.get(guard["id"])
        if old:
            old.update(last_seen=seen_at, evidence=guard["evidence"], confidence=guard["confidence"], reviews=int(old.get("reviews") or 1) + 1)
            if guard.get("source"):
                old["source"] = guard["source"]
            if guard.get("check"):
                # The guard follows the check its finding was filed under this
                # run: one finding per object, so a re-grade moves the identity
                # and the old id is resolved by the absence of a candidate.
                old["check"] = guard["check"]
        else:
            merged.append(guard)
    # One object, one check: every failure guard of an object a fresh guard
    # re-filed this run takes that guard's check, one a keep rule re-submitted
    # or a failed read held included, so the held path cannot file a second
    # identity beside the one the re-grade moved. The review's guards come
    # first in `fresh` and agree per object (`_symptom_group_check`).
    object_check: dict[tuple[str, str], str] = {}
    for guard in fresh:
        if guard.get("kind", GUARD_KIND_FAILURE) == GUARD_KIND_FAILURE and guard.get("check") and guard.get("entry") != ENTRY_OPERATION:
            object_check.setdefault((str(guard.get("cluster")), str(guard.get("object"))), guard["check"])
    for guard in merged:
        if guard.get("kind", GUARD_KIND_FAILURE) != GUARD_KIND_FAILURE or guard.get("entry") == ENTRY_OPERATION:
            continue
        check = object_check.get((str(guard.get("cluster")), str(guard.get("object"))))
        if check:
            guard["check"] = check
    merged.sort(key=_guard_order)
    return {"version": GUARDS_VERSION, "updated_at": seen_at, "guards": merged}


def _guard_order(guard: dict) -> tuple:
    """Numbered entries first, in order; then the named one (`operation`);
    then the unclassified."""
    entry = guard.get("entry")
    rank = 0 if isinstance(entry, int) else 2 if entry is None else 1
    return (guard["cluster"], rank, entry if isinstance(entry, int) else 0, str(entry or ""), guard["object"])


# --------------------------------------------------------------------------- #
# Shapes: the catalogue's before-signals read statically. Risks, not
# incidents; each is keyed by owner like a symptom and writes a `risk` guard.
# --------------------------------------------------------------------------- #


def _shape(obj: dict, entry: int, confidence: str, evidence: str, detail: str = "") -> dict:
    return {
        "reads": list(SHAPE_READS_BY_ENTRY.get(entry, SPEC_SHAPE_READS)),
        "kind": obj["kind"],
        "namespace": obj["namespace"],
        "name": obj["name"],
        "object": obj["object"],
        "system": obj["system"],
        "entry": entry,
        "title": MITIGATIONS[entry]["title"],
        "confidence": confidence,
        "evidence": evidence[:MESSAGE_EXCERPT_CHARS],
        "detail": detail,
    }


def _object_of(namespace: str, kind: str, name: str) -> dict:
    return {"kind": kind, "namespace": namespace, "name": name, "object": _object_ref(namespace, kind, name), "system": is_system_namespace(namespace) if namespace else True}


def _pod_template_spec(workload: dict) -> dict | None:
    spec = workload.get("spec") or {}
    if workload.get("kind") == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
    return (spec.get("template") or {}).get("spec")


def _template_labels(workload: dict) -> dict:
    spec = workload.get("spec") or {}
    return ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}


def pod_specs_by_owner(workloads: list[dict], pods: list[dict], resolver: Resolver) -> dict[str, tuple[dict, dict, dict]]:
    """Every workload's pod template, plus the spec of any pod whose owner
    the workload list does not cover (bare pods, Jobs), keyed by owner."""
    out: dict[str, tuple[dict, dict, dict]] = {}
    for workload in workloads:
        if workload.get("kind") not in TEMPLATE_KINDS:
            continue
        meta = workload.get("metadata") or {}
        obj = _object_of(meta.get("namespace", ""), workload["kind"], meta.get("name", ""))
        spec = _pod_template_spec(workload)
        if spec is not None:
            out[obj["object"]] = (obj, spec, workload)
    for pod in pods:
        meta = pod.get("metadata") or {}
        kind, name = resolver.resolve(meta.get("namespace", ""), "Pod", meta.get("name", ""))
        obj = _object_of(meta.get("namespace", ""), kind, name)
        if obj["object"] not in out:
            out[obj["object"]] = (obj, pod.get("spec") or {}, pod)
    return out


def _selector_label_keys(spec: dict) -> list[str]:
    keys = list((spec.get("nodeSelector") or {}).keys())
    node_affinity = (spec.get("affinity") or {}).get("nodeAffinity") or {}
    terms = list((node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or {}).get("nodeSelectorTerms") or [])
    terms += [p.get("preference") or {} for p in node_affinity.get("preferredDuringSchedulingIgnoredDuringExecution") or []]
    for term in terms:
        for expr in (term.get("matchExpressions") or []) + (term.get("matchFields") or []):
            if expr.get("key"):
                keys.append(expr["key"])
    return keys


def _containers(spec: dict) -> list[dict]:
    return (spec.get("containers") or []) + (spec.get("initContainers") or [])


def shapes_in_spec(obj: dict, spec: dict) -> list[dict]:
    """Entries 12, 20, 4, 18 from one pod spec, and 13/17 for a DaemonSet."""
    out = []
    deprecated = [k for k in _selector_label_keys(spec) if k.startswith(DEPRECATED_NODE_LABEL_PREFIXES)]
    if deprecated:
        selector = spec.get("nodeSelector") or {}
        evidence = ", ".join(f"{k}={selector[k]}" if k in selector else f"affinity on {k}" for k in sorted(set(deprecated)))
        out.append(_shape(obj, ENTRY_NODE_LABEL, HIGH, f"selector {evidence}", f"selector {sorted(set(deprecated))[0]}"))
    for container in _containers(spec):
        image = container.get("image") or ""
        if image.startswith(RETIRED_IMAGE_HOSTS):
            out.append(_shape(obj, ENTRY_REGISTRY, HIGH, f"image {image}", f"image host {_image_host(image)}"))
            break
    for volume in spec.get("volumes") or []:
        name = volume.get("name") or ""
        host_path = (volume.get("hostPath") or {}).get("path") or ""
        if "emptyDir" in volume and STATEFUL_VOLUME_NAME_RE.search(name):
            out.append(_shape(obj, ENTRY_NODE_LOCAL_STATE, MEDIUM, f"emptyDir volume `{name}`", f"volume {name}"))
        elif host_path.startswith(LOCAL_SSD_HOSTPATH_PREFIXES):
            out.append(_shape(obj, ENTRY_NODE_LOCAL_STATE, MEDIUM, f"hostPath volume `{name}` at {host_path}", f"volume {name}"))
    gpu = any(GPU_RESOURCE in ((c.get("resources") or {}).get("limits") or {}) or GPU_RESOURCE in ((c.get("resources") or {}).get("requests") or {}) for c in _containers(spec))
    if gpu:
        pins = []
        for container in _containers(spec):
            m = CUDA_IMAGE_PIN_RE.search(container.get("image") or "")
            if m:
                pins.append(f"image {container.get('image')}")
            for env in container.get("env") or []:
                if CUDA_ENV_NAME_MARKER in (env.get("name") or "").upper() and VERSION_IN_TEXT_RE.search(str(env.get("value") or "")):
                    pins.append(f"env {env.get('name')}={env.get('value')}")
        if pins:
            out.append(_shape(obj, ENTRY_GPU, MEDIUM, f"{GPU_RESOURCE} requested with {'; '.join(pins)}", "CUDA pin"))
    if obj["kind"] == "DaemonSet":
        if spec.get("hostNetwork"):
            out.append(_shape(obj, ENTRY_NODE_AGENT, MEDIUM, "DaemonSet on hostNetwork", "hostNetwork"))
        for volume in spec.get("volumes") or []:
            host_path = (volume.get("hostPath") or {}).get("path") or ""
            if host_path.startswith(CONTAINERD_SOCKET_PATH_PREFIXES):
                out.append(_shape(obj, ENTRY_RUNTIME, MEDIUM, f"DaemonSet mounts {host_path} (volume `{volume.get('name')}`)", f"hostPath {host_path}"))
                break
    return out


def _budget_allows_no_disruption(spec: dict, replicas: int | None) -> bool:
    """The catalogue's before-signal for entry 1: `maxUnavailable` 0 or
    `0%`, or `minAvailable` at or above the pods the budget covers, a
    percentage rounded up as the controller rounds it (`50%` of one pod,
    `60%` of two or `80%` of four demand every pod); with the covered count
    unknown a percentage forbids disruption only at 100%. A budget at
    `disruptionsAllowed` 0 for another reason (pods not ready behind a
    `maxUnavailable` 1) clears on its own. A budget covering no pod (the
    controller writes `expectedPods` 0 for a selector matching nothing, an
    orphaned budget or a workload scaled to zero) blocks no drain, whatever
    its spec: every `minAvailable` would be vacuously met."""
    if replicas == 0:
        return False
    max_unavailable = spec.get("maxUnavailable")
    if max_unavailable is not None:
        return str(max_unavailable).rstrip("%") == "0"
    min_available = spec.get("minAvailable")
    pct = _percent(min_available)
    if pct is not None:
        if replicas is not None:
            return -(-(pct * replicas) // PERCENT_ALL) >= replicas
        return pct >= PERCENT_ALL
    if isinstance(min_available, int) and replicas is not None:
        return min_available >= replicas
    return False


def _percent(value: object) -> int | None:
    """A budget field's percentage form (`"50%"`) as an integer, else None."""
    text = str(value) if isinstance(value, str) else ""
    return int(text.rstrip("%")) if text.endswith("%") and text.rstrip("%").isdigit() else None


def _budget_demands_the_only_pod(spec: dict, covered: int | None) -> bool:
    """The catalogue's third entry-1 before-signal: a single-replica workload
    behind a budget whose spec forbids evicting its only pod. The budget's
    controller counts every pod the selector covers together (`covered`), so
    the question is whether the spec allows no disruption across the set
    (`_budget_allows_no_disruption`, with the controller's rounding): a
    positive `maxUnavailable`, by count or by percentage, allows at least one
    eviction and never demands the singleton's pod; over one covered pod any
    `minAvailable` percentage above zero does. A controller that counts no
    pod (`covered` 0) demands nothing, as the budget's own legs grade it;
    only an unknown count falls back to the one replica."""
    if covered == 0:
        return False
    return _budget_allows_no_disruption(spec, SINGLE_REPLICA if covered is None else covered)


def budget_shapes(pdbs: list[dict], workloads: list[dict], pods: list[dict] | None = None) -> list[dict]:
    """Entry 1, the catalogue's three before-signals: a budget whose spec
    allows no disruption (high), one at `disruptionsAllowed` 0 for a reason
    that will clear (medium), and a single-replica Deployment or StatefulSet
    behind a budget whose spec demands its only pod (high). The pods a budget
    covers are counted as its controller counts them: `status.expectedPods`
    when the controller has written it, else the covered Deployments' and
    StatefulSets' `spec.replicas` plus every covered pod of another owner (a
    DaemonSet's, a Job's, a bare pod), which has no scale to read."""
    out = []
    for pdb in pdbs:
        meta, status = pdb.get("metadata") or {}, pdb.get("status") or {}
        namespace, name = meta.get("namespace", ""), meta.get("name", "")
        obj = _object_of(namespace, "PodDisruptionBudget", name)
        full_spec = pdb.get("spec") or {}
        spec = {k: v for k, v in full_spec.items() if k != "selector"}
        selector = full_spec.get("selector")
        covered = [] if selector is None else [w for w in workloads if w.get("kind") in ("Deployment", "StatefulSet") and (w.get("metadata") or {}).get("namespace") == namespace and _selector_matches(selector, _template_labels(w))]
        covered_pods = [] if selector is None else [p for p in pods or [] if (p.get("metadata") or {}).get("namespace") == namespace and _selector_matches(selector, (p.get("metadata") or {}).get("labels") or {})]
        unmanaged = [p for p in covered_pods if (_controller_of(p) or ("", ""))[0] not in SCALED_POD_OWNER_KINDS]
        expected = status.get("expectedPods")
        if selector is not None and isinstance(expected, int) and not isinstance(expected, bool):
            replicas: int | None = expected
        elif covered or unmanaged:
            replicas = sum((w.get("spec") or {}).get("replicas") or 0 for w in covered) + len(unmanaged)
        else:
            replicas = None
        # A budget covering no pod is graded on neither leg: at zero because
        # nothing is covered, it blocks no drain and owes no shape.
        if status.get("disruptionsAllowed") == 0 and replicas != 0:
            if _budget_allows_no_disruption(spec, replicas):
                out.append(_shape(obj, ENTRY_BUDGET, HIGH, f"disruptionsAllowed=0 by spec {json.dumps(spec, sort_keys=True)}" + (f" over {replicas} replica(s)" if replicas is not None else ""), f"budget {name}"))
            else:
                out.append(_shape(obj, ENTRY_BUDGET, MEDIUM, f"disruptionsAllowed=0 now, spec {json.dumps(spec, sort_keys=True)} allows disruption once its pods are ready", f"budget {name}"))
        for workload in covered:
            wmeta = workload.get("metadata") or {}
            if (workload.get("spec") or {}).get("replicas") == SINGLE_REPLICA and _budget_demands_the_only_pod(spec, replicas):
                wobj = _object_of(namespace, workload["kind"], wmeta.get("name", ""))
                out.append(_shape(wobj, ENTRY_BUDGET, HIGH, f"one replica behind budget {name} ({json.dumps(spec, sort_keys=True)}) covering {replicas} pod(s)", f"budget {name}"))
    return out


def storage_shapes(storage: list[dict], cluster: dict) -> list[dict]:
    """Entry 19: in-tree PD volumes and StorageClasses; high when the PD CSI
    add-on is off, medium when migration carries them and the class should
    still move."""
    addon = ((cluster.get("addonsConfig") or {}).get(PD_CSI_ADDON_KEY) or {}).get("enabled")
    confidence = MEDIUM if addon else HIGH
    addon_text = "PD CSI driver add-on enabled" if addon else "PD CSI driver add-on disabled"
    out = []
    for item in storage:
        meta = item.get("metadata") or {}
        obj = _object_of("", item.get("kind") or "", meta.get("name", ""))
        if item.get("kind") == "PersistentVolume" and IN_TREE_PD_VOLUME_KEY in (item.get("spec") or {}):
            disk = ((item.get("spec") or {}).get(IN_TREE_PD_VOLUME_KEY) or {}).get("pdName", "")
            out.append(_shape(obj, ENTRY_IN_TREE_VOLUME, confidence, f"in-tree {IN_TREE_PD_VOLUME_KEY} {disk}; {addon_text}", f"volume {meta.get('name', '')}"))
        elif item.get("kind") == "StorageClass" and item.get("provisioner") == IN_TREE_PD_PROVISIONER and not addon:
            # With the add-on on, CSI migration serves `gce-pd` and GKE's own
            # `standard` class would be a permanent row on every cluster.
            out.append(_shape(obj, ENTRY_IN_TREE_VOLUME, HIGH, f"provisioner {IN_TREE_PD_PROVISIONER}; {addon_text}; move to {CSI_PD_PROVISIONER}", f"class {meta.get('name', '')}"))
    return out


def _ready_services(endpointslices: list[dict]) -> set[tuple[str, str]]:
    ready = set()
    for es in endpointslices:
        meta = es.get("metadata") or {}
        service = (meta.get("labels") or {}).get(ENDPOINTSLICE_SERVICE_LABEL)
        if not service:
            continue
        for endpoint in es.get("endpoints") or []:
            if (endpoint.get("conditions") or {}).get("ready") is not False:
                ready.add((meta.get("namespace", ""), service))
                break
    return ready


def _rules_reach_pod_creation(hook: dict) -> bool:
    for rule in hook.get("rules") or []:
        groups = rule.get("apiGroups") or []
        resources = rule.get("resources") or []
        operations = rule.get("operations") or []
        if any(g in WEBHOOK_POD_GROUPS for g in groups) and any(r in WEBHOOK_POD_RESOURCES for r in resources) and any(o in WEBHOOK_POD_OPERATIONS for o in operations):
            return True
    return False


def webhook_shapes(webhooks: list[dict], endpointslices: list[dict] | None) -> list[dict]:
    """Entry 7: a fail-closed webhook whose Service has no ready endpoint.
    Without the endpoint read the check cannot run and reports nothing."""
    if endpointslices is None:
        return []
    ready = _ready_services(endpointslices)
    out = []
    for config in webhooks:
        if config.get("kind") not in WEBHOOK_CONFIGURATION_KINDS:
            continue
        meta = config.get("metadata") or {}
        obj = _object_of("", config["kind"], meta.get("name", ""))
        for hook in config.get("webhooks") or []:
            service = ((hook.get("clientConfig") or {}).get("service")) or {}
            if hook.get("failurePolicy") != WEBHOOK_FAIL_CLOSED or not service.get("name") or not _rules_reach_pod_creation(hook):
                continue
            if (service.get("namespace", ""), service["name"]) not in ready:
                # The catalogue's full shape also has a long timeout and no
                # namespaceSelector exempting kube-system; short of that it
                # is a lesser risk.
                timeout = hook.get("timeoutSeconds") or WEBHOOK_DEFAULT_TIMEOUT_S
                full = timeout >= WEBHOOK_LONG_TIMEOUT_S and not hook.get("namespaceSelector")
                out.append(_shape(obj, ENTRY_WEBHOOK, HIGH if full else MEDIUM, f"webhook {hook.get('name')} failurePolicy {WEBHOOK_FAIL_CLOSED}, timeout {timeout}s{'' if hook.get('namespaceSelector') else ', no namespaceSelector'}; Service {service.get('namespace', '')}/{service['name']} has no ready endpoint", f"webhook {hook.get('name')}"))
                break
    return out


def _image_repository_and_tag(image: str) -> tuple[str, str]:
    """`docker.io/library/eclipse-temurin:8u302-b08-jre` -> (`eclipse-temurin`, `8u302-b08-jre`);
    the repository is the last path component, the tag what follows the colon."""
    name, _, digest_free = image.partition("@")
    path, sep, tag = name.rpartition(":")
    if not sep or "/" in tag:
        path, tag = name, ""
    return path.rsplit("/", 1)[-1], tag


def cgroup_v1_runtime(image: str) -> str | None:
    """Why `image` is a runtime that misreads a cgroup v2 limit, or None."""
    repository, tag = _image_repository_and_tag(image)
    if repository in JAVA_IMAGE_REPOS:
        m = JAVA_TAG_RE.match(tag)
        if not m:
            return None
        major, update, minor, patch = (int(x) if x is not None else None for x in m.groups())
        if major == 8 and update is not None and update < JDK8_CGROUP_V2_UPDATE:
            return f"JDK 8u{update} < 8u{JDK8_CGROUP_V2_UPDATE}"
        if major == 11 and minor is not None and (minor, patch) < JDK11_CGROUP_V2_PATCH:
            return f"JDK 11.{minor}.{patch} < 11.{JDK11_CGROUP_V2_PATCH[0]}.{JDK11_CGROUP_V2_PATCH[1]}"
        if major < 8 or (8 < major < 11) or (11 < major < JDK_FIRST_MAJOR_WITH_CGROUP_V2):
            return f"JDK {major} predates cgroup v2 support"
        return None
    if any(marker in image for marker in DOTNET_IMAGE_REPO_MARKERS):
        m = DOTNET_TAG_RE.match(tag)
        if m and (int(m.group(1)), int(m.group(2))) < DOTNET_CGROUP_V2_VERSION:
            return f".NET {m.group(1)}.{m.group(2)} < {DOTNET_CGROUP_V2_VERSION[0]}.{DOTNET_CGROUP_V2_VERSION[1]}"
    return None


def _pool_exposure(pool: str, mode: str, version: str) -> str | None:
    """How the pool exposes a cgroup v1 runtime: on v2 now, or v1 but below
    the minor GKE migrates at."""
    if mode == CGROUP_V2_MODE:
        return f"pool {pool} on cgroup v2"
    parsed = parse_version(version)
    if mode == CGROUP_V1_MODE and parsed and parsed[1] < CGROUP_V2_MIGRATION_MINOR:
        return f"pool {pool} on cgroup v1 at {version}, migrated to v2 at 1.{CGROUP_V2_MIGRATION_MINOR}"
    return None


def runtime_shapes(obj: dict, spec: dict, pools: list[str], cluster: dict) -> list[dict]:
    """Entry 14 for a pod spec: a cgroup v1 runtime image on an exposed pool.
    `pools` are the pools the owner's pods run on; empty means any pool."""
    modes, versions = pool_cgroup_modes(cluster), versions_of(cluster)["node_pools"]
    candidates = pools or sorted(modes)
    exposures = [e for e in (_pool_exposure(p, modes.get(p, ""), versions.get(p, "")) for p in candidates) if e]
    if not exposures:
        return []
    for container in _containers(spec):
        reason = cgroup_v1_runtime(container.get("image") or "")
        if reason:
            return [_shape(obj, ENTRY_CGROUP_V2, MEDIUM, f"image {container.get('image')} ({reason}) on {'; '.join(exposures)}", "runtime image")]
    return []


def _is_managed_agent_namespace(namespace: str) -> bool:
    return is_system_namespace(namespace) or namespace.startswith(MANAGED_AGENT_NAMESPACE_PREFIXES)


def collect_risks(cluster: dict, reads: dict[str, list]) -> tuple[list[dict], int]:
    """The shapes present, and how many GKE-managed agents carry 13/17
    (counted, not reported: they move with the node image)."""
    pods = reads.get("pods") or []
    nodes = reads.get("nodes") or []
    workloads = reads.get("workloads") or []
    resolver = Resolver(pods, reads.get("owners") or [], workloads)
    node_pool = {(n.get("metadata") or {}).get("name"): ((n.get("metadata") or {}).get("labels") or {}).get(NODEPOOL_LABEL, "") for n in nodes}
    pools_of: dict[str, set[str]] = {}
    for pod in pods:
        meta = pod.get("metadata") or {}
        kind, name = resolver.resolve(meta.get("namespace", ""), "Pod", meta.get("name", ""))
        pool = node_pool.get((pod.get("spec") or {}).get("nodeName"), "")
        if pool:
            pools_of.setdefault(_object_ref(meta.get("namespace", ""), kind, name), set()).add(pool)
    shapes: list[dict] = []
    for obj, spec, _ in pod_specs_by_owner(workloads, pods, resolver).values():
        shapes += shapes_in_spec(obj, spec)
        shapes += runtime_shapes(obj, spec, sorted(pools_of.get(obj["object"], ())), cluster)
    shapes += budget_shapes(reads.get("pdbs") or [], workloads, pods)
    shapes += storage_shapes(reads.get("storage") or [], cluster)
    shapes += webhook_shapes(reads.get("webhooks") or [], reads.get("endpointslices") if "endpointslices" in reads else None)
    managed_agents: set[str] = set()
    kept = []
    for shape in shapes:
        if shape["entry"] in MANAGED_AGENT_ENTRIES and _is_managed_agent_namespace(shape["namespace"]):
            managed_agents.add(shape["object"])
            continue
        kept.append(shape)
    unique: dict[tuple, dict] = {}
    for shape in kept:
        unique.setdefault((shape["object"], shape["entry"]), shape)
    return sorted(unique.values(), key=lambda s: (s["system"], s["namespace"], s["kind"], s["name"], s["entry"])), len(managed_agents)


def collect_shapes(cluster: dict, reads: dict[str, list]) -> list[dict]:
    return collect_risks(cluster, reads)[0]


# --------------------------------------------------------------------------- #
# The next upgrade: target, window, exclusions.
# --------------------------------------------------------------------------- #


def fetch_server_config(project: str, location: str, *, run: RunFn) -> tuple[dict | None, str | None]:
    parsed, error = run_json(["gcloud", "container", "get-server-config", "--location", location, "--project", project, "--format", "json"], run=run)
    if error:
        return None, error
    return (parsed if isinstance(parsed, dict) else None), (None if isinstance(parsed, dict) else "get-server-config returned JSON that is not an object")


def _parse_iso_duration(text: str) -> timedelta | None:
    m = ISO_DURATION_RE.match(text or "")
    if not m:
        return None
    hours, minutes, seconds = (int(x or 0) for x in m.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def recurring_window_rule(recurring: dict) -> tuple[dict, str | None, datetime | None, timedelta, list[int]]:
    """One parse of a `recurringWindow` for the next opening and the last
    closing alike: the raw rule, the frequency (None when it is one this
    does not read), the window's start, its length, and the weekdays it
    repeats on: BYDAY's for either frequency (RFC 5545 lets DAILY carry
    one), DTSTART's weekday for WEEKLY without BYDAY, every day (an empty
    list) for DAILY without."""
    rule = {k: v for k, v in (part.split("=", 1) for part in (recurring.get("recurrence") or "").split(";") if "=" in part)}
    start, end = parse_ts((recurring.get("window") or {}).get("startTime")), parse_ts((recurring.get("window") or {}).get("endTime"))
    freq = rule.get("FREQ")
    days = [RRULE_WEEKDAYS[d] for d in (rule.get("BYDAY") or "").split(",") if d in RRULE_WEEKDAYS]
    if freq == RRULE_FREQ_WEEKLY and not days and start is not None:
        days = [start.weekday()]
    length = (end - start) if (start and end) else timedelta(0)
    return rule, (freq if freq in (RRULE_FREQ_DAILY, RRULE_FREQ_WEEKLY) else None), start, length, days


def _next_opening(window: dict, now: datetime) -> tuple[str, datetime | None]:
    """A one-line description of the maintenance window and when it next
    opens; `None` when the rule is one this does not parse."""
    daily = window.get("dailyMaintenanceWindow")
    if daily:
        start = datetime.strptime(daily.get("startTime") or "00:00", DAILY_WINDOW_TIME_FORMAT).time()
        length = _parse_iso_duration(daily.get("duration") or "") or DEFAULT_DAILY_WINDOW
        candidate = datetime.combine(now.date(), start, tzinfo=timezone.utc)
        if candidate <= now:
            candidate += timedelta(days=1)
        return f"daily at {daily.get('startTime')} UTC for {int(length.total_seconds() // SECONDS_PER_HOUR)}h", candidate
    recurring = window.get("recurringWindow")
    if recurring:
        rule, freq, start, length, days = recurring_window_rule(recurring)
        if start is None:
            return f"recurring {recurring.get('recurrence')}", None
        text = f"{rule.get('FREQ') or 'recurring'}{' on ' + rule['BYDAY'] if rule.get('BYDAY') else ''} at {start.strftime(DAILY_WINDOW_TIME_FORMAT)} UTC for {int(length.total_seconds() // SECONDS_PER_HOUR)}h"
        if freq is None:
            return text, None
        for offset in range(NEXT_WINDOW_SEARCH_DAYS):
            candidate = datetime.combine((now + timedelta(days=offset)).date(), start.timetz())
            if candidate >= now and (not days or candidate.weekday() in days):
                return text, candidate
        return text, None
    return NO_WINDOW_TEXT, None


def next_upgrade(cluster: dict, server_config: dict | None, now: datetime) -> dict:
    current = cluster.get("currentMasterVersion") or ""
    channel = (cluster.get("releaseChannel") or {}).get("channel") or ""
    target = None
    if server_config:
        if channel:
            info = next((c for c in server_config.get("channels") or [] if c.get("channel") == channel), None)
            target = (info or {}).get("defaultVersion")
        else:
            target = server_config.get("defaultClusterVersion")
    cur_t, tgt_t = parse_version(current), parse_version(target or "")
    below = (cur_t < tgt_t) if cur_t and tgt_t else None
    policy = ((cluster.get("maintenancePolicy") or {}).get("window") or {})
    window_text, opens = _next_opening(policy, now)
    exclusions = []
    for name, exclusion in sorted((policy.get("maintenanceExclusions") or {}).items()):
        start, end = parse_ts(exclusion.get("startTime")), parse_ts(exclusion.get("endTime"))
        exclusions.append({
            "name": name,
            "scope": ((exclusion.get("maintenanceExclusionOptions") or {}).get("scope")) or DEFAULT_EXCLUSION_SCOPE,
            "start": fmt_ts(start) if start else None,
            "end": fmt_ts(end) if end else None,
            "active": bool(start and end and start <= now <= end),
        })
    return {
        "channel": channel,
        "current": current,
        "target": target,
        "below_target": below,
        "window": window_text,
        "next_opens": fmt_ts(opens) if opens else None,
        "exclusions": exclusions,
    }


def parse_version(v: str) -> tuple[int, int, int, int] | None:
    m = VERSION_RE.match(v or "")
    if not m:
        return None
    major, minor, patch, build = m.groups()
    return (int(major), int(minor), int(patch), int(build or 0))


# --------------------------------------------------------------------------- #
# The review of one cluster.
# --------------------------------------------------------------------------- #


def recheck_reads_for(guard: dict) -> set[str]:
    """The reads a guard's re-check needs: a risk guard's recorded reads; a
    failure guard's pods, owners and nodes, with the budget, workload or
    event read its entry or source adds."""
    if guard.get("entry") == ENTRY_OPERATION:
        return set()  # re-checked through the operations listing
    if guard.get("kind", GUARD_KIND_FAILURE) == GUARD_KIND_RISK:
        return set(guard.get("reads") or SPEC_SHAPE_READS)
    reads = set(RECHECK_FAILURE_READS)
    if guard.get("entry") == ENTRY_BUDGET:
        reads |= set(RECHECK_BUDGET_READS)
    if guard.get("entry") == ENTRY_SPREAD:
        reads |= set(RECHECK_SPREAD_READS)
    if guard.get("source") == GUARD_SOURCE_EVENT:
        reads |= set(RECHECK_EVENT_READS)
    return reads


def recheck_reads_needed(cluster_guards: list[dict], refresh: bool) -> set[str]:
    """Every read a re-check of these guards makes, plus the refresh reads on
    a full run."""
    needed: set[str] = set(REFRESH_READS) if refresh else set()
    for guard in cluster_guards:
        needed |= recheck_reads_for(guard)
    return needed


def recheck_operation_guards(guards: list[dict], operations: list[dict], listed: set[str], unread: set[str], seen_at: str, scope: set[str] | None = None, pools: dict[str, set[str]] | None = None) -> dict:
    """The operation guards against this run's listing, which every run
    makes for each project. A guard not seen this run is refreshed while
    the listing returns its operation DONE with an error and no later
    operation of the same type on the same target reached DONE without one.
    Three things clear it: that later operation, the retry that succeeded;
    the operation itself listed without an error; and, for a node-pool
    operation, a cluster record (`pools`, per enumerated cluster) that no
    longer names the target pool. A listing that answered without the
    operation holds it (`unlisted`): GKE retains operation records for a
    limited time, and a record that aged out is not a retry. A failed
    listing holds it (`held`); a project not listed this run, or a cluster
    outside a scoped run, is left alone."""
    result: dict[str, list] = {"refreshed": [], "cleared": [], "held": [], "unlisted": []}
    for guard in guards:
        if guard.get("kind", GUARD_KIND_FAILURE) != GUARD_KIND_FAILURE or guard.get("entry") != ENTRY_OPERATION or guard.get("last_seen") == seen_at:
            continue
        if scope is not None and guard.get("cluster") not in scope:
            continue
        project = str(guard.get("cluster") or "").split(CLUSTER_KEY_SEPARATOR)[0]
        if project in unread:
            result["held"].append(guard["id"])
            continue
        if project not in listed:
            continue
        name = str(guard.get("object") or "").split("/", 1)[-1]
        cluster_parts = str(guard.get("cluster") or "").split(CLUSTER_KEY_SEPARATOR, 2)
        own_cluster = tuple(cluster_parts[1:3]) if len(cluster_parts) == 3 else ("", "")
        ops = [op for op in operations if op.get("project") == project]
        op = next((o for o in ops if o.get("name") == name), None)
        summary = operation_summary(op) if op is not None else None
        op_type = guard.get(OPERATION_GUARD_TYPE_KEY) or (summary["type"] if summary else None)
        target = guard.get(OPERATION_GUARD_TARGET_KEY) or (summary["target"] if summary else None)
        started = parse_ts(guard.get(OPERATION_GUARD_START_KEY)) or (parse_ts(op.get("startTime")) if op else None)
        cluster_pools = (pools or {}).get(str(guard.get("cluster") or ""))
        if op_type == OP_UPGRADE_NODES and target and target != CONTROL_PLANE_TARGET and cluster_pools is not None and target not in cluster_pools:
            result["cleared"].append({"id": guard["id"], "reason": OPERATION_GUARD_POOL_GONE_FORMAT.format(target=target)})
            continue
        # The one failure predicate decides the retry too: `DONE` with a
        # `statusMessage` and no `error` object is a failed operation to the
        # guard and the Error, so it is no clean retry here.
        # The retry is the guard's own cluster's: a same-named pool on another
        # cluster in the project (GKE's `default-pool`), or another cluster's
        # control plane, is no retry of this operation.
        retries = [o for o in ops if o.get("name") != name and op_type and o.get("operationType") == op_type and target and operation_summary(o)["target"] == target and (parse_target_link(o.get("targetLink", "")) or ("", "", None))[:2] == own_cluster and not _operation_failed(operation_summary(o)) and started is not None and (parse_ts(o.get("startTime")) or started) > started]
        if retries:
            retry = operation_summary(sorted(retries, key=lambda o: o.get("startTime") or "")[0])
            result["cleared"].append({"id": guard["id"], "reason": OPERATION_GUARD_RETRIED_FORMAT.format(type=retry["type"], target=retry["target"], name=retry["name"], end=retry["end"] or "?")})
        elif op is None:
            result["unlisted"].append({"id": guard["id"], "reason": OPERATION_GUARD_UNLISTED_FORMAT.format(name=name)})
        elif _operation_failed(summary):
            result["refreshed"].append(guard["id"])
        else:
            result["cleared"].append({"id": guard["id"], "reason": OPERATION_GUARD_CLEAN_FORMAT.format(name=name)})
    return result


def recheck_cluster(key: str, cluster: dict, cluster_guards: list[dict], *, run: RunFn, refresh: bool = False, now: datetime | None = None) -> dict:
    """Re-read an unchanged cluster for the guards it holds: the symptom or
    shape behind each is looked for again with only the reads it needs (an
    event-source guard adds the events read). A guard whose finding is gone,
    and whose reads all answered, is cleared; one a needed read failed for is
    held as it stands, named in `held` with the reads that did not answer."""

    needed = recheck_reads_needed(cluster_guards, refresh)
    result = {"cluster": key, "guards": len(cluster_guards), "cleared": [], "refreshed": [], "held": {}, "failed_reads": [], "errors": [], "symptom_baseline": None, "commands": {}}
    kubeconfig, error = fetch_credentials(cluster, run=run)
    if error:
        result["errors"].append(error)
        result["failed_reads"] = sorted(needed)
        result["held"] = {guard["id"]: sorted(recheck_reads_for(guard)) for guard in cluster_guards}
        return result
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    reads: dict[str, list] = {}
    for name, argv in KUBECTL_READS:
        if name not in needed:
            continue
        parsed, read_error = run_json(argv, run=run, timeout=KUBECTL_TIMEOUT_S, env=env)
        if read_error:
            result["errors"].append(f"{name}: {read_error}")
            result["failed_reads"].append(name)
            continue
        reads[name] = (parsed.get("items") or []) if isinstance(parsed, dict) else []
        result["commands"][name] = command_text(argv, kubeconfig)
    answered = set(reads)
    # A budget still allowing no disruption keeps its entry-1 failure guard:
    # the drain it held cannot be re-observed without the operation.
    budgets_at_zero = {_object_ref((b.get("metadata") or {}).get("namespace", ""), "PodDisruptionBudget", (b.get("metadata") or {}).get("name", "")) for b in reads.get("pdbs") or [] if (b.get("status") or {}).get("disruptionsAllowed") == 0}
    # A workload still unavailable keeps its entry-3 guard: the operation its
    # outage fell inside cannot be re-observed, the outage can.
    unavailable = set(_unavailable_workloads(reads.get("workloads") or []))
    # A skew that still stands keeps its entry-5 guard, whatever the window
    # is doing at this hour: a guard is cleared only when the skew is gone.
    skew = version_skew(cluster, now) if now is not None else {"skews": [], "state": "", "keys": []}
    skewed = {_object_ref("", "NodePool", pool) for pool, _ in skew["skews"]}
    symptoms = collect_symptoms(cluster, reads, [], EPOCH, now=now)
    if refresh and set(REFRESH_READS) <= answered:
        result["symptom_baseline"] = sorted({symptom_key(sym) for sym in symptoms} | set(skew["keys"]))
    fresh_failure = guards_for(key, symptoms, "")
    fresh = {g["id"] for g in fresh_failure}
    fresh |= {g["id"] for g in risk_guards_for(key, collect_risks(cluster, reads)[0], "")}
    # An event-source guard is also present while the pods read shows its
    # object failing: the event may have left the API server's hour, the
    # symptom has not.
    failing_objects = {g["object"] for g in fresh_failure}
    for guard in cluster_guards:
        kind = guard.get("kind", GUARD_KIND_FAILURE)
        reads_needed = recheck_reads_for(guard)
        present = guard["id"] in fresh
        if kind == GUARD_KIND_FAILURE:
            present = present or (guard.get("source") == GUARD_SOURCE_EVENT and guard.get("object") in failing_objects) or (guard.get("entry") == ENTRY_BUDGET and guard.get("object") in budgets_at_zero) or (guard.get("entry") == ENTRY_SPREAD and guard.get("object") in unavailable) or (guard.get("entry") == ENTRY_WINDOW and guard.get("object") in skewed)
        if present:
            result["refreshed"].append(guard["id"])
        elif reads_needed <= answered:
            result["cleared"].append(guard["id"])
        else:
            result["held"][guard["id"]] = sorted(reads_needed - answered)
    return result


def deferred_recheck(key: str, cluster_guards: list[dict]) -> dict | None:
    """The record of a re-check the run's deadline left unstarted: with
    guards, a failed read of every kind the guards need, so their checks are
    unevaluated, the guards held and the run partial, as after a re-check
    that raised; with none, nothing to record (the stored symptom set keeps
    its age)."""
    if not cluster_guards:
        return None
    return {"cluster": key, "guards": len(cluster_guards), "cleared": [], "refreshed": [], "held": {g["id"]: sorted(recheck_reads_for(g)) for g in cluster_guards}, "failed_reads": sorted(recheck_reads_needed(cluster_guards, False)), "errors": [RECHECK_DEFERRED_TEXT.format(seconds=RUN_DEADLINE_S)], "symptom_baseline": None, "commands": {}, "deferred": True}


def _safe_recheck(key: str, cluster: dict, cluster_guards: list[dict], *, run: RunFn, refresh: bool = False, now: datetime | None = None) -> dict:
    try:
        return recheck_cluster(key, cluster, cluster_guards, run=run, refresh=refresh, now=now)
    except Exception as exc:  # noqa: BLE001 -- the boundary is the point
        log(f"{key}: re-check failed: {exc!r}")
        # A re-check that raised is a failed read of every kind it would have
        # made: the unchanged row then lists those checks as unevaluated and
        # the run as partial, as after a credentials failure, rather than
        # claiming every check ran on the listing's strength beside a
        # limitation saying none did.
        return {"cluster": key, "guards": len(cluster_guards), "cleared": [], "refreshed": [], "held": {g["id"]: sorted(recheck_reads_for(g)) for g in cluster_guards}, "failed_reads": sorted(recheck_reads_needed(cluster_guards, refresh)), "errors": [f"re-check failed: {exc!r}"[:ERROR_EXCERPT_CHARS]], "symptom_baseline": None, "commands": {}}


def _bare_what_happened(selection: Selection) -> dict:
    """What a failed review can still say about the cluster without reading
    anything that can raise: the selection's own facts."""
    return {
        "status": selection.status,
        "reasons": list(selection.reasons),
        "window_start": fmt_ts(selection.window_start),
        "channel": "",
        "versions_before": None,
        "versions_after": {"control_plane": "", "node_pools": {}},
        "cluster_status": "",
        "operations": [],
        "symptom_window_start": fmt_ts(selection.window_start),
    }


def _bare_next_upgrade() -> dict:
    return {"channel": "", "current": "", "target": None, "below_target": None, "window": NO_WINDOW_TEXT, "next_opens": None, "exclusions": []}


def _safe_next_upgrade(cluster: dict, server_config: dict | None, now: datetime, errors: list[str] | None = None) -> dict:
    """`next_upgrade` under the per-cluster boundary: a maintenance window in
    a shape the parser rejects loses that cluster's next-upgrade line, not
    the run. The error joins the caller's read errors when it keeps any."""
    try:
        return next_upgrade(cluster, server_config, now)
    except Exception as exc:  # noqa: BLE001 -- the boundary is the point
        log(f"{cluster_key(cluster.get('project', ''), cluster.get('location', ''), cluster.get('name', ''))}: next upgrade failed: {exc!r}")
        if errors is not None:
            errors.append(f"{NEXT_UPGRADE_ERROR_PREFIX}{exc!r}"[:ERROR_EXCERPT_CHARS])
        return _bare_next_upgrade()


def _safe_review(selection: Selection, ledger: dict, **kwargs) -> dict:
    """`review_cluster` with the exception boundary the docstring promises:
    an object shape this collector did not expect is a failed read of that
    cluster, not the end of the run. The fallback calls nothing that reads
    the cluster record, so it cannot raise in turn; the record's own parts
    are attempted one at a time. It keeps the two facts the listing
    established before any read, which `review_cluster` records first: the
    operation check the listing backs and the guard of a failed operation,
    so the entry is `collected` on that check with the kubectl checks
    unevaluated, as a cluster whose credentials failed is, and the Error the
    report shows is the entry's candidate with its guard written."""
    try:
        return review_cluster(selection, ledger, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- the boundary is the point
        log(f"{selection.key}: review failed: {exc!r}")
        try:
            happened = what_happened(selection, ledger)
        except Exception:  # noqa: BLE001 -- the fallback must not raise
            happened = _bare_what_happened(selection)
        upcoming = _safe_next_upgrade(selection.cluster, kwargs.get("server_config"), kwargs.get("now") or now_utc())
        # Both read the summaries `what_happened` built and cannot raise in turn.
        operations_command = kwargs.get("operations_command")
        seen_at = kwargs.get("seen_at") or fmt_ts(kwargs.get("now") or now_utc())
        return {
            "cluster": selection.key,
            "project": selection.cluster.get("project", ""),
            "location": selection.cluster.get("location", ""),
            "name": selection.cluster.get("name", ""),
            "what_happened": happened,
            "what_failed": [],
            "mitigations": [],
            "shapes": [],
            "managed_agents": 0,
            "next_upgrade": upcoming,
            "baseline": None,
            "symptom_baseline": None,
            "guards": operation_guards_for(selection.key, happened["operations"], seen_at),
            "read_errors": [f"review failed: {exc!r}"[:ERROR_EXCERPT_CHARS]],
            "reviewed": False,
            "partial": list(CORE_READS),
            "answered": [],
            "commands": checks_run({OPERATIONS_READ: operations_command}) if operations_command else [],
            "starting": [],
            "gated": [],
            "terminating": [],
            "skew_notes": [],
            "unavailable_workloads": [],
        }


def review_cluster(selection: Selection, ledger: dict, *, run: RunFn, seen_at: str, server_config: dict | None = None, now: datetime | None = None, operations_command: str | None = None) -> dict:
    """`operations_command` is the project's operations listing as `collect`
    ran it, when it answered; without one the review vouches for no operation
    check; a review none of whose kubectl reads answered vouches for the
    operation check alone, since the listing answered, and for none at all
    when the listing failed too."""
    cluster = selection.cluster
    review = {
        "cluster": selection.key,
        "project": cluster["project"],
        "location": cluster["location"],
        "name": cluster["name"],
        "what_happened": what_happened(selection, ledger),
        "what_failed": [],
        "mitigations": [],
        "shapes": [],
        "managed_agents": 0,
        "next_upgrade": _bare_next_upgrade(),
        "baseline": None,
        "symptom_baseline": None,
        "guards": [],
        "read_errors": [],
        "reviewed": False,
        "partial": [],
        "answered": [],
        "commands": [],
        "starting": [],
        "gated": [],
        "terminating": [],
        "skew_notes": [],
        "unavailable_workloads": [],
    }
    review["next_upgrade"] = _safe_next_upgrade(cluster, server_config, now or now_utc(), review["read_errors"])
    # A failed operation's guard needs no read: written whatever the reads do,
    # and the operation check is backed by the listing before any kubectl read.
    review["guards"] = operation_guards_for(selection.key, review["what_happened"]["operations"], seen_at)
    review["commands"] = checks_run({OPERATIONS_READ: operations_command}) if operations_command else []
    ops = selection.operations
    first_op = min((parse_ts(op.get("startTime")) for op in ops if parse_ts(op.get("startTime"))), default=None)
    window_start = first_op or selection.window_start
    review["what_happened"]["symptom_window_start"] = fmt_ts(window_start)
    kubeconfig, error = fetch_credentials(cluster, run=run)
    if error:
        # Nothing was read: a partial attempt, re-selected next run.
        review["read_errors"].append(error)
        review["partial"] = list(CORE_READS)
        return review
    reads, errors = read_cluster(kubeconfig, run=run)
    review["read_errors"].extend(errors)
    failed = {e.split(":")[0] for e in errors}
    review["answered"] = [name for name, _ in KUBECTL_READS if name not in failed]
    review["partial"] = [name for name in CORE_READS if name in failed]
    if len(review["partial"]) == len(CORE_READS):
        # Nothing was evaluated, so nothing counts as answered: no guard of
        # this cluster may be dropped on this run's say-so, and no check ran.
        review["answered"] = []
        return review
    commands = {name: command_text(argv, kubeconfig) for name, argv in KUBECTL_READS if name not in failed}
    if operations_command:
        commands[OPERATIONS_READ] = operations_command
    review["commands"] = checks_run(commands)
    # A partial read still reports what it saw; it is `reviewed` -- the
    # ledger moves and absent guards drop -- only when every core read answered.
    review["reviewed"] = not review["partial"]
    starting: list[str] = []
    gated: list[str] = []
    terminating: list[str] = []
    entry = (ledger.get("clusters") or {}).get(selection.key) or {}
    stored = entry.get("symptoms") if entry.get("last_run") else None
    skews_first_seen = entry.get(LEDGER_SKEW_FIRST_SEEN_KEY) or {}
    symptoms = collect_symptoms(cluster, {k: v for k, v in reads.items() if k not in failed}, ops, window_start, operations_known=operations_command is not None, now=now, starting=starting, baseline=stored, gated=gated, terminating=terminating, skew_first_seen=skews_first_seen)
    review["starting"] = starting
    review["gated"] = gated
    review["terminating"] = terminating
    skew = version_skew(cluster, now or now_utc(), skews_first_seen)
    # The pools still skewed, for the keep rule: under an open window or an
    # active exclusion the review files no row, and the entry-5 guard is kept
    # as the re-check keeps it, cleared only when the skew is gone.
    review["skewed_pools"] = [_object_ref("", "NodePool", pool) for pool, _ in skew["skews"]]
    if skew["skews"] and not any(sym["category"] == CATEGORY_VERSION_SKEW for sym in symptoms):
        skews_text = "; ".join(text for _, text in skew["skews"])
        if skew.get("no_policy"):
            review["skew_notes"] = [SKEW_NO_POLICY_TEXT.format(skews=skews_text)]
        elif skew["state"]:
            review["skew_notes"] = [SKEW_OPEN_TEXT.format(skews=skews_text, state=skew["state"])]
        else:
            review["skew_notes"] = [SKEW_YOUNG_TEXT.format(skews=skews_text, age="; ".join(skew["ages"][key] for key in skew["keys"]), limit=SKEW_STANDING_DAYS)]
    mark_since(symptoms, stored, first_op)
    review["what_failed"] = symptoms
    # Every skew seen joins the stored set, graded or not, so the ledger can
    # date it (`LEDGER_SKEW_FIRST_SEEN_KEY`) and a later run grade it.
    review["symptom_baseline"] = sorted({symptom_key(sym) for sym in symptoms} | set(skew["keys"]))
    review["budgets_at_zero"] = sorted(_object_ref((b.get("metadata") or {}).get("namespace", ""), "PodDisruptionBudget", (b.get("metadata") or {}).get("name", "")) for b in reads.get("pdbs") or [] if (b.get("status") or {}).get("disruptionsAllowed") == 0)
    # Workloads still unavailable, for the entry-3 keep rule: the operation
    # the outage fell inside is outside a later window, the outage is not.
    review["unavailable_workloads"] = sorted(_unavailable_workloads(reads.get("workloads") or [])) if "workloads" not in failed else []
    # The objects still failing when the window is ignored, graded as the
    # re-check grades them (the symptom pass at EPOCH, the starting grace
    # and the scheduling gates applied, so a replica merely starting is no
    # failure): a failure guard on one of them that this review filed nothing
    # for is held by the merge as a re-check holds it, since a pod that lost
    # readiness, or a Job pod in phase Failed, before the window's first
    # operation is not graded again by the windowed pass yet still fails.
    review["failing_objects"] = sorted({g["object"] for g in guards_for(selection.key, collect_symptoms(cluster, {k: v for k, v in reads.items() if k not in failed}, [], EPOCH, now=now), "")}) if "pods" not in failed else []
    review["mitigations"] = [mitigation_lines(s, c) for s in symptoms for c in s["classifications"] if c["entry"] is not None]
    shapes, managed_agents = collect_risks(cluster, {k: v for k, v in reads.items() if not any(e.startswith(k + ":") for e in errors)})
    review["shapes"] = shapes
    review["managed_agents"] = managed_agents
    review["guards"] += guards_for(selection.key, symptoms, seen_at, upgrade_context=selection.status == STATUS_UPGRADED or bool(ops)) + risk_guards_for(selection.key, shapes, seen_at)
    review["baseline"] = {
        "versions": versions_of(cluster),
        "pods": len(reads.get("pods") or []),
        "budgets": len(reads.get("pdbs") or []),
        "shapes": len(shapes),
        "symptom_count": len(symptoms),
        "first_seen": sum(1 for sym in symptoms if sym.get("since") == SINCE_FIRST_SEEN),
        "first_run": not entry.get("last_run"),
        "guards": [g["id"] for g in review["guards"]],
        "shape_reads_failed": [e for e in errors if e.split(":")[0] in SHAPE_READS],
    }
    return review


# --------------------------------------------------------------------------- #
# Rendering.
# --------------------------------------------------------------------------- #


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


def _versions_table(before: dict | None, after: dict) -> list[str]:
    lines = ["| Target | Before | After |", "| --- | --- | --- |"]
    prev_cp = (before or {}).get("control_plane") or "-"
    lines.append(f"| {CONTROL_PLANE_TARGET} | {prev_cp} | {after['control_plane']} |")
    for pool, version in sorted(after["node_pools"].items()):
        lines.append(f"| pool `{_cell(pool)}` | {_cell(((before or {}).get('node_pools') or {}).get(pool) or '-')} | {_cell(version)} |")
    return lines


def _operation_failed(op: dict) -> bool:
    return bool(op.get("error")) or (op.get("status") or "") != OPERATION_TERMINAL_STATUS


def _symptom_severity(symptom: dict) -> str:
    """Error for a high-confidence entry on a user workload, or a node that
    went NotReady after its pool was upgraded; Warning for everything else
    (medium, system namespace, unclassified)."""
    for c in symptom["classifications"]:
        if c["entry"] == NODE_AFTER_POOL_UPGRADE_ENTRY and c["confidence"] == HIGH and symptom["category"] == CATEGORY_NODE:
            return SEVERITY_ERROR
        if c["entry"] is not None and c["confidence"] == HIGH and not symptom["system"]:
            return SEVERITY_ERROR
    return SEVERITY_WARNING


def _entries_label(classifications: list[dict]) -> str:
    numbers = sorted({c["entry"] for c in classifications if c["entry"] is not None})
    label = ", ".join(str(n) for n in numbers)
    if any(c["entry"] is None for c in classifications):
        label = f"{label}, {UNCLASSIFIED}" if label else UNCLASSIFIED
    return label


def _incident_key(incident: dict) -> tuple[str, str]:
    return incident["cluster"], incident["object"]


def triage(reviews: list[dict], unchanged: list[dict], failed_reads: list[str], guards: list[dict], seen_at: str, removed: list[str] | None = None, rechecks: list[dict] | None = None, upgrading: list[dict] | None = None, in_scope: set[str] | None = None, scope_reason: str = "", unlisted: set[str] | None = None, operation_guards: dict | None = None, listing_unread: set[str] | None = None) -> dict:
    """Group the reviews into the report's three sections. An incident is one
    object on one cluster: its symptoms, their (C) rows and the guards it
    produced, plus the cluster's (A) summary."""
    errors: list[dict] = []
    warnings: list[dict] = []
    clean: list[dict] = []
    for review in reviews:
        incidents: dict[str, dict] = {}
        upgraded = review["what_happened"]["status"] == STATUS_UPGRADED
        for symptom in review["what_failed"]:
            incident = incidents.get(symptom["object"])
            if incident is None:
                incidents[symptom["object"]] = incident = {
                    "kind": INCIDENT_SYMPTOM,
                    "severity": SEVERITY_WARNING,
                    "cluster": review["cluster"],
                    "object": symptom["object"],
                    "system": symptom["system"],
                    "entries": "",
                    "predates_upgrade": False,
                    "outside_fleet": bool(review.get("outside_fleet")),
                    "what_happened": review["what_happened"],
                    "symptoms": [],
                    "mitigations": [m for m in review["mitigations"] if m["object"] == symptom["object"]],
                    "guards": [g for g in review["guards"] if g["object"] == symptom["object"]],
                }
            incident["symptoms"].append(symptom)
            if symptom.get("recreated_only"):
                incident["recreated_only"] = True
        upgrade_context = upgraded or bool(review["what_happened"]["operations"])
        for incident in incidents.values():
            # A symptom that predates the window's operations, or that the
            # previous full run recorded, is reported but never as the
            # upgrade's Error; the check and the severity come from the one
            # rule the guards record too.
            incident["check"], incident["severity"], incident["predates_upgrade"] = _symptom_group_check(incident["symptoms"], upgrade_context)
            incident["entries"] = _entries_label([c for s in incident["symptoms"] for c in s["classifications"]])
        for op in review["what_happened"]["operations"]:
            if _operation_failed(op):
                # Its own kind, keyed by the operation's name: two failed
                # operations on one pool are two findings, a retry a new one.
                obj = f"{OPERATION_OBJECT_KIND}/{op['name'] or op['type']}"
                incidents[obj] = {
                    "kind": INCIDENT_OPERATION,
                    "severity": SEVERITY_ERROR,
                    "cluster": review["cluster"],
                    "object": obj,
                    "system": False,
                    "entries": "operation " + (op["status"] or "?"),
                    "what_happened": review["what_happened"],
                    "operation": op,
                    "symptoms": [],
                    "mitigations": [],
                    "guards": [g for g in review["guards"] if g["object"] == obj],
                }
        if review["reviewed"] or review.get("partial") and review["answered"]:
            # Every reviewed cluster gets an Info block: its next upgrade,
            # the risks present and the baseline recorded. A cluster none of
            # whose reads answered is not clean; Info names it under the failed reads.
            clean.append({
                "cluster": review["cluster"],
                "what_happened": review["what_happened"],
                "incidents": len(incidents),
                "partial": review.get("partial") or [],
                "outside_fleet": bool(review.get("outside_fleet")),
                "starting": review.get("starting") or [],
                "gated": review.get("gated") or [],
                "terminating": review.get("terminating") or [],
                "skew_notes": review.get("skew_notes") or [],
                "next_upgrade": review["next_upgrade"],
                "shapes": review["shapes"],
                "managed_agents": review["managed_agents"],
                "baseline": review["baseline"],
            })
        for incident in sorted(incidents.values(), key=lambda i: i["object"]):
            (errors if incident["severity"] == SEVERITY_ERROR else warnings).append(incident)
    # The reads a stale guard could not be re-observed through: a partial
    # review's core reads, or a reviewed cluster's failed shape read (an
    # entry-3 guard needs `workloads`).
    read_names = {name for name, _ in KUBECTL_READS}
    partial_reads = {r["cluster"]: r["partial"] for r in reviews if r.get("partial") and not r["reviewed"]}
    for r in reviews:
        if r["reviewed"]:
            failed = [e.split(":")[0] for e in r.get("read_errors") or [] if e.split(":")[0] in read_names]
            if failed:
                partial_reads[r["cluster"]] = failed
    upgrading_keys = {row["cluster"] for row in upgrading or []}
    recheck_errors = {r["cluster"]: r["errors"] for r in rechecks or [] if r.get("errors")}
    # A review that reached no read at all: the credentials failed, or the
    # review raised after the reads answered. Its error is the reason the
    # guards were kept, not the five read names `partial` lists for it.
    unread_errors = {r["cluster"]: list(r.get("read_errors") or []) for r in reviews if r.get("partial") and not r["reviewed"] and not r["answered"]}
    for r in rechecks or []:
        if r.get("deferred"):
            unread_errors[r["cluster"]] = list(r.get("errors") or [])
    stale: dict[str, list[dict]] = {}
    for guard in guards:
        # A risk is never an incident, so only a failure guard that outlived
        # its last review is a Warning; stale risk guards stay in the file,
        # and a scoped run says nothing about clusters outside its scope.
        if in_scope is not None and guard.get("cluster") not in in_scope:
            continue
        if guard.get("cluster") in (unlisted or set()):
            # Its project's listing failed this run: the gap is under "Reads
            # that failed", not a stale Warning per guard.
            continue
        if guard.get("cluster") in upgrading_keys:
            # Its upgrade is still running: the cluster is under "Upgrading
            # now" and is reviewed next run, guards included.
            continue
        if guard.get("entry") == ENTRY_OPERATION and str(guard.get("cluster") or "").split(CLUSTER_KEY_SEPARATOR)[0] in (listing_unread or set()):
            # Its project's operations listing failed this run: held, the
            # gap under "Reads that failed" and the check unevaluated.
            continue
        if guard.get("last_seen") != seen_at and guard.get("kind", GUARD_KIND_FAILURE) == GUARD_KIND_FAILURE:
            stale.setdefault(guard["cluster"], []).append(guard)
    for cluster, held in stale.items():
        warnings.extend(fold_held_guards(cluster, held, partial_reads.get(cluster) or [], recheck_errors.get(cluster) or [], unread_errors.get(cluster) or []))
    errors.sort(key=_incident_key)
    warnings.sort(key=_incident_key)
    return {
        "errors": errors,
        "warnings": warnings,
        "info": {"clean": clean, "unchanged": unchanged, "failed_reads": failed_reads, "removed": list(removed or []), "rechecked": list(rechecks or []), "upgrading": list(upgrading or []), "scoped": scope_reason, "operation_guards": operation_guards or {"refreshed": [], "cleared": [], "held": [], "unlisted": []}},
    }


def fold_held_guards(cluster: str, held: list[dict], partial: list[str] | None = None, recheck_errors: list[str] | None = None, unread_errors: list[str] | None = None) -> list[dict]:
    """Failure guards held on a cluster this run did not re-observe, as one
    stale-guard incident per object whatever the number of entries its
    guards carry: a finding is identified by check, cluster, namespace and
    object, so two guards on one object are one finding. `unread_errors`
    is why a review reached no read (the credentials, a raise); `partial`
    the reads that failed on one that did."""
    by_object: dict[str, dict] = {}
    for guard in held:
        incident = by_object.get(guard["object"])
        if incident is None:
            by_object[guard["object"]] = incident = {
                "partial": list(partial or []),
                "recheck_errors": list(recheck_errors or []),
                "unread_errors": list(unread_errors or []),
                "kind": INCIDENT_STALE_GUARD,
                "severity": SEVERITY_WARNING,
                "cluster": cluster,
                "object": guard["object"],
                "system": False,
                "entries": "",
                "what_happened": None,
                "symptoms": [],
                "mitigations": [],
                "guards": [],
            }
        incident["guards"].append(guard)
    for incident in by_object.values():
        incident["guards"].sort(key=lambda g: (g.get("entry") or 0, g["id"]))
        incident["entries"] = _guard_entries_label(incident["guards"])
        incident["check"] = _guard_check(incident["guards"][0])
    return sorted(by_object.values(), key=_incident_key)


def _guard_entries_label(guards: list[dict]) -> str:
    return _entries_label([{"entry": g.get("entry")} for g in guards])


def _operations_lines(wh: dict) -> list[str]:
    if not wh["operations"]:
        return [NO_OPERATION_LINE + "."]
    lines = ["", "| Operation | Target | Start | End | Duration | Status | Error |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for op in wh["operations"]:
        status = _cell(op["status"])
        if op.get("already_reviewed"):
            status += "; " + ALREADY_REVIEWED_FORMAT.format(route=_cell(op["already_reviewed"].get("route")), at=op["already_reviewed"].get("at") or "?")
        lines.append(f"| {_cell(op['type'])} | {_cell(op['target'])} | {op['start'] or '-'} | {op['end'] or '-'} | {_duration(op['duration_s'])} | {status} | {_cell(op['error'] or '')} |")
    return lines


def _what_happened_lines(wh: dict) -> list[str]:
    lines = [f"{PART_WHAT_HAPPENED} {wh['status']} ({_cell('; '.join(wh['reasons']))}); channel {_cell(wh['channel'] or 'none')}; cluster status {_cell(wh['cluster_status'])}.", ""]
    lines += _versions_table(wh["versions_before"], wh["versions_after"])
    lines += _operations_lines(wh)
    return lines


def _cell(text: object) -> str:
    """Tenant text made safe for a Markdown cell, heading or code span."""
    out = CONTROL_CHARS_RE.sub(" ", str(text if text is not None else ""))
    for raw, safe in CELL_ESCAPES:
        out = out.replace(raw, safe)
    return out


def _incident_lines(incident: dict) -> list[str]:
    lines = [f"### {incident['entries']} — {_cell(incident['cluster'])} — `{_cell(incident['object'])}`" + (" (system)" if incident["system"] else ""), ""]
    if incident["kind"] == INCIDENT_STALE_GUARD:
        if incident.get("unread_errors"):
            lines.append(f"{PART_WHAT_HAPPENED} {STALE_UNREAD_TEXT.format(errors=_cell('; '.join(incident['unread_errors'])))}")
        elif incident.get("partial"):
            lines.append(f"{PART_WHAT_HAPPENED} {STALE_PARTIAL_TEXT.format(failed=', '.join(incident['partial']))}")
        elif incident.get("recheck_errors"):
            lines.append(f"{PART_WHAT_HAPPENED} {STALE_RECHECK_FAILED_TEXT.format(errors=_cell('; '.join(incident['recheck_errors'])))}")
        else:
            lines.append(f"{PART_WHAT_HAPPENED} cluster not reviewed this run; the guard below is from an earlier run.")
        for guard in incident["guards"]:
            heading = f"entry {guard['entry']}. {guard['title']}" if guard.get("entry") is not None else UNCLASSIFIED
            lines.append(f"{PART_WHAT_FAILED} {heading} ({guard['confidence']}) last seen {guard['last_seen']}: {_cell(guard['evidence'])}")
        lines.append(f"{PART_MITIGATE} the entry's row applies until the cluster is reviewed again.")
        lines.append(f"{PART_MITIGATION_SET_UP} " + "; ".join(f"guard `{_cell(g['object'])}` entry {_guard_entry_text(g)}, first seen {g['first_seen']}, {int(g.get('reviews') or 1)} review(s), still live" for g in incident["guards"]) + ".")
        return lines + [""]
    lines += _what_happened_lines(incident["what_happened"])
    lines.append("")
    if incident["kind"] == INCIDENT_OPERATION:
        op = incident["operation"]
        lines.append(f"{PART_WHAT_FAILED} {_cell(op['type'])} on {_cell(op['target'])} ended {_cell(op['status'])}: {_cell(op['error'] or 'no error text')}")
        lines.append(f"{PART_MITIGATE} {OPERATION_MITIGATE_TEXT}")
        guards_text = "; ".join(OPERATION_GUARD_FORMAT.format(id=_cell(g["id"]), first_seen=g["first_seen"], reviews=int(g.get("reviews") or 1), type=_cell(op["type"]), target=_cell(op["target"])) for g in incident["guards"]) or OPERATION_NO_GUARD_TEXT
        lines.append(f"{PART_MITIGATION_SET_UP} {guards_text}")
        return lines + [""]
    lines += [PART_WHAT_FAILED, "", "| Symptom | Since | Catalogue entry | Confidence | Evidence |", "| --- | --- | --- | --- | --- |"]
    for symptom in incident["symptoms"]:
        for c in symptom["classifications"]:
            entry = f"{c['entry']}. {c['title']}" if c["entry"] else UNCLASSIFIED
            if c.get("detail"):
                entry += f" ({_cell(c['detail'])})"
            lines.append(f"| {_cell(symptom['reason'])} | {symptom.get('since') or '-'} | {entry} | {c['confidence']} | {_cell(c['evidence'])} |")
    if incident.get("predates_upgrade"):
        lines += ["", PREDATES_UPGRADE_TEXT + "."]
    if incident.get("recreated_only"):
        lines += ["", RECREATED_TEXT]
    lines += ["", PART_MITIGATE]
    if not incident["mitigations"]:
        lines.append(UNCLASSIFIED_MITIGATION_TEXT)
    for m in incident["mitigations"]:
        lines.append(f"- **{m['entry']}. {m['title']}** — {_cell(m['before_signal'])} Read today: {m['read_today']}. Mitigate before: {m['mitigate_before']} Mitigate after: {m['mitigate_after']}")
    lines += ["", PART_MITIGATION_SET_UP]
    if not incident["guards"]:
        lines.append(OUTSIDE_FLEET_GUARD_TEXT if incident.get("outside_fleet") else NO_GUARD_TEXT)
    for g in incident["guards"]:
        lines.append(f"- guard `{_cell(g['id'])}` {g['kind']} entry {_guard_entry_text(g)} ({g['confidence']}), first seen {g['first_seen']}, {int(g.get('reviews') or 1)} review(s)")
    return lines + [""]


def _next_upgrade_lines(nu: dict) -> list[str]:
    standing = "unknown" if nu["below_target"] is None else ("behind the target" if nu["below_target"] else "at or ahead of the target")
    exclusions = "; ".join(f"{_cell(e['name'])} ({_cell(e['scope'])}) until {e['end'] or '?'}{' [active]' if e['active'] else ''}" for e in nu["exclusions"]) or "none"
    return [
        f"{PART_NEXT_UPGRADE} channel {_cell(nu['channel'] or 'none')}; target {_cell(nu['target'] or 'unknown')}; cluster at {_cell(nu['current'])}, {standing}. "
        f"Window: {nu['window']}; next opens {nu['next_opens'] or 'unknown'}. Exclusions: {exclusions}."
    ]


def _risks_lines(shapes: list[dict], managed_agents: int, failed_reads: list[str] | None = None) -> list[str]:
    agents = [MANAGED_AGENTS_LINE.format(count=managed_agents)] if managed_agents else []
    if not shapes:
        failed = {e.split(":")[0] for e in failed_reads or []}
        checked = [text for text, reads in SHAPE_CHECKS if not (set(reads) & failed)]
        skipped = [text for text, reads in SHAPE_CHECKS if set(reads) & failed]
        line = f"{PART_RISKS} {NO_SHAPE_TEXT}{'; '.join(checked)}"
        if skipped:
            line += NOT_CHECKED_TEXT + "; ".join(skipped)
        return [line + "."] + agents
    lines = [PART_RISKS, "", "| Object | Catalogue entry | Confidence | Evidence | Mitigate before |", "| --- | --- | --- | --- | --- |"]
    for shape in shapes:
        system = " (system)" if shape["system"] else ""
        lines.append(f"| `{_cell(shape['object'])}`{system} | {shape['entry']}. {shape['title']} | {shape['confidence']} | {_cell(shape['evidence'])} | {_cell(MITIGATIONS[shape['entry']]['mitigate_before'])} |")
    return lines + ([""] + agents if agents else [])


def _baseline_lines(baseline: dict | None) -> list[str]:
    if not baseline:
        return [f"{PART_BASELINE} {NONE_LINE}"]
    versions = baseline["versions"]
    pools = ", ".join(f"{_cell(p)} {_cell(v)}" for p, v in sorted(versions["node_pools"].items()))
    failed = f" Shape reads that failed: {'; '.join(baseline['shape_reads_failed'])}." if baseline["shape_reads_failed"] else ""
    symptoms = f" Symptom baseline recorded: {baseline.get('symptom_count', 0)} symptom(s), {baseline.get('first_seen', 0)} first seen."
    if baseline.get("first_run"):
        symptoms += " " + FIRST_RUN_GRADING_TEXT
    lines = [f"{PART_BASELINE} control plane {versions['control_plane']}; pools {pools or 'none'}; {baseline['pods']} pods, {baseline['budgets']} budgets, {baseline['shapes']} shapes.{failed}{symptoms} Guards written: {len(baseline['guards'])}."]
    for gid in baseline["guards"]:
        lines.append(f"- `{_cell(gid)}`")
    return lines


def _cluster_block_lines(row: dict) -> list[str]:
    standing = "clean" if not row["incidents"] else f"{row['incidents']} incident(s) above"
    if row.get("outside_fleet"):
        standing += f"; {OUTSIDE_FLEET_TEXT}"
    lines = [f"### {_cell(row['cluster'])} — {standing}", ""]
    lines += _what_happened_lines(row["what_happened"])
    lines.append("")
    if row.get("partial"):
        lines += [PARTIAL_READ_TEXT.format(failed=", ".join(row["partial"])), ""]
    if row.get("starting"):
        lines += [STARTING_TEXT.format(count=len(row["starting"]), minutes=int(STARTING_GRACE.total_seconds() // SECONDS_PER_MINUTE), pods=", ".join(_cell(p) for p in row["starting"])), ""]
    if row.get("gated"):
        lines += [GATED_TEXT.format(count=len(row["gated"]), pods=", ".join(_cell(p) for p in row["gated"])), ""]
    if row.get("terminating"):
        lines += [TERMINATING_TEXT.format(count=len(row["terminating"]), pods=", ".join(_cell(p) for p in row["terminating"])), ""]
    for note in row.get("skew_notes") or []:
        lines += [_cell(note), ""]
    lines += _next_upgrade_lines(row["next_upgrade"])
    lines.append("")
    lines += _risks_lines(row["shapes"], row.get("managed_agents") or 0, list(row.get("partial") or []) + list((row.get("baseline") or {}).get("shape_reads_failed") or []))
    lines.append("")
    lines += _baseline_lines(row["baseline"])
    return lines + [""]


def _incident_check(incident: dict) -> str:
    """The SOP's check id for an incident: the operation that failed, a
    workload or node the upgrade broke (an Error), a guard still live, and
    the three Warning readings of a symptom."""
    if incident["kind"] == INCIDENT_OPERATION:
        return CHECK_OPERATION_FAILED
    if incident["kind"] == INCIDENT_STALE_GUARD:
        return _guard_check(incident["guards"][0])
    if incident.get("check"):
        return str(incident["check"])
    if incident["severity"] == SEVERITY_ERROR:
        return CHECK_NODE_BROKEN if any(s["category"] == CATEGORY_NODE for s in incident["symptoms"]) else CHECK_BROKE_WORKLOAD
    if incident.get("predates_upgrade"):
        return CHECK_SYMPTOM_PREDATES
    if all(c["entry"] is None for s in incident["symptoms"] for c in s["classifications"]):
        return CHECK_SYMPTOM_UNCLASSIFIED
    return CHECK_SYMPTOM_TENTATIVE


def _split_object(obj: str) -> tuple[str, str]:
    """`namespace/Kind/name` -> (namespace, `Kind/name`); a cluster-scoped
    `Kind/name` keeps an empty namespace. The candidate's object is the bare
    resource; the identity tuple already carries the qualified cluster."""
    parts = obj.split("/")
    if len(parts) >= 3:
        return parts[0], "/".join(parts[1:])
    return "", obj


def _incident_candidate(incident: dict, command: str | None) -> dict:
    check = _incident_check(incident)
    if incident["kind"] == INCIDENT_OPERATION:
        op = incident["operation"]
        namespace, obj = "", f"{OPERATION_OBJECT_KIND}/{op.get('name') or op['type']}"
        excerpt = f"{op['type']} on {op['target']} ended {op['status']}: {op['error'] or 'no error text'}"
        capacity_shaped = any(marker in (op.get("error") or "").lower() for marker in CAPACITY_ERROR_MARKERS)
        impact = OPERATION_MITIGATE_TEXT + (f" {MITIGATIONS[ENTRY_CAPACITY]['mitigate_after']}" if capacity_shaped else "")
    elif incident["kind"] == INCIDENT_STALE_GUARD:
        # Every guard on the object in one candidate: one finding per object.
        namespace, obj = _split_object(incident["object"])
        excerpt = "; ".join(f"guard {g['id']} last seen {g['last_seen']}: {g['evidence']}" for g in incident["guards"])
        impact = " ".join(MITIGATIONS[g["entry"]]["mitigate_after"] for g in incident["guards"] if g.get("entry") in MITIGATIONS)
    else:
        namespace, obj = _split_object(incident["object"])
        excerpt = "; ".join(f"{s['reason']}: {c['evidence']}" for s in incident["symptoms"] for c in s["classifications"])
        impact = " ".join(f"{m['before_signal']} Read today: {m['read_today']}. Mitigate before: {m['mitigate_before']} Mitigate after: {m['mitigate_after']}" for m in incident["mitigations"])
        if incident.get("predates_upgrade"):
            impact = (PREDATES_UPGRADE_TEXT + ". " + impact).strip()
        if incident.get("recreated_only"):
            impact = (RECREATED_TEXT + " " + impact).strip()
        if not impact:
            impact = UNCLASSIFIED_MITIGATION_TEXT
    candidate = {
        "check": check,
        "cluster": incident["cluster"],
        "namespace": namespace,
        "object": obj,
        "severity": MANIFEST_SEVERITY_MAJOR if incident["severity"] == SEVERITY_ERROR else MANIFEST_SEVERITY_MINOR,
        "excerpt": _cell(excerpt)[:MESSAGE_EXCERPT_CHARS],
        # Tenant text reaches `impact` through a classification's detail (a
        # webhook name, an image host), and `draft` copies it into the
        # findings document: escaped as the excerpt is.
        "impact": _cell(impact)[:MANIFEST_IMPACT_CHARS],
        "impact_authoritative": False,
        "needs_triage": None,
        "entries": incident.get("entries"),
    }
    if command:
        candidate["command"] = command
    return candidate


def candidate_objects(candidates: list[dict]) -> set[tuple[str, str]]:
    """The one rule for a finding's identity across runs: one finding per
    object per run, keyed (check, cluster, namespace, object). An object with
    a fresh incident this run gets no held candidate beside it; when the
    incident's check changes, the new candidate carries the new check and
    the old finding is resolved by the absence of a candidate under it."""
    return {(c["namespace"], c["object"]) for c in candidates}


def _held_candidates(cluster: str, guards: list[dict], covered: set[tuple[str, str]], commands: dict[str, str], seen_at: str, unevaluated: set[str] | frozenset[str] = frozenset()) -> tuple[list[dict], list[dict]]:
    """One candidate per object for the failure guards this run observed
    again on a cluster (`last_seen` is this run: re-observed by the re-check
    or kept by a keep rule) whose object has no incident this run
    (`covered`, per `candidate_objects`), under the check the object's
    guards record (one per object, which `merge_guards` keeps; were they to
    disagree, the check of the guard reviewed most), with its persistence;
    and the still-flagged identities beside them. A guard a failed read held
    emits nothing: a candidate with no read behind it would vouch for an
    observation nobody made; its check is unevaluated and the run partial
    instead."""
    groups: dict[str, list[dict]] = {}
    for guard in guards:
        if guard.get("last_seen") != seen_at:
            continue
        if _guard_check(guard) in unevaluated:
            # The check's own read failed this run (a budget kept at zero
            # through the pdbs read while pods failed): no candidate under
            # a check the manifest calls unevaluated.
            continue
        namespace, obj = _split_object(guard["object"])
        if (namespace, obj) in covered:
            continue
        groups.setdefault(guard["object"], []).append(guard)
    candidates, flagged = [], []
    for full_object, held in sorted(groups.items()):
        held.sort(key=lambda g: (-int(g.get("reviews") or 1), g.get("entry") or 0, g["id"]))
        check = _guard_check(held[0])
        held.sort(key=lambda g: (g.get("entry") or 0, g["id"]))
        namespace, obj = _split_object(full_object)
        excerpt = "; ".join(f"guard {g['id']} {HELD_PERSISTS_FORMAT.format(since=g['first_seen'], reviews=int(g.get('reviews') or 1))}: {g['evidence']}" for g in held)
        impact = " ".join(MITIGATIONS[g["entry"]]["mitigate_after"] for g in held if g.get("entry") in MITIGATIONS)
        if not impact:
            impact = OPERATION_MITIGATE_TEXT if any(g.get("entry") == ENTRY_OPERATION for g in held) else UNCLASSIFIED_MITIGATION_TEXT
        candidate = {
            "check": check,
            "cluster": cluster,
            "namespace": namespace,
            "object": obj,
            # A persisting finding is the Warning requirement 3 makes it,
            # under the check id it was first filed with.
            "severity": MANIFEST_SEVERITY_MINOR,
            "excerpt": _cell(excerpt)[:MESSAGE_EXCERPT_CHARS],
            "impact": impact[:MANIFEST_IMPACT_CHARS],
            "impact_authoritative": False,
            "needs_triage": None,
            "entries": _guard_entries_label(held),
            "persistence": [HELD_PERSISTS_FORMAT.format(since=g["first_seen"], reviews=int(g.get("reviews") or 1)) for g in held],
        }
        if commands.get(check):
            candidate["command"] = commands[check]
        candidates.append(candidate)
        flagged.append({"guard": held[0]["id"], "guards": [g["id"] for g in held], "check": check, "cluster": cluster, "namespace": namespace, "object": obj})
    return candidates, flagged


def build_manifest(result: dict, ledger: dict, *, started_at: str, finished_at: str) -> dict:
    """The collector manifest `finish` cross-checks the SOP's document
    against: every enumerated target with an outcome, the commands behind
    each check, a candidate per Error and Warning, and the facts the SOP
    copies rather than re-derives."""
    sections = result["sections"]
    incidents_by_cluster: dict[str, list[dict]] = {}
    for incident in sections["errors"] + sections["warnings"]:
        incidents_by_cluster.setdefault(incident["cluster"], []).append(incident)
    entries: list[dict] = []
    seen: set[str] = set()
    operations_unread = set(result.get("operations_unread") or [])
    listing_errors = {line.split(": ", 1)[0]: line.split(": ", 1)[1] for line in result.get("failed_reads") or [] if ": " in line and line.split(": ", 1)[0] in operations_unread}

    def commands_of(rows: list[dict]) -> list[dict]:
        return [{"check": c["check"], "command": c["command"], "rc": 0} for c in rows]

    guards_by_cluster: dict[str, list[dict]] = {}
    for guard in result.get("guards") or []:
        if guard.get("kind", GUARD_KIND_FAILURE) == GUARD_KIND_FAILURE:
            guards_by_cluster.setdefault(guard["cluster"], []).append(guard)
    still_flagged: list[dict] = []

    def candidates_of(key: str, commands: list[dict], unevaluated: list[dict] | None = None) -> list[dict]:
        # The incidents this run filed, then every failure guard the run
        # observed again on the cluster under its own finding's check (one
        # per check and object), so the harness holds rather than resolves
        # it; none under a check whose read failed this run.
        by_check = {c["check"]: c["command"] for c in commands}
        unevaluated_checks = {u["check"] for u in unevaluated or []}
        # No candidate under a check the entry calls unevaluated, fresh or
        # held: an event row filed while the pods read failed stays a
        # Warning in the report and is filed once the read answers.
        incidents = [i for i in incidents_by_cluster.get(key, []) if i["kind"] != INCIDENT_STALE_GUARD and _incident_check(i) not in unevaluated_checks]
        fresh = [_incident_candidate(i, by_check.get(_incident_check(i))) for i in incidents]
        # The incident's candidate is the object's one candidate this run and
        # carries the object's guard persistence: the merged guard's first-seen
        # date and review count, when the object carried a guard before.
        for candidate in fresh:
            carried = sorted((g for g in guards_by_cluster.get(key, []) if _split_object(g["object"]) == (candidate["namespace"], candidate["object"]) and g.get("first_seen") != result["generated_at"]), key=lambda g: (str(g.get("entry") or ""), g["id"]))
            if carried:
                candidate["persistence"] = [HELD_PERSISTS_FORMAT.format(since=g["first_seen"], reviews=int(g.get("reviews") or 1)) for g in carried]
                candidate["excerpt"] = _cell(f"{candidate['excerpt']}; {'; '.join(candidate['persistence'])}")[:MESSAGE_EXCERPT_CHARS]
        covered = candidate_objects(fresh)
        held, flagged = _held_candidates(key, guards_by_cluster.get(key, []), covered, by_check, result["generated_at"], {u["check"] for u in unevaluated or []})
        still_flagged.extend(flagged)
        return fresh + held

    for review in result["reviews"]:
        key = review["cluster"]
        seen.add(key)
        entry = {"name": key, "project": review["project"], "location": review["location"]}
        commands = commands_of(review.get("commands") or [])
        if review.get("outside_fleet"):
            entry.update(outcome=MANIFEST_OUTCOME_OUT_OF_SCOPE, error=OUTSIDE_FLEET_TEXT)
        elif not review["answered"] and not review["reviewed"] and not review["commands"]:
            # Nothing answered, the project's operations listing included.
            credentials_failed = any(e.startswith("get-credentials") for e in review["read_errors"])
            entry.update(outcome=MANIFEST_OUTCOME_UNREACHABLE if credentials_failed else MANIFEST_OUTCOME_GATE_FAILED, error="; ".join(review["read_errors"])[:ERROR_EXCERPT_CHARS])
        else:
            # The listing that decides the operation check answered even when
            # the cluster's own reads did not (credentials failed): the entry
            # is collected with that one check, the five kubectl-backed ones
            # unevaluated, so a failed operation's Error is published.
            failed = {e.split(":")[0] for e in review["read_errors"]} | set(review.get("partial") or [])
            unevaluated = [{"check": check, "reason": f"{read} read failed"} for check, read in CHECK_READS if read in failed or (read == OPERATIONS_READ and review["project"] in operations_unread)]
            entry.update(outcome=MANIFEST_OUTCOME_COLLECTED, commands=commands, candidates=candidates_of(key, commands, unevaluated))
            if unevaluated:
                entry["checks_unevaluated"] = unevaluated
            # A deciding or feeding read that failed is a limitation; the
            # storage, webhook, endpoint-slice and next-upgrade reads cost
            # Info alone (`INFO_ONLY_READS`).
            limiting = [e for e in review["read_errors"] if not info_only_error(e)]
            if review["project"] in operations_unread:
                # The project's listing failed: the operation check is
                # unevaluated above, and the listing is the limitation.
                limiting.append(listing_errors.get(review["project"]) or f"{OPERATIONS_READ} list failed")
            if limiting:
                entry["limitations"] = "reads that failed: " + "; ".join(limiting)
        wh = review["what_happened"]
        entry["facts"] = {
            "status": wh["status"],
            "reasons": wh["reasons"],
            "versions_before": wh["versions_before"],
            "versions_after": wh["versions_after"],
            "operations": wh["operations"],
            "incident_kinds": sorted({i["kind"] for i in incidents_by_cluster.get(key, [])}),
            "next_upgrade": review.get("next_upgrade"),
            "guards_written": [g["id"] for g in review.get("guards") or []],
        }
        entries.append(entry)
    for row in result["unchanged"]:
        key = row["cluster"]
        seen.add(key)
        project, location, _ = key.split(CLUSTER_KEY_SEPARATOR, 2)
        commands = commands_of(row.get("commands") or [])
        entry = {
            "name": key, "project": project, "location": location,
            "outcome": MANIFEST_OUTCOME_COLLECTED, "commands": commands, "candidates": candidates_of(key, commands, row.get("checks_unevaluated")),
            "facts": {"status": "unchanged", "control_plane": row["control_plane"], "last_operation": row.get("last_operation"), "next_upgrade": row.get("next_upgrade"), "last_run": row.get("last_run")},
        }
        # A re-check whose read failed: the checks it backs are unevaluated
        # and the read is a limitation, as on a review.
        if row.get("checks_unevaluated"):
            entry["checks_unevaluated"] = row["checks_unevaluated"]
        if row.get("limitations"):
            entry["limitations"] = row["limitations"]
        entries.append(entry)
    for row in result["upgrading"]:
        key = row["cluster"]
        seen.add(key)
        project, location, _ = key.split(CLUSTER_KEY_SEPARATOR, 2)
        # Mid-window the run read nothing on it: a skipped target, with the
        # operation in flight as the error, so `finish` holds the stream's
        # findings on it rather than resolving them. No candidate: a
        # candidate with no read behind it would vouch for nothing.
        entries.append({"name": key, "project": project, "location": location, "outcome": MANIFEST_OUTCOME_GATE_FAILED, "error": _upgrading_line(row, str), "facts": {"status": "upgrading", "operation": row["operation"]}})
    # A declared project the install could not read (`--scope-unread`): its
    # known clusters are unreachable, held, with the outcome the tool gave.
    unread_outcomes = dict(result.get("scope_unread") or [])
    for key, project in sorted((result.get("unread_clusters") or {}).items()):
        if key in seen:
            continue
        seen.add(key)
        _, location, _ = key.split(CLUSTER_KEY_SEPARATOR, 2)
        entries.append({"name": key, "project": project, "location": location, "outcome": MANIFEST_OUTCOME_UNREACHABLE, "error": UNREAD_PROJECT_TEXT.format(cluster=key, project=project, outcome=unread_outcomes.get(project, fleet_scope_args.SCOPE_UNREAD_DEFAULT_OUTCOME)).split(": ", 1)[-1][:ERROR_EXCERPT_CHARS]})
    # A ledger project outside the handed or listed set on a full run whose
    # set is not the fleet: held as an unread one is, with its reason.
    for key, reason in sorted((result.get("held_clusters") or {}).items()):
        if key in seen:
            continue
        seen.add(key)
        project, location, _ = key.split(CLUSTER_KEY_SEPARATOR, 2)
        entries.append({"name": key, "project": project, "location": location, "outcome": MANIFEST_OUTCOME_UNREACHABLE, "error": reason[:ERROR_EXCERPT_CHARS]})
    # A cluster whose status rules out reading it: listed, held with the status.
    for key, reason in sorted((result.get("not_running") or {}).items()):
        if key in seen:
            continue
        seen.add(key)
        project, location, _ = key.split(CLUSTER_KEY_SEPARATOR, 2)
        entries.append({"name": key, "project": project, "location": location, "outcome": MANIFEST_OUTCOME_GATE_FAILED, "error": reason[:ERROR_EXCERPT_CHARS]})
    # Clusters the run's deadline left unread: held, with the budget as the reason.
    for key in result.get("deferred") or []:
        if key in seen:
            continue
        seen.add(key)
        project, location, _ = key.split(CLUSTER_KEY_SEPARATOR, 2)
        entries.append({"name": key, "project": project, "location": location, "outcome": MANIFEST_OUTCOME_GATE_FAILED, "error": BUDGET_SPENT_REASON.format(seconds=RUN_DEADLINE_S)})
    for line in result["failed_reads"]:
        key, _, reason = line.partition(": ")
        if key in seen or key.count(CLUSTER_KEY_SEPARATOR) != 2 or not reason.startswith("its project's listing failed"):
            continue
        seen.add(key)
        project, location, _ = key.split(CLUSTER_KEY_SEPARATOR, 2)
        entries.append({"name": key, "project": project, "location": location, "outcome": MANIFEST_OUTCOME_GATE_FAILED, "error": reason[:ERROR_EXCERPT_CHARS]})
    # A project whose operations listing failed and that no entry carries
    # (the after-upgrade wake lists nothing else): its own gate-failed entry,
    # so the manifest says the wake was blind rather than idle.
    carried = {e.get("project") for e in entries}
    blind = sorted(project for project in operations_unread if project not in carried)
    for project in blind:
        entries.append({"name": PROJECT_TARGET_PREFIX + project, "project": project, "location": UNENUMERATED_TARGET_LOCATION, "outcome": MANIFEST_OUTCOME_GATE_FAILED, "error": (listing_errors.get(project) or OPERATIONS_LIST_FAILED_TEXT)[:ERROR_EXCERPT_CHARS]})
    manifest = {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
        "audit": AUDIT_ID,
        "started_at": started_at,
        "finished_at": finished_at,
        "scoped": result["scoped"],
        AFTER_RESET_KEY: bool(result.get(AFTER_RESET_KEY)),
        "partial": any(run_limiting_failure(line) for line in result["failed_reads"]) or any(r.get("partial") for r in result["reviews"]) or bool(result["upgrading"]) or bool(result.get("scope_note")) or bool(blind),
        "clusters": entries,
        "still_flagged": sorted(still_flagged, key=lambda h: (h["cluster"], h["check"], h["namespace"], h["object"])),
        "fleet": result.get("fleet") or [],
    }
    if not entries:
        manifest["error"] = "the run enumerated no cluster: " + ("; ".join(result["failed_reads"]) or "no project named a cluster")
    if result.get("scope_note"):
        manifest["limitations"] = result["scope_note"]
        # The declared projects the run did not read, or the unresolved row:
        # the siblings' target, so the gap reaches `finish` through
        # `scope.skipped` even when no known cluster carries it.
        entries.append({"name": UNENUMERATED_PROJECTS_TARGET, "project": "", "location": UNENUMERATED_TARGET_LOCATION, "outcome": MANIFEST_OUTCOME_GATE_FAILED, "error": result["scope_note"][:ERROR_EXCERPT_CHARS]})
    return manifest


def _upgrading_line(row: dict, cell: Callable[[object], str]) -> str:
    """A cluster held back: its operation still running, or, on the
    after-upgrade route, ended too recently for its pods to have settled."""
    op = row["operation"]
    if row.get("settling"):
        line = SETTLING_LINE.format(cluster=cell(row["cluster"]), operation=cell(op["type"]), target=cell(op["target"]), end=op["end"] or "?", minutes=int(AFTER_UPGRADE_SETTLE.total_seconds() // SECONDS_PER_MINUTE))
        if row.get("waiting"):
            line += SETTLED_WAITING_FORMAT.format(count=row["waiting"])
        return line
    return UPGRADING_LINE.format(cluster=cell(row["cluster"]), operation=cell(op["type"]), target=cell(op["target"]), start=op["start"] or "?")


def render_report(result: dict) -> str:
    generated = parse_ts(result["generated_at"]) or now_utc()
    sections = result["sections"]
    lines = [REPORT_TITLE.format(date=generated.strftime(REPORT_DATE_FORMAT)), ""]
    lines.append(f"Window since {result['since']}; projects: {', '.join(result['projects']) or 'none'}; generated {result['generated_at']}.")
    lines.append("")
    for heading, incidents in ((SECTION_ERRORS, sections["errors"]), (SECTION_WARNINGS, sections["warnings"])):
        lines += [heading, ""]
        if not incidents:
            lines += [NONE_LINE, ""]
        for incident in incidents:
            lines += _incident_lines(incident)
    info = sections["info"]
    lines += [SECTION_INFO, ""]
    if info.get("scoped"):
        lines += [INFO_SCOPED.format(reason=_cell(info["scoped"])), ""]
    if result.get("scope_note"):
        lines += [INFO_SCOPE_NOTE.format(note=_cell(result["scope_note"])), ""]
    if result.get(AFTER_RESET_KEY):
        lines += [INFO_AFTER_RESET.format(archive=_cell(str(result.get("reset_archive") or "?")), at=_cell(str(result.get("reset_at") or "?"))), ""]
    if result.get("budget_spent"):
        budget = result["budget"]
        lines += [INFO_BUDGET_SPENT.format(seconds=budget["seconds"], reviewed=budget["reviewed"], rechecked=budget["rechecked"], cut=budget["cut"], cut_rechecks=budget["cut_rechecks"], deferred=budget["deferred"], deferred_rechecks=budget["deferred_rechecks"]), ""]
    if not (info["clean"] or info["unchanged"] or info["failed_reads"] or info.get("removed") or info.get("rechecked") or info.get("upgrading")):
        lines += [NONE_LINE, ""]
    # Severity sections keep `_none_`; Info is never empty after a review.
    for row in info["clean"]:
        lines += _cluster_block_lines(row)
    if info["unchanged"]:
        lines += [INFO_UNCHANGED, ""]
        for row in info["unchanged"]:
            nu = row.get("next_upgrade") or {}
            lines.append(f"- {_cell(row['cluster'])} at {_cell(row['control_plane'])}; last upgrade operation {row.get('last_operation') or 'none recorded'}; next target {_cell(nu.get('target') or 'unknown')}{' (behind)' if nu.get('below_target') else ''}; last reviewed {row['last_run'] or 'never'}")
        lines.append("")
    if info.get("upgrading"):
        lines += [INFO_UPGRADING, ""]
        for row in info["upgrading"]:
            lines.append("- " + _upgrading_line(row, _cell))
        lines.append("")
    if info.get("rechecked"):
        lines += [INFO_RECHECKED, ""]
        for r in info["rechecked"]:
            if r.get("deferred"):
                lines.append(f"- {_cell(r['cluster'])}: " + RECHECK_DEFERRED_LINE.format(guards=r["guards"]))
                continue
            errors = f"; reads that failed: {_cell('; '.join(r['errors']))}" if r["errors"] else ""
            held = f"; {RECHECK_HELD_FORMAT.format(count=len(r['held']))}" if r.get("held") else ""
            refreshed = "; symptom set refreshed" if r.get("symptom_baseline") is not None else ""
            lines.append(f"- {_cell(r['cluster'])}: re-checked for {r['guards']} guard(s): {len(r['cleared'])} cleared{held}{refreshed}{errors}")
        lines.append("")
    operation_guards = info.get("operation_guards") or {}
    if any(operation_guards.get(k) for k in ("refreshed", "cleared", "held", "unlisted")):
        lines += [INFO_OPERATION_GUARDS, ""]
        lines.append("- " + OPERATION_GUARDS_SUMMARY_FORMAT.format(refreshed=len(operation_guards.get("refreshed") or []), held=len(operation_guards.get("held") or []), unlisted=len(operation_guards.get("unlisted") or [])))
        for item in operation_guards.get("unlisted") or []:
            lines.append("- " + OPERATION_GUARD_HELD_FORMAT.format(id=_cell(item["id"]), reason=_cell(item["reason"])))
        for item in operation_guards.get("cleared") or []:
            lines.append("- " + OPERATION_GUARD_CLEARED_FORMAT.format(id=_cell(item["id"]), reason=_cell(item["reason"])))
        lines.append("")
    if info.get("removed"):
        lines += [INFO_REMOVED, ""]
        for key in info["removed"]:
            lines.append(f"- {_cell(key)}: no longer listed by its project; its ledger entry and guards were dropped")
        lines.append("")
    if info["failed_reads"]:
        lines += [INFO_FAILED_READS, ""]
        for line in info["failed_reads"]:
            lines.append(f"- {_cell(line)}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The run.
# --------------------------------------------------------------------------- #


def parse_since(text: str | None, now: datetime) -> datetime:
    if not text:
        return now - timedelta(days=DEFAULT_SINCE_DAYS)
    m = SINCE_DAYS_RE.match(text.strip())
    if m:
        return now - timedelta(days=int(m.group(1)))
    parsed = parse_ts(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"--since takes <days>, <days>d or an RFC 3339 timestamp, not {text!r}")
    return parsed


def info_only_error(error: str) -> bool:
    """A read error that costs Info alone: the storage, webhook and
    endpoint-slice reads (the risks table) and the next-upgrade line's."""
    return error.split(":", 1)[0].strip() in INFO_ONLY_READS or error.startswith(NEXT_UPGRADE_ERROR_PREFIX)


def run_limiting_failure(line: str) -> bool:
    """Whether a "Reads that failed" line limits the run: a project's
    listing, a cluster's deciding or feeding read, a re-check that failed. A
    server config (keyed `project/location`), a next-upgrade window or an
    `INFO_ONLY_READS` read costs Info alone."""
    key, sep, error = line.partition(": ")
    if not sep:
        return True
    if key.count(CLUSTER_KEY_SEPARATOR) == 1:
        return False
    if error.startswith(RECHECK_ERROR_PREFIX):
        error = error[len(RECHECK_ERROR_PREFIX):]
    return not info_only_error(error)


def forced_clusters(args: argparse.Namespace) -> set[str]:
    return {c.strip() for c in args.cluster or [] if c.strip()}


def project_override(args: argparse.Namespace) -> str | None:
    """The one project `--project` names, stripped; None when the flag was
    not given, "" when it was given with no value."""
    value = getattr(args, "project", None)
    if value is None:
        return None
    return str(value).strip()


def validate_args(args: argparse.Namespace, now: datetime | None = None) -> None:
    """The flag set refused as a usage error before the lock is taken and
    before a state file is read: a bad hand run is told its flags are wrong
    at once, rather than waiting on another run to be told the lock was in
    the way, and a corrupt ledger is not set aside by a run that was never
    going to proceed. `main` calls it before the lock; `collect` repeats it
    for callers that skip `main`."""
    forced = forced_clusters(args)
    if getattr(args, "probe_lock", False) and any(getattr(args, flag, None) for flag in PROBE_LOCK_EXCLUSIVE):
        raise argparse.ArgumentTypeError(PROBE_LOCK_WITH_OTHERS_TEXT)
    if getattr(args, "reset_ledger", False) and any(getattr(args, flag, None) for flag in RESET_LEDGER_EXCLUSIVE):
        raise argparse.ArgumentTypeError(RESET_LEDGER_WITH_OTHERS_TEXT)
    # The scope the run was handed, read before anything else: a `--project`
    # override outside a declared scope, a declared scope with nothing
    # readable, or the sandbox's scope guard with no flags is refused here.
    declared_scope.set(getattr(args, "scope_projects", None), getattr(args, "scope_unread", None), getattr(args, "scope_exclude_clusters", None))
    project = project_override(args)
    if not (getattr(args, "probe_lock", False) or getattr(args, "reset_ledger", False)):
        if project is not None and not project:
            raise argparse.ArgumentTypeError(fleet_scope_args.EMPTY_PROJECT_OVERRIDE_ERROR)
        if project:
            override_error = declared_scope.override_error(project)
            if override_error:
                raise argparse.ArgumentTypeError(override_error)
        elif declared_scope.declared and not declared_scope.projects:
            raise argparse.ArgumentTypeError(declared_scope.empty_error())
        for key in sorted(forced):
            parts = key.split(CLUSTER_KEY_SEPARATOR)
            if len(parts) != CLUSTER_KEY_PARTS or not all(parts):
                raise argparse.ArgumentTypeError(CLUSTER_SHAPE_TEXT.format(cluster=key))
            # The project a `--cluster` names is admitted by the override's
            # rule: free on an undeclared install, else inside the readable
            # scope; a sweep of a project outside the declared scope is what
            # the scope exists to refuse, whichever flag names it.
            forced_error = declared_scope.override_error(parts[0])
            if forced_error:
                raise argparse.ArgumentTypeError(FORCED_OUTSIDE_SCOPE_TEXT.format(cluster=key, error=forced_error))
            if (parts[0], parts[1], parts[2]) in declared_scope.excluded_clusters:
                raise argparse.ArgumentTypeError(FORCED_EXCLUDED_TEXT.format(cluster=key))
    full = bool(getattr(args, "full", False))
    if full:
        if forced:
            raise argparse.ArgumentTypeError(FULL_WITH_CLUSTER_TEXT)
        if project:
            raise argparse.ArgumentTypeError(FULL_WITH_PROJECT_TEXT)
        if args.since:
            raise argparse.ArgumentTypeError(FULL_WITH_SINCE_TEXT)
    if getattr(args, "after_upgrade", False):
        if full:
            raise argparse.ArgumentTypeError(AFTER_UPGRADE_WITH_FULL_TEXT)
        if forced:
            raise argparse.ArgumentTypeError(AFTER_UPGRADE_WITH_CLUSTER_TEXT)
        if args.since:
            raise argparse.ArgumentTypeError(AFTER_UPGRADE_WITH_SINCE_TEXT)
    if args.report and Path(args.report).name == LATEST_REPORT_LINK:
        raise argparse.ArgumentTypeError(REPORT_IS_LINK_TEXT.format(name=LATEST_REPORT_LINK))
    parse_since(getattr(args, "since", None), now or now_utc())


def _operations_record(previous: list[dict], fresh: list[dict], seen_at: str, route: str, floor: datetime | None) -> list[dict]:
    """The operations a cluster's reviews covered, by id, with the route and
    time that reviewed each; kept while their end is inside the selection
    window, since an older one can no longer be selected."""
    by_id = {o.get("id"): o for o in previous if o.get("id")}
    for op in fresh:
        summary = operation_summary(op)
        if summary["name"] and summary["name"] not in by_id:
            by_id[summary["name"]] = {"id": summary["name"], "type": summary["type"], "target": summary["target"], "end": summary["end"], "reviewed_at": seen_at, "route": route}
    kept = [o for o in by_id.values() if floor is None or (parse_ts(o.get("end")) or floor) >= floor]
    return sorted(kept, key=lambda o: (o.get("end") or "", o["id"]))


def _skews_first_seen(previous: dict | None, symptoms: list[str] | None, seen_at: str) -> dict[str, str]:
    """When each skew key in the stored set was first recorded: kept while
    the key stands, dated this run when new, dropped with the key. Entry 5's
    age (`SKEW_STANDING`)."""
    previous = previous or {}
    marker = BASELINE_SEPARATOR + CATEGORY_VERSION_SKEW + BASELINE_SEPARATOR
    return {key: previous.get(key) or seen_at for key in (symptoms or []) if marker in key}


def ledger_after(ledger: dict, reviews: list[dict], clusters: list[dict], seen_at: str, operations: dict[str, list[dict]] | None = None, removed: set[str] | None = None, skip: set[str] | None = None, refreshed: dict[str, list[str]] | None = None, advance: bool = True, attempted: set[str] | None = None, route: str = ROUTE_FULL, since: datetime | None = None, outside: set[str] | None = None) -> dict:
    """Every enumerated cluster's current versions; `last_run` moves only for
    a cluster this run reviewed in full, so a failed or partial read is
    retried next time (`partial_read` records the attempt and re-selects
    it). `last_operation` is the latest upgrade operation the review saw and
    `operations_reviewed` every one a review covered, with its route; a
    cluster its project no longer lists is dropped. A scoped run
    (`advance=False`) writes only the operations reviewed of the clusters it
    reviewed: versions, last_run and the symptom set stay the full run's. A
    cluster outside the fleet (`outside`) gets no versions or symptom set,
    only the operations its review covered: route bookkeeping, so the
    after-upgrade route does not review the same operation on every wake."""
    entries = {k: dict(v) for k, v in (ledger.get("clusters") or {}).items() if k not in (removed or set())}
    reviewed = {r["cluster"] for r in reviews if r["reviewed"]} - (skip or set())
    recorded_in_full = reviewed - (outside or set())

    def floor_of(key: str) -> datetime | None:
        last = parse_ts((entries.get(key) or {}).get("last_run"))
        return min(since, last) if since and last else since

    partial = {r["cluster"] for r in reviews if r["partial"] and not r["reviewed"]} - (skip or set()) - (outside or set())
    if not advance:
        # A partial read is still recorded as one, so the next full run
        # re-selects the cluster whichever route attempted it.
        for key in sorted(partial):
            entries.setdefault(key, {})["partial_read"] = seen_at
        for key in sorted(reviewed):
            old = entries.setdefault(key, {})
            old[LEDGER_OPERATIONS_KEY] = _operations_record(old.get(LEDGER_OPERATIONS_KEY) or [], (operations or {}).get(key) or [], seen_at, route, floor_of(key))
            old.pop("partial_read", None)
        return {"version": LEDGER_VERSION, "updated_at": seen_at, LEDGER_PROJECTS_KEY: ledger.get(LEDGER_PROJECTS_KEY) or [], "clusters": entries}
    baselines = {r["cluster"]: r.get("symptom_baseline") or [] for r in reviews}
    for key, symptoms in (refreshed or {}).items():
        if key in entries:
            entries[key]["symptoms"] = symptoms
            entries[key]["symptoms_refreshed"] = seen_at
            entries[key][LEDGER_SKEW_FIRST_SEEN_KEY] = _skews_first_seen(entries[key].get(LEDGER_SKEW_FIRST_SEEN_KEY), symptoms, seen_at)
    for cluster in clusters:
        key = cluster_key(cluster["project"], cluster["location"], cluster["name"])
        if key in (skip or set()) or key in (outside or set()):
            # Upgrading now, or outside the fleet: no versions, no symptom set.
            continue
        current = versions_of(cluster)
        old = entries.get(key) or {}
        if old and key not in recorded_in_full and key not in (attempted or set()):
            # Unchanged: the versions and channel are what the listing says
            # now (a pool added or removed is recorded); last_run and the
            # symptom set stay. A selected cluster whose review did not
            # complete keeps its pre-upgrade versions, so the retry still
            # diffs before and after.
            old.update(control_plane=current["control_plane"], node_pools=current["node_pools"], channel=current["channel"])
            if old.get(LEDGER_OPERATIONS_KEY):
                old[LEDGER_OPERATIONS_KEY] = _operations_record(old[LEDGER_OPERATIONS_KEY], [], seen_at, route, floor_of(key))
        if key in recorded_in_full or not old:
            starts = [op.get("startTime") for op in (operations or {}).get(key) or [] if op.get("startTime")]
            latest = parse_ts(max(starts)) if starts else None
            covered = (operations or {}).get(key) or [] if key in recorded_in_full else []
            entries[key] = {
                "control_plane": current["control_plane"],
                "node_pools": current["node_pools"],
                "channel": current["channel"],
                "first_seen": old.get("first_seen") or seen_at,
                "last_run": seen_at if key in recorded_in_full else old.get("last_run"),
                "last_operation": fmt_ts(latest) if latest else old.get("last_operation"),
                "symptoms": baselines.get(key) if key in recorded_in_full else old.get("symptoms"),
                LEDGER_SKEW_FIRST_SEEN_KEY: _skews_first_seen(old.get(LEDGER_SKEW_FIRST_SEEN_KEY), baselines.get(key) if key in recorded_in_full else old.get("symptoms"), seen_at),
                LEDGER_OPERATIONS_KEY: _operations_record(old.get(LEDGER_OPERATIONS_KEY) or [], covered, seen_at, route, floor_of(key)),
            }
        if key in partial:
            entries.setdefault(key, {})["partial_read"] = seen_at
        elif key in recorded_in_full:
            entries[key].pop("partial_read", None)
    return {"version": LEDGER_VERSION, "updated_at": seen_at, LEDGER_PROJECTS_KEY: ledger.get(LEDGER_PROJECTS_KEY) or [], "clusters": entries}

def _listing_error(project: str, failed_reads: list[str]) -> str:
    """The error `project`'s own `clusters list` line under Reads that failed
    carries, for the line a cluster of an unlisted project gets."""
    line = next((e for e in failed_reads if e.startswith(f"{project}: {CLUSTERS_LIST_PREFIX}")), f"{project}: {LISTING_FAILED_DEFAULT}")
    return line.split(": ", 1)[-1]


def collect_fleet(args: argparse.Namespace, *, run: RunFn | None = None, now: datetime | None = None, max_workers: int = MAX_WORKERS, discovery: tuple[list[str], list[str], str | None] | None = None, clock: Callable[[], float] = time.monotonic, started: float | None = None) -> dict:
    """The run. `max_workers` sizes every pool the run opens (the server
    configs, the reviews, the re-checks); the default is the figure the
    sibling collectors pin to the credential proxy's admitted count.
    `discovery` is `discover_fleet`'s answer when the caller resolved it
    already (`main` does, before the lock, so an exit line can name the
    projects); without it the run resolves the projects itself. `clock`
    and `started` bound the run: `RUN_DEADLINE_S` after `started` (the
    process's start when `main` calls, this call's otherwise) no further
    cluster is read and the rest are held for the next run."""
    # Resolved at call time, so a test that patches `default_run` is honoured.
    run = run or default_run
    clock_fixed = now is not None  # a caller-fixed clock also stamps the finish
    now = now or now_utc()
    seen_at = fmt_ts(now)
    run_started = clock() if started is None else started

    def budget_left() -> bool:
        return clock() - run_started < RUN_DEADLINE_S

    base_run = run

    def bounded_run(argv: list[str], *, timeout: int = GCLOUD_TIMEOUT_S, env: dict | None = None) -> Run:
        # Every subprocess gets the smaller of its own timeout and the time
        # left under the deadline; one attempted past it fails at once with
        # the budget as its error, which the failed-read rules then carry.
        remaining = RUN_DEADLINE_S - (clock() - run_started)
        if remaining <= 0:
            return Run(argv, TIMEOUT_RC, "", BUDGET_READ_TEXT.format(seconds=RUN_DEADLINE_S), 0.0)
        result = base_run(argv, timeout=min(timeout, remaining), env=env)
        if result.rc == TIMEOUT_RC and remaining < timeout:
            # The deadline fell inside this read: the budget ended it, not
            # the read's own timeout, and the error says so.
            own = result.stderr.strip()
            return result._replace(stderr=BUDGET_CUT_TEXT.format(seconds=RUN_DEADLINE_S, given=round(remaining)) + (f"; {own}" if own else ""))
        return result

    run = bounded_run

    # Refused before any state file is read; `main` refused it before the lock.
    validate_args(args, now)
    since = parse_since(args.since, now)
    ledger_path = Path(args.ledger) if args.ledger else data_dir() / LEDGER_FILENAME
    guards_path = Path(args.guards) if args.guards else data_dir() / GUARDS_FILENAME
    for state_path in (ledger_path, guards_path):
        if not state_path.exists():
            # Records are named after the file they came from, so a --ledger
            # or --guards path finds its own.
            crash_records = sorted(state_path.parent.glob(state_path.name + UNREADABLE_GLOB_SUFFIX))
            if crash_records:
                # A set-aside guards file blocks like a ledger's: restarting
                # with no guards while the ledger says "unchanged" loses them.
                raise StateUnreadable(CRASH_RECORD_TEXT.format(path=crash_records[-1], name=state_path.name))
    # Before a state file is read: a set-aside would otherwise rename through
    # a planted link, and every write below goes under the same root.
    _refuse_symlinked_store_path(data_dir())
    ledger = load_json(ledger_path, empty_ledger(), version=LEDGER_VERSION, now=now, move_aside=not args.dry_run)
    if ledger_path.exists() and not guards_path.exists():
        # The write order never leaves this (guards, then ledger, every run):
        # a hand delete did, and the guards are the memory the hold rule reads.
        raise StateUnreadable(GUARDS_ABSENT_TEXT.format(guards=guards_path, ledger=ledger_path, reports=REPORTS_SUBDIR))
    guards = load_json(guards_path, empty_guards(), version=GUARDS_VERSION, now=now, move_aside=not args.dry_run)
    reset_marker_path = data_dir() / RESET_MARKER_FILENAME
    reset_marker = read_reset_marker(reset_marker_path)
    after_reset = reset_marker is not None
    forced = forced_clusters(args)
    after_upgrade = bool(getattr(args, "after_upgrade", False))
    if after_upgrade and not any((entry or {}).get("last_run") for entry in (ledger.get("clusters") or {}).values()):
        # No full run has recorded the fleet: nothing to diff against.
        log(NO_BASELINE_TEXT)
        return {"generated_at": seen_at, "since": fmt_ts(since), "projects": [], "reviews": [], "unchanged": [], "upgrading": [], "failed_reads": [], "scoped": True, "after_upgrade": True, "scope_reason": AFTER_UPGRADE_SCOPE_TEXT, "quiet": True, "no_baseline": True, "report": NO_BASELINE_TEXT + "\n", "guards": guards.get("guards") or [], "dry_run": bool(args.dry_run), AFTER_RESET_KEY: after_reset}

    failed_reads: list[str] = []
    projects, failed_reads, listing_note = discovery if discovery is not None else discover_fleet(project_override(args) or None, run=run, forced=forced)
    failed_reads = list(failed_reads)
    projects = sorted(set(projects))
    # The declared projects the install could not read: not swept, their
    # known clusters this run's gap, held unchanged in the ledger and the
    # guards, and the unread note the manifest's top-level limitation.
    unread_outcomes = {p: outcome for p, outcome in declared_scope.unread if p != fleet_scope_args.UNRESOLVED_SCOPE_ROW}
    # The tool's unresolved row: not a project, and the one `--scope-unread`
    # entry under which the handed set is not the fleet (`sweep_is_declared`).
    unresolved_outcome = next((outcome for p, outcome in declared_scope.unread if p == fleet_scope_args.UNRESOLVED_SCOPE_ROW), None)
    # The gap under which the handed or listed set is not the fleet: the
    # tool's unresolved row, or an undeclared install's incomplete listing.
    scope_note = declared_scope.note() or listing_note
    if forced:
        projects = sorted(set(projects) | {c.split(CLUSTER_KEY_SEPARATOR)[0] for c in forced})

    clusters: list[dict] = []
    operations: list[dict] = []
    # Projects whose listing answered in full, with the clusters they hold:
    # a ledger entry or guard for a cluster missing from one is history.
    listed_projects: dict[str, set[str]] = {}
    # The operations listing as it ran, per project that answered: the command
    # behind every check on an unchanged cluster and the operation check on a
    # reviewed one. A project whose listing failed vouches for no check: its
    # clusters carry no operation entry, and the SOP names the gap.
    operations_commands: dict[str, str] = {}
    operations_unread: set[str] = set()
    from collect import AUDITABLE_STATUSES  # noqa: PLC0415 -- the siblings' statuses a cluster can be read in

    not_running: dict[str, str] = {}
    for project in projects:
        found, list_error = enumerate_clusters(project, run=run)
        error = list_error
        if error:
            failed_reads.append(f"{project}: {error}")
        for c in found:
            status = c.get("status")
            if status and status not in AUDITABLE_STATUSES:
                key = cluster_key(project, c["location"], c["name"])
                not_running[key] = NOT_RUNNING_REASON_FORMAT.format(status=status)
                failed_reads.append(NOT_RUNNING_TEXT.format(cluster=key, status=status))
            else:
                clusters.append(c)
        known = [parse_ts((ledger.get("clusters") or {}).get(cluster_key(project, c["location"], c["name"]), {}).get("last_run")) for c in found]
        # The listing reaches every operation guard of the project, so a
        # failed operation older than the window is still re-checked.
        guarded = [parse_ts(g.get(OPERATION_GUARD_START_KEY)) for g in guards.get("guards") or [] if g.get("entry") == ENTRY_OPERATION and str(g.get("cluster") or "").split(CLUSTER_KEY_SEPARATOR)[0] == project]
        earliest = min([since] + [k for k in known if k] + [g for g in guarded if g])
        ops, error = list_operations(project, earliest, run=run)
        if error:
            failed_reads.append(f"{project}: {error}")
            operations_unread.add(project)
        else:
            operations_commands[project] = command_text(operations_argv(project, earliest))
        for op in ops:
            op["project"] = project
        operations.extend(ops)
        if not list_error:
            listed_projects[project] = {cluster_key(project, c["location"], c["name"]) for c in found}
    # What the listings returned, a partial one's clusters included: those
    # are reviewed, and never also held as their project's unlisted ones.
    enumerated_keys = {cluster_key(c["project"], c["location"], c["name"]) for c in clusters}
    if forced:
        clusters = [c for c in clusters if cluster_key(c["project"], c["location"], c["name"]) in forced]
        # Against the listing before the status filter: a forced cluster the
        # listing returned outside AUDITABLE_STATUSES has its status line
        # already, and is not also said to be unlisted.
        missing = forced - enumerated_keys - set(not_running)
        for key in sorted(missing):
            project = key.split(CLUSTER_KEY_SEPARATOR)[0]
            if project in listed_projects:
                failed_reads.append(f"{key}: named by --cluster but not listed in its project")
            else:
                # A project the run could not list, or not wholly, says
                # nothing of the cluster: the line is the listing's failure,
                # as a ledger cluster of an unlisted project gets on a full
                # run, so the manifest carries the cluster gate-failed with it.
                failed_reads.append(LISTING_FAILED_TEXT.format(cluster=key, error=_listing_error(project, failed_reads)))

    selected, unchanged, upgrading = select_clusters(clusters, ledger, operations, since=since, forced=forced, widen=bool(args.since), after_upgrade=after_upgrade, now=now)
    # Reviews in a fixed order, the cluster reviewed longest ago first (one
    # never reviewed before any other), so the tail the deadline cuts rotates.
    selected.sort(key=lambda s: ((((ledger.get("clusters") or {}).get(s.key) or {}).get("last_run")) or "", s.key))
    if after_upgrade:
        # The route reads only the clusters its operations name: nothing else
        # is listed or re-checked, and with nothing to review it prints
        # nothing and writes nothing.
        kept = {s.key for s in selected} | {row["cluster"] for row in upgrading}
        clusters = [c for c in clusters if cluster_key(c["project"], c["location"], c["name"]) in kept]
        unchanged = []
        if not selected and operations_unread:
            # A listing that failed is a blind wake, not a quiet one: the run
            # goes on with nothing to review and writes the failure where the
            # SOP reads it (Reads that failed, the manifest's gate-failed
            # project entry, `partial`).
            log(AFTER_UPGRADE_BLIND_LOG.format(projects=", ".join(sorted(operations_unread))))
        elif not selected:
            # A held cluster is why nothing was written: say so, and name it.
            held_lines = [_upgrading_line(row, str) for row in upgrading]
            for line in [AFTER_UPGRADE_HELD_LOG if held_lines else AFTER_UPGRADE_QUIET_LOG] + held_lines + failed_reads:
                log(line)
            return {"generated_at": seen_at, "since": fmt_ts(since), "projects": projects, "reviews": [], "unchanged": [], "upgrading": upgrading, "failed_reads": failed_reads, "scoped": True, "after_upgrade": True, "scope_reason": AFTER_UPGRADE_SCOPE_TEXT, "quiet": True, "report": "", "guards": guards.get("guards") or [], "dry_run": bool(args.dry_run), AFTER_RESET_KEY: after_reset}
    log(f"{len(clusters)} cluster(s) in {len(projects)} project(s); reviewing {len(selected)}, {len(unchanged)} unchanged, {len(upgrading)} upgrading now")
    # One get-server-config per (project, location), in parallel, for every
    # cluster: the next target is printed for unchanged clusters too.
    locations = sorted({(c["project"], c["location"]) for c in clusters})
    server_configs: dict[tuple[str, str], dict | None] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for (project, location), (config, error) in zip(locations, pool.map(lambda pl: fetch_server_config(pl[0], pl[1], run=run), locations)):
            server_configs[(project, location)] = config
            if error:
                failed_reads.append(f"{project}/{location}: {error}")
    # The shared kubeconfig directory, made usable once before the pools
    # open (`collect.usable_kubeconfig_dir`, the one rule): the first wave's
    # threads would otherwise race one another's move-aside of a directory
    # another uid left, and the loser would report the race as its cluster's
    # error. `fetch_credentials` then finds it this run's and clears a stale
    # file alone.
    if clusters:
        from collect import usable_kubeconfig_dir  # noqa: PLC0415 -- the one rule for the shared directory

        shared_note = usable_kubeconfig_dir(kubeconfig_dir())
        if shared_note:
            log(f"{kubeconfig_dir()}: {shared_note}")

    def review_or_defer(s: Selection) -> dict | None:
        # Checked as each review starts: past the deadline the cluster is
        # held, untouched in the ledger and the guards, for the next run.
        if not budget_left():
            return None
        return _safe_review(s, ledger, run=run, seen_at=seen_at, server_config=server_configs.get((s.cluster["project"], s.cluster["location"])), now=now, operations_command=operations_commands.get(s.cluster["project"]))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        outcomes = list(pool.map(review_or_defer, selected))
    deferred = sorted(s.key for s, outcome in zip(selected, outcomes) if outcome is None)
    read = [s for s, outcome in zip(selected, outcomes) if outcome is not None]
    reviews = [outcome for outcome in outcomes if outcome is not None]
    reviews.sort(key=lambda r: r["cluster"])
    for key in deferred:
        failed_reads.append(BUDGET_SPENT_TEXT.format(cluster=key, seconds=RUN_DEADLINE_S, count=len(reviews)))
    # Only a run invoked with --full is full: it records the fleet's project
    # set and may change it, prunes, writes the dated report and moves the
    # link. Every other run is scoped, whatever its --project set. A roster
    # project whose listing failed is that project's gap, not the run's: its
    # entries and guards stay, its known clusters go under "Reads that failed".
    fleet = set(ledger.get(LEDGER_PROJECTS_KEY) or [])
    scope_reasons = []
    if after_upgrade:
        scope_reasons.append(AFTER_UPGRADE_SCOPE_TEXT)
    elif not getattr(args, "full", False):
        scope_reasons.append(NOT_FULL_TEXT)
    if forced:
        scope_reasons.append("--cluster named " + ", ".join(sorted(forced)))
    if args.since:
        scope_reasons.append("--since widens the window by hand")
    scoped = bool(scope_reasons)
    route = ROUTE_AFTER_UPGRADE if after_upgrade else (ROUTE_SCOPED if scoped else ROUTE_FULL)
    unlisted = {project for project in projects if project not in listed_projects}
    unlisted_clusters = {key for key in (ledger.get("clusters") or {}) if key.split(CLUSTER_KEY_SEPARATOR)[0] in unlisted and key not in enumerated_keys}
    unread_clusters = {key: key.split(CLUSTER_KEY_SEPARATOR)[0] for key in (ledger.get("clusters") or {}) if key.split(CLUSTER_KEY_SEPARATOR)[0] in unread_outcomes}
    for key, project in sorted(unread_clusters.items()):
        failed_reads.append(UNREAD_PROJECT_TEXT.format(cluster=key, project=project, outcome=unread_outcomes[project]))
    # Ledger clusters of a project outside the handed or listed set on a full
    # run whose set is not the fleet: held, each with its reason.
    held_clusters: dict[str, str] = {}
    if scoped:
        new_fleet = fleet
        removed = set()
    else:
        handed = set(projects) | set(unread_outcomes)
        if unresolved_outcome is None and listing_note is None:
            # The scope the run was handed is the fleet it records, the declared
            # projects it could not read included: they are held, not pruned.
            new_fleet = handed
            # A project outside the fleet this run records -- one that left the
            # roster, or one a scoped hand run recorded operations for -- goes
            # with its clusters; a roster project whose listing failed stays.
            removed = {key for key in (ledger.get("clusters") or {}) if key.split(CLUSTER_KEY_SEPARATOR)[0] not in new_fleet}
        else:
            # The handed or listed set is not the fleet: no project is pruned,
            # the ledger's set stands with the projects this run read added (a
            # first run still records what it read, else its reviews would
            # fall outside the fleet and write no guard), and a ledger project
            # outside the set is held as an unread one, with the reason.
            new_fleet = fleet | handed
            removed = set()
            for key in sorted(ledger.get("clusters") or {}):
                project = key.split(CLUSTER_KEY_SEPARATOR)[0]
                if project in handed:
                    continue
                if unresolved_outcome is not None:
                    line = SCOPE_UNRESOLVED_HOLD_TEXT.format(cluster=key, project=project, row=fleet_scope_args.UNRESOLVED_SCOPE_ROW, outcome=unresolved_outcome)
                else:
                    line = LISTING_INCOMPLETE_HOLD_TEXT.format(cluster=key, project=project)
                held_clusters[key] = line.split(": ", 1)[-1]
                failed_reads.append(line)
        # A cluster a successful listing no longer names leaves, whatever the scope's state.
        removed |= {key for project, listed in listed_projects.items() for key in (ledger.get("clusters") or {}) if key.startswith(project + CLUSTER_KEY_SEPARATOR) and key not in listed}
        for key in sorted(unlisted_clusters):
            failed_reads.append(LISTING_FAILED_TEXT.format(cluster=key, error=_listing_error(key.split(CLUSTER_KEY_SEPARATOR)[0], failed_reads)))
    outside = set()
    for review in reviews:
        if review["project"] not in new_fleet:
            # Reviewed and reported, recorded nowhere.
            outside.add(review["cluster"])
            review["outside_fleet"] = True
            review["guards"] = []
            review["symptom_baseline"] = None
            review["what_happened"]["reasons"] = list(review["what_happened"]["reasons"]) + [OUTSIDE_FLEET_TEXT]
    for row in unchanged:
        cluster = next(c for c in clusters if cluster_key(c["project"], c["location"], c["name"]) == row["cluster"])
        window_errors: list[str] = []
        row["next_upgrade"] = _safe_next_upgrade(cluster, server_configs.get((cluster["project"], cluster["location"])), now, window_errors)
        failed_reads.extend(f"{row['cluster']}: {e}" for e in window_errors)
        row["last_operation"] = ((ledger.get("clusters") or {}).get(row["cluster"]) or {}).get("last_operation")

    # A partially read cluster is "reviewed" for the merge so a finding seen
    # again is refreshed; `answered` keeps it from dropping what it could not see.
    reviewed = {r["cluster"] for r in reviews if (r["reviewed"] or r["partial"]) and r["cluster"] not in outside}
    for review in reviews:
        review.setdefault("budgets_at_zero", [])
    answered = {r["cluster"]: set(r["answered"]) for r in reviews}
    fresh_guards = [g for r in reviews for g in r["guards"]]
    # One rule for the review and the re-check: an entry-1 failure guard
    # stays while its budget still allows no disruption, because the drain
    # it held cannot be re-observed without the operation.
    fresh_ids = {g["id"] for g in fresh_guards}
    zero_budgets = {(r["cluster"], obj) for r in reviews for obj in r.get("budgets_at_zero") or []}
    # The same rule for entry 3: a workload still unavailable keeps its guard
    # while the operation its outage fell inside is out of the window.
    unavailable = {(r["cluster"], obj) for r in reviews for obj in r.get("unavailable_workloads") or []}
    # And for entry 5: a pool still skewed keeps its guard through a review
    # under an open window or an active exclusion, which files no row, as
    # the re-check keeps it; the guard clears only when the skew is gone.
    skewed = {(r["cluster"], obj) for r in reviews for obj in r.get("skewed_pools") or []}
    # And for a failure guard whose object the API still shows failing while
    # the review filed nothing for that object and every read the guard
    # needs answered (the re-check's rule): a review's window drops a pod
    # that lost readiness, or a Job pod in phase Failed, before the window's
    # first operation, so the symptom pass does not re-file it, but the
    # failure stands and the guard is held with its candidate. An object the
    # review did file keeps the ordinary rules (its fresh guards replace the
    # old), and a guard a needed read failed for keeps the held path, named
    # with the read that failed; the entries with a rule of their own above
    # (1, 3, 5) and the operation guards are not this rule's.
    failing = {(r["cluster"], obj) for r in reviews for obj in r.get("failing_objects") or []}
    fresh_objects = {(g["cluster"], g["object"]) for g in fresh_guards if g.get("kind", GUARD_KIND_FAILURE) == GUARD_KIND_FAILURE}
    # A kept guard follows the object's fresh guard this run: one object, one
    # check (`merge_guards` holds the same rule for every guard it merges).
    fresh_check_by_object = {}
    for fresh in fresh_guards:
        if fresh.get("kind", GUARD_KIND_FAILURE) == GUARD_KIND_FAILURE and fresh.get("check"):
            fresh_check_by_object.setdefault((fresh["cluster"], fresh["object"]), fresh["check"])
    for guard in guards.get("guards") or []:
        if guard.get("kind", GUARD_KIND_FAILURE) != GUARD_KIND_FAILURE or guard["id"] in fresh_ids:
            continue
        where = (guard.get("cluster"), guard.get("object"))
        reads_answered = answered is None or set(guard.get("reads") or FAILURE_GUARD_READS) <= answered.get(guard.get("cluster"), set())
        still_failing = where in failing and where not in fresh_objects and guard.get("entry") not in (ENTRY_BUDGET, ENTRY_SPREAD, ENTRY_WINDOW, ENTRY_OPERATION) and reads_answered
        if (guard.get("entry") == ENTRY_BUDGET and where in zero_budgets) or (guard.get("entry") == ENTRY_SPREAD and where in unavailable) or (guard.get("entry") == ENTRY_WINDOW and where in skewed) or still_failing:
            kept = {**guard, "last_seen": seen_at}
            if fresh_check_by_object.get(where):
                kept["check"] = fresh_check_by_object[where]
            fresh_guards.append(kept)
    new_guards = merge_guards(guards, fresh_guards, reviewed, seen_at, answered, removed)
    # Unchanged clusters holding live guards: re-read only what the guards
    # need, clear what is gone, refresh what is still there.
    held: dict[str, list[dict]] = {}
    for guard in new_guards["guards"]:
        if guard.get("entry") == ENTRY_OPERATION:
            continue  # re-checked through the listing, below
        held.setdefault(guard["cluster"], []).append(guard)
    by_key = {cluster_key(c["project"], c["location"], c["name"]): c for c in clusters}
    # A full run also refreshes every fleet cluster's stored symptom set, so
    # the next review knows what was there before its upgrade.
    to_recheck = [
        (row["cluster"], by_key[row["cluster"]], held.get(row["cluster"], []), not scoped and row["cluster"].split(CLUSTER_KEY_SEPARATOR)[0] in new_fleet)
        for row in unchanged
        if row["cluster"] in by_key and (row["cluster"] in held or (not scoped and row["cluster"].split(CLUSTER_KEY_SEPARATOR)[0] in new_fleet))
    ]
    # The reviews' order again: the cluster refreshed longest ago first.
    to_recheck.sort(key=lambda item: ((((ledger.get("clusters") or {}).get(item[0]) or {}).get("symptoms_refreshed")) or (((ledger.get("clusters") or {}).get(item[0]) or {}).get("last_run")) or "", item[0]))

    def recheck_or_defer(item: tuple) -> dict | None:
        key, cluster, cluster_guards, refresh = item
        if budget_left():
            return _safe_recheck(key, cluster, cluster_guards, run=run, refresh=refresh, now=now)
        return deferred_recheck(key, cluster_guards)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        recheck_outcomes = list(pool.map(recheck_or_defer, to_recheck))
    deferred_rechecks = sum(1 for r in recheck_outcomes if r is None or r.get("deferred"))
    rechecks = [r for r in recheck_outcomes if r is not None]
    refreshed_baselines = {r["cluster"]: r["symptom_baseline"] for r in rechecks if r.get("symptom_baseline") is not None}
    cleared_ids = {gid for r in rechecks for gid in r["cleared"]}
    refreshed_ids = {gid for r in rechecks for gid in r["refreshed"]}
    new_guards["guards"] = [g for g in new_guards["guards"] if g["id"] not in cleared_ids]
    for guard in new_guards["guards"]:
        if guard["id"] in refreshed_ids:
            guard["last_seen"] = seen_at
            guard["reviews"] = int(guard.get("reviews") or 1) + 1
    for r in rechecks:
        failed_reads.extend(f"{r['cluster']}: {RECHECK_ERROR_PREFIX}{e}" for e in r["errors"])
    # Operation guards are re-checked through the listing every run makes
    # for their project: no read, and a reviewed cluster's review does not
    # decide them, since its window may no longer reach the operation.
    cluster_pools = {cluster_key(c["project"], c["location"], c["name"]): {p.get("name", "") for p in c.get("nodePools") or []} for c in clusters}
    operation_guards = recheck_operation_guards(new_guards["guards"], operations, set(operations_commands), operations_unread, seen_at, scope={cluster_key(c["project"], c["location"], c["name"]) for c in clusters} if scoped else None, pools=cluster_pools)
    cleared_operation_ids = {item["id"] for item in operation_guards["cleared"]}
    unlisted_operation_ids = {item["id"] for item in operation_guards["unlisted"]}
    new_guards["guards"] = [g for g in new_guards["guards"] if g["id"] not in cleared_operation_ids]
    for guard in new_guards["guards"]:
        if guard["id"] in operation_guards["refreshed"]:
            guard["last_seen"] = seen_at
            guard["reviews"] = int(guard.get("reviews") or 1) + 1
        elif guard["id"] in unlisted_operation_ids:
            # Held: its candidate stands as it was, no review having seen it.
            guard["last_seen"] = seen_at
    # The budget was spent when anything was held or deferred, when a read
    # inside a review or a re-check was refused for it, or when the clock
    # says so after the last pool: the last cluster's reads are where it
    # falls on a fleet that nearly fits, with nothing queued behind them.
    budget_text = BUDGET_PREFIX_FORMAT.format(seconds=RUN_DEADLINE_S)
    cut_reviews = sorted(r["cluster"] for r in reviews if any(budget_text in e for e in r["read_errors"]))
    cut_rechecks = sorted(r["cluster"] for r in rechecks if any(budget_text in e for e in r.get("errors") or []) and not r.get("deferred"))
    budget_spent = bool(deferred or deferred_rechecks or cut_reviews or cut_rechecks) or not budget_left()
    # The report's guard lines carry the store's record of each guard (first
    # seen, reviews), not the fresh dict this run's review built.
    merged_by_id = {g["id"]: g for g in new_guards["guards"]}
    for review in reviews:
        review["guards"] = [merged_by_id.get(g["id"], g) for g in review["guards"]]
    # An unchanged cluster's checks ran as the operations listing that found no
    # upgrade; a re-check that read pods or nodes backs those checks with that read.
    recheck_by_cluster = {r["cluster"]: r for r in rechecks}
    listing_errors = {line.split(": ", 1)[0]: line.split(": ", 1)[1] for line in failed_reads if ": " in line and line.split(": ", 1)[0] in operations_unread}
    for row in unchanged:
        project = row["cluster"].split(CLUSTER_KEY_SEPARATOR)[0]
        recheck = recheck_by_cluster.get(row["cluster"]) or {}
        failed = set(recheck.get("failed_reads") or [])
        if project in operations_unread:
            failed.add(OPERATIONS_READ)
        # A check whose read ran and failed -- a re-check read, or the
        # project's operations listing -- is unevaluated, not backed by the
        # listing: the harness then holds the cluster's findings as a partial
        # run rather than resolving or re-filing them, as on a review.
        row["commands"] = checks_run(recheck.get("commands") or {}, fallback=operations_commands.get(project), failed=failed)
        row["checks_unevaluated"] = [{"check": check, "reason": f"{read} read failed"} for check, read in CHECK_READS if read in failed]
        # A read that costs Info alone (a risk guard's storage, webhook or
        # endpoint-slice read) is no limitation.
        errors = [e for e in recheck.get("errors") or [] if not info_only_error(e)]
        if project in operations_unread:
            errors.append(listing_errors.get(project) or f"{OPERATIONS_READ} list failed")
        if errors:
            row["limitations"] = "reads that failed: " + "; ".join(errors)
    new_ledger = ledger_after(ledger, reviews, clusters, seen_at, {s.key: s.operations for s in read}, removed, {row["cluster"] for row in upgrading} | set(deferred), refreshed_baselines, advance=not scoped, attempted={s.key for s in read}, route=route, since=since, outside=outside)
    new_ledger[LEDGER_PROJECTS_KEY] = sorted(new_fleet)

    # Read failures from inside a review join the top-level list so Info
    # names every one in one place.
    failed_reads = failed_reads + [f"{r['cluster']}: {e}" for r in reviews for e in r["read_errors"]]
    result = {
        "generated_at": seen_at,
        "since": fmt_ts(since),
        "projects": projects,
        "reviews": reviews,
        "unchanged": unchanged,
        "failed_reads": failed_reads,
        "operations_unread": sorted(operations_unread),
        "removed_clusters": sorted(removed),
        "rechecks": rechecks,
        "upgrading": upgrading,
        "scoped": scoped,
        "after_upgrade": after_upgrade,
        AFTER_RESET_KEY: after_reset,
        "reset_archive": (reset_marker or {}).get("archive"),
        "reset_at": (reset_marker or {}).get("reset_at"),
        "scope_reason": "; ".join(scope_reasons),
        "fleet": sorted(new_fleet),
        "outside_fleet": sorted(outside),
        "sections": triage(reviews, unchanged, failed_reads, new_guards["guards"], seen_at, sorted(removed), rechecks, upgrading, in_scope={cluster_key(c["project"], c["location"], c["name"]) for c in clusters} if scoped else None, scope_reason="; ".join(scope_reasons), unlisted=unlisted_clusters | set(unread_clusters) | set(held_clusters) | set(not_running) | set(deferred), operation_guards=operation_guards, listing_unread=operations_unread),
        "operation_guards": operation_guards,
        "unread_clusters": unread_clusters,
        "held_clusters": held_clusters,
        "not_running": not_running,
        "deferred": deferred,
        "deferred_rechecks": deferred_rechecks,
        "budget_spent": budget_spent,
        "budget": {"seconds": RUN_DEADLINE_S, "reviewed": len(reviews), "rechecked": len(to_recheck) - deferred_rechecks, "cut": len(cut_reviews), "cut_rechecks": len(cut_rechecks), "deferred": len(deferred), "deferred_rechecks": deferred_rechecks},
        "scope_unread": sorted(unread_outcomes.items()),
        "scope_unresolved": unresolved_outcome,
        "scope_note": scope_note,
        "ledger_path": str(ledger_path),
        "guards_path": str(guards_path),
        "guards": new_guards["guards"],
        "dry_run": bool(args.dry_run),
    }
    result["report"] = render_report(result)
    if args.dry_run:
        return result
    # Write order: report, JSON, manifest, guards, ledger last -- each
    # atomic -- so a crash leaves at most a report with no ledger advance,
    # and the pruning after all of them. Names carry the finish time; a
    # scoped run never moves the fleet link.
    finish = (now if clock_fixed else now_utc()).strftime(REPORT_TS_FORMAT)
    suffix = SCOPED_SUFFIX if scoped else ""
    reports_dir = data_dir() / REPORTS_SUBDIR
    if not getattr(args, "no_report", False):
        report_path = Path(args.report) if args.report else reports_dir / REPORT_FILENAME.format(ts=finish, scoped=suffix)
        write_report(report_path, result["report"], link=not scoped)
        result["report_path"] = str(report_path)
    json_path = Path(args.output) if args.output else reports_dir / REPORT_JSON_FILENAME.format(ts=finish, scoped=suffix)
    write_json_atomically(json_path, {k: v for k, v in result.items() if k != "report"})
    result["json_path"] = str(json_path)
    if getattr(args, "manifest_file", None):
        result["manifest"] = build_manifest(result, ledger, started_at=seen_at, finished_at=fmt_ts(now_utc() if not clock_fixed else now))
        write_json_atomically(Path(args.manifest_file), result["manifest"])
        result["manifest_path"] = str(args.manifest_file)
    write_json_atomically(guards_path, new_guards)
    write_json_atomically(ledger_path, new_ledger)
    if after_reset and not scoped:
        # The baseline is written: the runs from here on file as usual.
        with contextlib.suppress(FileNotFoundError):
            reset_marker_path.unlink()
    # Housekeeping, after the state writes: a refusal here (a link planted at
    # `reports/`, a file this uid cannot unlink) leaves the run's state
    # consistent with the manifest `finish` reads, and is one logged line.
    try:
        prune_reports(reports_dir)
    except (StateUnreadable, OSError) as exc:
        log(PRUNE_FAILED_FORMAT.format(error=exc))
    return result


# `collect` is the name the tests and `main` call; `collect_fleet` is the
# entry the sweep-pool test across the collectors pins.
collect = collect_fleet


def prune_reports(reports_dir: Path) -> list[str]:
    """Keep the newest `REPORTS_KEPT` full reports and the newest
    `REPORTS_KEPT` scoped ones (Markdown and JSON by their shared stamp), and
    whatever stamp the latest link names: `--no-report` and an outside
    `--report` leave JSON-only stamps that would otherwise age the linked
    report out from under its link."""
    pruned = []
    _refuse_symlinked_store_path(reports_dir)
    if not reports_dir.is_dir():
        return pruned
    linked = None
    with contextlib.suppress(OSError):
        m = REPORT_NAME_RE.match(os.readlink(reports_dir / LATEST_REPORT_LINK))
        linked = m.group(1) if m else None
    stamps: dict[bool, set[str]] = {False: set(), True: set()}
    for entry in reports_dir.iterdir():
        m = REPORT_NAME_RE.match(entry.name)
        if m:
            stamps[bool(m.group(2))].add(m.group(1))
    for scoped, found in stamps.items():
        for stamp in sorted(found, reverse=True)[REPORTS_KEPT:]:
            if stamp == linked:
                continue
            for ext in ("md", "json"):
                victim = reports_dir / f"{stamp}{SCOPED_SUFFIX if scoped else ''}.{ext}"
                with contextlib.suppress(FileNotFoundError):
                    victim.unlink()
                    pruned.append(victim.name)
    return pruned


def write_report(path: Path, text: str, *, link: bool = True) -> None:
    """The report through a temporary file, the latest link through a
    temporary symlink, each renamed into place: a reader never sees half.
    The file is `STORE_FILE_MODE` wherever `path` is, as every file this
    collector writes. A scoped run writes no link: the link always names a
    fleet-wide report."""
    output_parent(path)
    # Staged as the JSON is: a random name made with O_EXCL, the mode and
    # owner set through the descriptor, so a symlink planted at a predictable
    # staging name is never opened, truncated, chmodded or chowned.
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=TEMP_SUFFIX)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), STORE_FILE_MODE)
            _adopt_owner(out.fileno(), path.parent.stat())
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    if not link:
        return
    link = path.parent / LATEST_REPORT_LINK
    temp_link = path.parent / (LATEST_REPORT_LINK + LINK_TEMP_SUFFIX)
    with contextlib.suppress(FileNotFoundError):
        temp_link.unlink()
    temp_link.symlink_to(path.name)
    os.replace(temp_link, link)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect what each new or upgraded cluster's last upgrade did, and classify what failed against the upgrade failure catalogue.")
    parser.add_argument("--project", help="one project, a hand run's override: free on an install that declares no scope; on one that does, admitted only beside the fleet_scope tool's collector_args and for a project inside the readable scope. Not with --full. Without it the scope flags decide, else the active gcloud project plus every project `gcloud projects list` returns, as collect.py discovers")
    fleet_scope_args.add_scope_arguments(parser)
    parser.add_argument("--cluster", action="append", help="<project>/<location>/<name>: restrict to this cluster and review it even if unchanged (repeatable)")
    parser.add_argument("--since", help=f"hand run: widen the window to <days>, <days>d or an RFC 3339 timestamp (default {DEFAULT_SINCE_DAYS} days for a cluster not yet in the ledger); makes the run scoped and leaves the ledger's last-run times alone")
    parser.add_argument("--ledger", help=f"ledger path (default ${STORE_HOME_ENV}/{LEDGER_FILENAME}, {DEFAULT_STORE_DIR}/{LEDGER_FILENAME})")
    parser.add_argument("--guards", help=f"guards path (default ${STORE_HOME_ENV}/{GUARDS_FILENAME})")
    parser.add_argument("--output", help=f"write the JSON result here instead of ${STORE_HOME_ENV}/{REPORTS_SUBDIR}/<finish-UTC>[{SCOPED_SUFFIX}].json")
    parser.add_argument("--report", help=f"write the Markdown report here instead of ${STORE_HOME_ENV}/{REPORTS_SUBDIR}/<finish-UTC>[{SCOPED_SUFFIX}].md (a full run also points {LATEST_REPORT_LINK} beside it at it)")
    parser.add_argument("--no-report", action="store_true", help="print the report without writing it to the store")
    parser.add_argument("--manifest-file", help=f"also write the fleet-audit collector manifest here (the contract `audit_report.py finish --manifest-file` cross-checks the SOP's document against); the SOP passes the harness's path for the stream, {harness_manifest_path()}, where `finish`'s --no-collector-manifest waiver and `start`'s scrub look for this run's manifest")
    parser.add_argument("--reset-ledger", action="store_true", help=f"the second remedy for a crash record, after the restore (the record moved back under the file's name, or the guards from the newest {REPORTS_SUBDIR}/<stamp>.json): archive the crash records ({CRASH_RECORD_GLOB}, {GUARDS_RECORD_GLOB}) and the live ledger and guards files (the store's, or --ledger/--guards) under the store's {ARCHIVE_SUBDIR}/<stamp>/ and mark the store, so the next full run is a baseline the SOP files nothing from; does nothing on a store with no record and no file absent beside the other. With no other mode flag (--dry-run, --ledger and --guards apart)")
    parser.add_argument("--probe-lock", action="store_true", help="test the lock without taking it: a non-blocking flock attempt on .lock, held for no time, reading and writing nothing; exit 0 when free, 1 with the holder line when another run holds it. With no other mode flag")
    parser.add_argument("--full", action="store_true", help=f"the fleet-wide run: records and may change the ledger's fleet set, prunes departed projects and clusters (no project when handed the fleet_scope tool's {fleet_scope_args.UNRESOLVED_SCOPE_ROW} row, or when an undeclared install's projects listing failed or did not name the active project: the swept set is then not the fleet), writes {REPORTS_SUBDIR}/<finish-UTC>.md and moves {LATEST_REPORT_LINK}; without it a run is scoped whatever its --project set")
    parser.add_argument("--after-upgrade", action="store_true", help=f"the after-upgrade route: review the clusters with an UPGRADE_MASTER or UPGRADE_NODES that reached DONE at least {int(AFTER_UPGRADE_SETTLE.total_seconds() // SECONDS_PER_MINUTE)} min ago and that the ledger does not list as reviewed, as a scoped run (a cluster with a fresher unreviewed operation waits whole for the next wake); with nothing to review it prints nothing and writes nothing. Not with --full, --cluster or --since")
    parser.add_argument("--dry-run", action="store_true", help="read everything, print the report, write nothing; with --reset-ledger, list the crash records and move none")
    return parser


def acquire_lock(path: Path, *, wait: timedelta | None = None, poll: timedelta | None = None, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
    """An exclusive lock on `path` for the whole run, waited for up to `wait`
    (`LOCK_WAIT`) in steps of `poll`; None when another run still holds it
    after that. The handle keeps the lock until it is closed."""
    wait = LOCK_WAIT if wait is None else wait
    poll = LOCK_POLL_INTERVAL if poll is None else poll
    make_store_dir(path.parent)
    # Read-only on purpose: flock(2) needs no writable descriptor, and a lock
    # another uid created must still open for everyone after; the umask is
    # cleared so the file is LOCK_FILE_MODE whoever creates it.
    mask = os.umask(0)
    try:
        # O_NOFOLLOW: a symlink planted at the lock's name, which needs no
        # guessing, is refused rather than opened and handed over.
        descriptor = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, LOCK_FILE_MODE)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StateUnreadable(LOCK_IS_SYMLINK_TEXT.format(path=path)) from exc
        raise
    finally:
        os.umask(mask)
    handle = os.fdopen(descriptor, "r", encoding="utf-8")  # noqa: SIM115 -- held for the run
    # The lock is the store's too: a root run hands it to the store's owner,
    # through the descriptor it holds.
    with contextlib.suppress(OSError):
        _adopt_owner(descriptor, path.parent.stat())
    deadline = clock() + wait.total_seconds()
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            # Contention, the one error the wait is for.
            remaining = deadline - clock()
            if remaining <= 0:
                handle.close()
                return None
            sleep(min(poll.total_seconds(), remaining))
        except OSError:
            # A filesystem that refuses the lock is not another run holding it.
            handle.close()
            raise
    # The "since" stamp; a file another uid owns refuses it and keeps its own.
    with contextlib.suppress(OSError):
        if os.utime in os.supports_fd:
            os.utime(descriptor, None)
    return handle


def _lock_since(path: Path) -> str:
    try:
        return fmt_ts(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
    except OSError:
        return "unknown"


def lock_held_line(path: Path, waited: timedelta | None = None) -> str:
    return LOCK_HELD_TEXT.format(path=path, since=_lock_since(path), minutes=int((waited or timedelta(0)).total_seconds() // SECONDS_PER_MINUTE))


def lock_probe_held_line(path: Path) -> str:
    return LOCK_PROBE_HELD_TEXT.format(path=path, since=_lock_since(path))


def probe_lock(path: Path) -> tuple[int, str]:
    """`--probe-lock`: whether another run holds `path`, without taking it
    for any time and without writing. A lock file that does not exist is
    free and is not created; an existing one is opened read-only and tried
    with a non-blocking flock that is released at once. Exit 0 with the
    free line, or `EXIT_LOCK_HELD` with the holder line; either line names
    a pending reset (`RESET_MARKER_FILENAME` beside the lock), since the
    probe is what the SOP runs before `start`."""
    rc, line = _probe_lock_state(path)
    marker = path.parent / RESET_MARKER_FILENAME
    if rc != EXIT_USAGE and os.path.lexists(marker):
        line = LOCK_PROBE_RESET_PENDING_FORMAT.format(line=line, marker=marker)
    return rc, line


def _probe_lock_state(path: Path) -> tuple[int, str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return 0, LOCK_PROBE_FREE_TEXT.format(path=path)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return EXIT_USAGE, LOCK_IS_SYMLINK_TEXT.format(path=path)
        raise
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return EXIT_LOCK_HELD, lock_probe_held_line(path)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
    return 0, LOCK_PROBE_FREE_TEXT.format(path=path)

def read_reset_marker(path: Path) -> dict | None:
    """The marker `--reset-ledger` leaves until a full run has written the
    baseline; None when there is none, an empty record when it cannot be
    read (the marker still holds)."""
    try:
        # Without following: a symlink planted at the marker's name holds
        # the marker as unreadable, and nothing is read through it.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def reset_ledger(store: Path, now: datetime, ledger_path: Path | None = None, guards_path: Path | None = None, move: bool = True) -> str:
    """The second remedy for a crash record, after the restore: archive the
    records beside the ledger and guards files (the store's, or the paths
    --ledger/--guards name) and the live files with them, so the next full
    run is a baseline, and leave the marker that makes every run until then
    carry `after_reset`. Nothing happens on a store with no record and no
    file absent beside the other. With `move` False (--dry-run) what would
    move is listed and nothing moves."""
    ledger_file = ledger_path or store / LEDGER_FILENAME
    guards_file = guards_path or store / GUARDS_FILENAME
    records = sorted(ledger_file.parent.glob(ledger_file.name + UNREADABLE_GLOB_SUFFIX)) + sorted(guards_file.parent.glob(guards_file.name + UNREADABLE_GLOB_SUFFIX))
    guards_absent = ledger_file.exists() and not guards_file.exists()
    if not records and not guards_absent:
        return RESET_LEDGER_NOTHING_TEXT
    moving = records + [p for p in (ledger_file, guards_file) if p.exists()]
    archive = store / ARCHIVE_SUBDIR / now.strftime(REPORT_TS_FORMAT)
    names = ", ".join(p.name for p in moving)
    if not move:
        return RESET_LEDGER_DRY_RUN_TEXT.format(count=len(moving), archive=archive, names=names)
    # The archive root is the store's too: made as one, not as a caller-umask
    # ancestor, or a root hand reset would refuse the next uid's.
    make_store_dir(store / ARCHIVE_SUBDIR)
    make_store_dir(archive)
    for item in moving:
        shutil.move(str(item), str(archive / item.name))  # across filesystems too
    write_json_atomically(store / RESET_MARKER_FILENAME, {"version": RESET_MARKER_VERSION, "reset_at": fmt_ts(now), "archive": str(archive), "archived": [p.name for p in moving]})
    return RESET_LEDGER_TEXT.format(count=len(moving), archive=archive, names=names)


def main(argv: list[str] | None = None) -> int:
    run_started = time.monotonic()  # the deadline counts the lock wait too
    args = build_parser().parse_args(argv)
    declared_scope.set(args.scope_projects, args.scope_unread, args.scope_exclude_clusters)
    try:
        validate_args(args)
    except argparse.ArgumentTypeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    if args.probe_lock:
        try:
            rc, line = probe_lock(data_dir() / LOCK_FILENAME)
        except PermissionError as exc:
            print(store_denied_line(exc), file=sys.stderr)
            return EXIT_USAGE
        print(line)
        return rc
    if args.reset_ledger:
        # The reset writes the store, so it holds the lock as a run does and
        # yields with the lock-held line while a run is in flight; the dry
        # run only reads, and takes none.
        reset_lock = None
        try:
            if not args.dry_run:
                lock_path = data_dir() / LOCK_FILENAME
                reset_lock = acquire_lock(lock_path)
                if reset_lock is None:
                    print(lock_held_line(lock_path, LOCK_WAIT))
                    return EXIT_LOCK_HELD
            print(reset_ledger(data_dir(), now_utc(), Path(args.ledger) if args.ledger else None, Path(args.guards) if args.guards else None, move=not args.dry_run))
        except PermissionError as exc:
            print(store_denied_line(exc), file=sys.stderr)
            return EXIT_USAGE
        except StateUnreadable as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
        finally:
            if reset_lock is not None:
                reset_lock.close()
        return 0
    # The projects this run resolves for its route, before the lock: an exit
    # after `start` without a manifest names them, so the SOP's recovery
    # `finish` lists their clusters rather than resolving the scope again.
    discovery = discover_fleet(project_override(args) or None, run=default_run, forced=forced_clusters(args))
    exit_tail = exit_projects_text(discovery, forced_clusters(args))
    lock = None
    try:
        if not args.dry_run:
            lock_path = data_dir() / LOCK_FILENAME
            lock = acquire_lock(lock_path)
            if lock is None:
                print(lock_held_line(lock_path, LOCK_WAIT) + exit_tail)
                return 0
        result = collect(args, discovery=discovery, started=run_started)
    except argparse.ArgumentTypeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except StateUnreadable as exc:
        print(str(exc) + exit_tail, file=sys.stderr)
        return EXIT_USAGE
    except PermissionError as exc:
        # A store path this uid cannot write or read, met at the lock, at a
        # write or at a state file's open: one line, with the projects for the
        # recovery `finish`, no traceback, and nothing moved aside.
        print(store_denied_line(exc) + exit_tail, file=sys.stderr)
        return EXIT_USAGE
    finally:
        if lock is not None:
            lock.close()
    sys.stdout.write(result["report"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
