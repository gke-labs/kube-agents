#!/usr/bin/env python3
"""
fleet_upgrade_report.py — per-member GKE version table against a target version.

Enumerates every cluster in the target projects with `gcloud container clusters list`,
reads each control-plane and node-pool version, and compares them with a target: the
`--target-version` given on the command line, or, without one, each cluster's own
release-channel `defaultVersion` from `gcloud container get-server-config`. Prints a
Markdown table on stdout and, with `--output`, writes the same data as JSON.

Each run also records its per-member result under a state directory, keyed by target,
and reads the previous run's record for the same target: a "Rollout progress" section
after the table says per member whether it `started`, `completed`, or is `unchanged`
since that run, and flags an unchanged, behind member as `stalled` while a rollout is
active (another member moved, or `--rollout-in-progress` was passed).

With `--readiness`, each member is also graded on whether it can take the upgrade to its
target: drain-blocking PodDisruptionBudgets and fail-closed admission webhooks with an
unreachable backend in the upgrade's path or able to refuse the control plane's bootstrap RBAC
writes (ClusterRoles and ClusterRoleBindings; Roles and RoleBindings in kube-system and
kube-public; read with two `kubectl get` per member after
`gcloud container clusters get-credentials` into a per-target kubeconfig), a maintenance
exclusion in effect whose scope covers the upgrade, the maintenance window's state at
`--at`, and node-pool version skew against the target control plane; then the per-entry
rules registered in `upgrade_readiness.EXTRA_RULES` (`readiness_rules/`, one module per
entry of the upgrade-failure catalogue), which read the same cluster record, the objects of
the `kubectl get`s after the PDB read, one per group in LATER_READS (the namespaced kinds;
the Pods on their own; the Nodes through a jsonpath template; StorageClasses and
PersistentVolumes), each failing on its own, and, through the per-member context, whatever
a rule reads itself (a rule that reads the audit log runs paged `gcloud logging read`s).
A rule's blockers join the verdict and its risks get a column of their own. The rules live
in `upgrade_readiness.py` and `readiness_rules/`; this file reads and renders.

Read-only against GCP: the gcloud commands it runs are `container clusters list`,
`container get-server-config`, `projects list` (when neither `--project` nor a declared scope is passed), `config get-value project`, `projects
describe` (to tie an API-disabled refusal to its project or resolve a numeric project
number to its projectId) and, with
`--readiness`, `container clusters get-credentials` and, for a rule that reads the audit
log, `logging read`. The only things it writes are its own state file, the per-target
kubeconfig files `get-credentials` produces, and the optional `--output` JSON.
"""

import argparse
import errno
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
import tempfile
from datetime import datetime, timezone

import readiness_rules
import upgrade_readiness as readiness
from readiness_rules import audit_log

# Project resolution: explicit --project flags are the whole scope, and so is the
# declared scope the agent passes (--scope-projects, from the platform_control
# fleet_scope tool). Otherwise the
# per-profile project variables are unioned with the fleet's monitored-project list
# when it is set, or with gcloud's configured project and every project
# `gcloud projects list` returns when it is unset or blank.
MONITORED_PROJECTS_ENV = "MONITORED_PROJECT_IDS"
PROJECT_ENV_VARS = ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID")
GCLOUD = "gcloud"
JSON_FORMAT_FLAG = "--format=json"
PROJECT_ID_FORMAT = "--format=value(projectId)"
PROJECTS_LIST_CMD = (GCLOUD, "projects", "list", PROJECT_ID_FORMAT)
# The `project` a failed or filtered `gcloud projects list` is reported under.
PROJECTS_LIST_ERROR_SCOPE = "(all projects: gcloud projects list)"
# The same slot when the scope came from the fleet_scope tool and nothing was listed.
DECLARED_SCOPE_ERROR_SCOPE = "(declared scope: fleet_scope)"
PROJECTS_LIST_ERROR_CHARS = 300
# Where the image and the shell sandbox ship the scripts the collectors share
# (deploy/docker/Dockerfile and deploy/sandbox/Dockerfile copy them to
# /opt/defaults/scripts), then the checkout's own copy for a run from the
# repository.
# The checkout's copy only when there is a checkout: a file three or fewer
# directories below `/` has no parents[3], as collect.py guards the same path.
SHARED_SCRIPT_DIRS = (
    "/opt/defaults/scripts",
    "/opt/data/scripts",
    *([str(Path(__file__).resolve().parents[3] / "scripts")] if len(Path(__file__).resolve().parents) > 3 else []),
)

# The install's declared scope, handed in by the agent from the platform_control
# `fleet_scope` tool (the collectors run in the shell sandbox and cannot read
# the reconcile's snapshot themselves): the flags, their parsing and the note
# live in fleet_scope_args, shared with every collector. `--scope-projects` is
# the sweep, complete coverage; `--scope-unread` names each declared project
# the install could not read, recorded as a coverage gap. Without them the
# collector enumerates every project the identity can list, which is right on
# a checkout and on an install that declares no scope; on a sandbox whose
# operator says a scope is declared (KUBEAGENTS_SCOPE_DECLARED, forwarded into
# the session) a run without them refuses instead, and so does a --project
# the scope does not list.
for _shared_dir in SHARED_SCRIPT_DIRS:
    if _shared_dir not in sys.path:
        sys.path.append(_shared_dir)
import fleet_scope_args  # noqa: E402

# The scope this collector was handed, set by main from the scope flags.
declared_scope = fleet_scope_args.DeclaredScope()

CONFIG_PROJECT_CMD = (GCLOUD, "config", "get-value", "project")
API_DISABLED_MARKERS = (
    "SERVICE_DISABLED",
    "accessNotConfigured",
    "has not been used in project",
)
# A disabled-API refusal names the consumer project -- by number in gcloud's
# usual phrasing, by id in some. With a quota project set, that consumer is the
# quota project rather than `--project`, so the marker alone cannot say whose
# API is off. fleet_waste.refusal_owner draws the same line.
REFUSED_PROJECT_NUMBER_RE = re.compile(r"\bprojects?[ /](\d+)\b")
PROJECT_DESCRIBE_CMD = (GCLOUD, "projects", "describe")
PROJECT_NUMBER_FORMAT = "--format=value(projectNumber)"
# A stalled API call is reported as a failed read for its project or location rather
# than blocking the agent turn; the same budget compute_fleet_audit.py gives gcloud.
GCLOUD_TIMEOUT_SECONDS = 60

# `MAJOR.MINOR.PATCH-gke.BUILD`; the `-gke.BUILD` suffix is optional, and a version
# without it gets BUILD 0, as the security-patch-orchestrator SOP's comparison rule
# says. Anything else is unparsable and degrades its row to `unknown`.
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-gke\.(\d+))?$")

# Per-member verdicts. `lagging` is a minor (or major) behind the target on the control
# plane or a pool; `patch-behind` is the same minor with a lower patch or gke build,
# kept apart because a new patch reaches a channel default before any rollout wave has
# applied it, so a whole fleet is routinely patch-behind the morning after. `ahead` is
# reported and never flagged for the mirror-image reason. The SOP grades the same split
# as major versus minor severity.
STATUS_LAGGING = "lagging"
STATUS_PATCH_BEHIND = "patch-behind"
STATUS_CURRENT = "current"
STATUS_AHEAD = "ahead"
STATUS_UNKNOWN = "unknown"
STATUS_ORDER = (STATUS_LAGGING, STATUS_PATCH_BEHIND, STATUS_CURRENT, STATUS_AHEAD, STATUS_UNKNOWN)

# `releaseChannel.channel` values that mean "no channel"; such a member has no
# channel default to measure against and needs an explicit --target-version.
NO_CHANNEL_VALUES = ("", "UNSPECIFIED")

# How the target column labels a target that came from the channel rather than the
# flag, so the baseline is visible per member on a mixed-channel fleet.
TARGET_SOURCE_FLAG = "--target-version"
CHANNEL_DEFAULT_LABEL = "channel default ({channel})"

# Cluster and node-pool `status` values that mean an upgrade is in flight. The row is
# still graded (the plan's four states are the contract), but the note says so, as
# the SOP does before it suppresses a version finding.
IN_FLIGHT_STATUSES = ("RECONCILING", "PROVISIONING")

# Table rendering: the empty-cell placeholder, the header separator cell, the
# separator between the reasons a row's note carries, and the name shown for a pool
# record that has none.
EMPTY_CELL = "-"
TABLE_SEPARATOR_CELL = "---"
NOTE_SEPARATOR = "; "
UNNAMED_POOL = "?"
JSON_INDENT = 2
TABLE_COLUMNS = (
    "project",
    "cluster",
    "location",
    "channel",
    "control plane",
    "lowest node pool",
    "target",
    "gap (minors)",
    "status",
    "note",
)

# Where a run leaves its record for the next one. The script runs in the shell sandbox,
# whose /opt/data is a PersistentVolumeClaim the operator retains when the sandbox
# StatefulSet goes (shell_sandbox_manifests.go), and whose entrypoint replaces only the
# /opt/defaults trees and the database stubs on start (deploy/sandbox/entrypoint.sh);
# /opt/data/scratch is per-run scratch by convention, so the record goes in a directory
# of the skill's own name that nothing else writes. One file per target, named after
# it: the target string is validated by VERSION_RE, so it is filesystem-safe as is, and
# a run without --target-version measures each member against its channel default and
# keys its record under CHANNEL_DEFAULT_STATE_KEY. --state-dir overrides the directory.
DEFAULT_STATE_DIR = "/opt/data/state/fleet-upgrade-verification"
CHANNEL_DEFAULT_STATE_KEY = "channel-default"
STATE_FILE_SUFFIX = ".json"
# The record is written to a uniquely named sibling and renamed into place, so two
# runs against one target at once (a chat turn and a delegated card) cannot truncate
# each other's half-written file; the last rename wins whole.
STATE_TMP_PREFIX = ".fleet-upgrade-record-"
# The `--output` document is written the same way, whole, so a reader never meets a partial
# one; a run that dies before it writes leaves the path as it found it, and a caller that must
# tell its own run's file from an earlier one names the file per run (the persona's bullet).
OUTPUT_TMP_PREFIX = ".fleet-upgrade-output-"
# An `errors` row for the second, webhook read carries this key, so a reader of the report (the
# readiness watch) can tell a member whose first read answered, and whose other rules were
# graded, from one the API server never answered.
ERROR_READ_KEY = "read"
WEBHOOK_READ = "webhooks"
# Bumped when the record's shape changes; a file with another version is reported and
# treated as no prior run rather than compared field by field.
STATE_FORMAT_VERSION = 1
# `project/location/cluster`, the same triple the table sorts on; unique across a
# fleet because a project's cluster names are unique per location.
MEMBER_KEY_SEPARATOR = "/"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Per-member progress since the previous run for the same target. `completed`: the
# member is current or ahead after a version change (a current member that moved to a
# new channel default included), or its status became current or ahead. `started`:
# the control plane or the lowest pool changed version without reaching the target, or
# the member is in flight. `unchanged`:
# the same versions and status as before. `new`: no prior record. `stalled` is an
# unchanged member that is lagging or patch-behind while a rollout is active, which is
# the signal a point-in-time table cannot give: a member that has not moved between two
# polls while its peers did.
PROGRESS_NEW = "new"
PROGRESS_STARTED = "started"
PROGRESS_COMPLETED = "completed"
PROGRESS_UNCHANGED = "unchanged"
PROGRESS_STALLED = "stalled"
PROGRESS_ORDER = (PROGRESS_COMPLETED, PROGRESS_STARTED, PROGRESS_STALLED, PROGRESS_UNCHANGED, PROGRESS_NEW)
# Statuses that mean the member has reached the target, and the ones a stall can be
# called on. `unknown` is in neither: a row that could not be graded is not a stall,
# and an `unknown` observation on either side of a comparison is not evidence of a
# move either (a timed-out get-server-config must not read as a completed upgrade),
# so the comparison falls back to the versions alone and the record keeps the last
# graded status.
DONE_STATUSES = (STATUS_CURRENT, STATUS_AHEAD)
BEHIND_STATUSES = (STATUS_LAGGING, STATUS_PATCH_BEHIND)
GRADED_STATUSES = DONE_STATUSES + BEHIND_STATUSES
# Why a rollout counted as active, for the summary line and the JSON.
ACTIVE_REASON_FLAG = "--rollout-in-progress"
ACTIVE_REASON_MOVERS = "another member moved since the previous run"
# Why a member in the previous record has no row this run.
GONE_REASON_ABSENT = "not in this run's cluster list; dropped from the record"
GONE_REASON_NOT_READ = "not read this run ({why}); carried forward"
NOT_READ_FAILED = "clusters list failed for {project}"
NOT_READ_OUT_OF_SCOPE = "{project} not in this run's projects"

# Elapsed-time rendering for `stalled (unchanged for ...)`.
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
ELAPSED_UNDER_A_MINUTE = "<1m"
PROGRESS_COLUMNS = (
    "project",
    "cluster",
    "location",
    "previous (control plane / lowest pool)",
    "now (control plane / lowest pool)",
    "status",
    "progress",
)
VERSION_PAIR_SEPARATOR = " / "

# Readiness (`--readiness`). The rules need two kubectl reads per member (PDBs with their
# Deployments and StatefulSets, then webhook configurations with their Services and
# EndpointSlices, and the Namespaces), and the per-entry rules the reads after those
# (LATER_READS, below), which need credentials for that member: `get-credentials` writes a
# per-target kubeconfig, passed to every command as KUBECONFIG in the subprocess
# environment rather than through a flag,
# because the gcloud and kubectl in the agent pod are credential-proxy shims that forward
# that variable. The directory is `$HERMES_HOME/.kubeconfigs`, the platform AGENTS.md
# convention, and the file name mirrors `_thread_kubeconfig_path` in
# scripts/platform_mcp_server.py: one file per target, so concurrent reads of different
# clusters never race on one current-context; the proxy refuses a path outside its
# workspace, which is why the default is not /tmp. `--kubeconfig-dir` overrides it.
KUBECTL = "kubectl"
KUBECTL_TIMEOUT_SECONDS = 60
# Error text from the first `kubectl get` after which the second read, against the same
# kubeconfig, would fail the same way and only spend a second timeout: kubectl's transport
# prefix, which a connection failure, a credential-plugin failure (`getting credentials:`) and a
# certificate failure (`tls:`) all carry, and a dial or resolver failure. run_cmd's own deadline
# is not one: in the agent sandbox kubectl is the credential-proxy client, whose broker may hold
# a request in its admission queue for up to its slot wait before kubectl runs, so the deadline
# measures the queue as often as the server, and a second read enters the queue afresh. The note
# names the class, not an unreachable server, and kubectl's message stays in the error row. In
# the sandbox the shim prints its own line when it cannot reach the broker (`credential proxy
# unavailable: ...`), which the second call would print again. Matched without case: the errno
# text behind the shim's line is capitalised. An error the server returned never skips, whatever
# dial words its body quotes: kubectl prefixes one with `Error from server (<reason>):`, and an
# InternalError quoting the apiserver's own etcd dial failure is an answer, which the second read
# may still get.
FIRST_READ_SKIP_MARKERS = ("unable to connect to the server", "i/o timeout", "connection refused", "no such host", "credential proxy unavailable")
SERVER_ANSWERED_PREFIX = "error from server"
WEBHOOK_READ_SKIPPED = "skipped: the PDB read failed before the API server answered it (a connection, credential-plugin or certificate failure, or the credential proxy unreachable)"
# Two reads, so a failure listing the webhook side (a large EndpointSlice list timing out or cut
# at the proxy's cap, a custom role without webhook-configuration or namespace reads; the
# installer's roles have both, and the name-label fallback in `bootstrap_namespace_labels` is for
# a read that returns no Namespace object, not for a refused read) costs the webhook rule
# only, never the PDBs. The webhook read lists every Service, EndpointSlice and Namespace in the
# cluster though only the referenced Services and the two bootstrap namespaces (whose labels the
# `namespaceSelector` is judged on) are consulted; a read proportional to the webhooks (the
# configurations first, then each referenced Service and its slices, and the two namespaces by
# name) is the change that removes the cluster-size bound, and is not made here.
KUBECTL_RESOURCES = "pdb,deploy,statefulset"
KUBECTL_WEBHOOK_RESOURCES = "validatingwebhookconfigurations,mutatingwebhookconfigurations,services,endpointslices,namespaces"
# The reads after the PDB read, which stays as it is above, one per group in LATER_READS:
# the namespaced kinds the per-entry rules read; the Pods on their own, since the pod list
# is the one most likely to exceed the credential proxy's output cap and a cut read must
# cost only the rules that need pods; the Nodes through a jsonpath template that keeps the
# fields the rules use (name, labels, taints, whether the node is cordoned, nodeInfo,
# allocatable, conditions) and drops the rest (`status.images`, the annotations), which is
# what keeps a few hundred nodes under that cap (one line per node, tab-separated, each
# field JSON as kubectl's jsonpath renders maps, lists and booleans, an absent field empty);
# and StorageClasses and PersistentVolumes as JSON. Each read fails on its own: a refused,
# unparsable or cut read is recorded per kind in `read_errors`, costs only the rules that
# need those kinds (each files `unknown` for itself), never the PDB rule, and sets no exit
# code. The kinds are readiness_rules.READ_KINDS, named once for the rules and the report; a
# failed command marks every kind it carried, including a partial listing.
# KUBECTL_READS_PER_MEMBER, derived from the table, is the count the help cites.
ALL_NAMESPACES_FLAG = "-A"
OUTPUT_FLAG = "-o"
JSON_OUTPUT_ARGS = (OUTPUT_FLAG, "json")
READ_MODE_NAMESPACED_JSON = "namespaced-json"
READ_MODE_CLUSTER_JSON = "cluster-json"
READ_MODE_NODE_JSONPATH = "node-jsonpath"
KUBECTL_NAMESPACED_RESOURCES = ",".join(readiness_rules.NAMESPACED_READ_KINDS)
KUBECTL_POD_RESOURCE = readiness_rules.POD_READ_KIND
KUBECTL_NODE_RESOURCE = readiness_rules.NODE_READ_KIND
KUBECTL_CLUSTER_RESOURCES = ",".join(readiness_rules.CLUSTER_READ_KINDS)
LATER_READS = (
    (readiness_rules.NAMESPACED_READ_KINDS, KUBECTL_NAMESPACED_RESOURCES, READ_MODE_NAMESPACED_JSON),
    ((readiness_rules.POD_READ_KIND,), KUBECTL_POD_RESOURCE, READ_MODE_NAMESPACED_JSON),
    ((readiness_rules.NODE_READ_KIND,), KUBECTL_NODE_RESOURCE, READ_MODE_NODE_JSONPATH),
    (readiness_rules.CLUSTER_READ_KINDS, KUBECTL_CLUSTER_RESOURCES, READ_MODE_CLUSTER_JSON),
)
KUBECTL_READS_PER_MEMBER = 2 + len(LATER_READS)  # the PDB read, the webhook read, then LATER_READS
KUBECTL_NODE_JSONPATH = '{range .items[*]}{.metadata.name}{"\\t"}{.metadata.labels}{"\\t"}{.spec.taints}{"\\t"}{.spec.unschedulable}{"\\t"}{.status.nodeInfo}{"\\t"}{.status.allocatable}{"\\t"}{.status.conditions}{"\\n"}{end}'
JSONPATH_OUTPUT_FORMAT = "jsonpath={template}"
NODE_FIELD_SEPARATOR = "\t"
NODE_LINE_SEPARATOR = "\n"  # what the jsonpath template ends each node with; a NEL (U+0085) inside a field is not a line break
NODE_FIELD_COUNT = 7
NODE_KIND = readiness_rules.KIND_NODE
# The credential proxy caps each stream a brokered command returns; past the cap it writes
# the cut stdout, prints a marker on stderr (readiness_rules.PROXY_TRUNCATION_MARKER, which
# the audit-log reader checks too) and returns the command's own exit code, so a cut list
# reads as rc 0 with output that may or may not parse. The marker, not the parse failure,
# names the cause, and the kinds the read carried are recorded as cut.
PROXY_TRUNCATION_MARKER = readiness_rules.PROXY_TRUNCATION_MARKER
KUBECTL_OUTPUT_CUT = "{cmd} output was cut at the credential proxy's output cap, so the list did not fit in one read"
# The member's note names the one cause its reads failed for, so an operator goes to the
# step that failed: the directory, the credentials, the first read, the second read, or the
# second read skipped because the first failed before the server answered it (the note names
# the classes that produce that and the error row carries kubectl's message, rather than the
# note calling the server unreachable). A skipped read is not a failed one: it gets no error
# row of its own under the table.
NOTE_DIRECTORY_FAILED = "kubeconfig directory could not be created; PDBs and webhooks not graded"
NOTE_CREDENTIALS_FAILED = "credentials for the cluster could not be fetched; PDBs and webhooks not graded"
NOTE_PDB_READ_FAILED = "PDB read failed; PDBs not graded"
NOTE_WEBHOOK_READ_FAILED = "webhook read failed; webhooks not graded"
NOTE_WEBHOOK_READ_SKIPPED = f"webhook read {WEBHOOK_READ_SKIPPED}; webhooks not graded"
NOTE_READ_FAILED = "{kinds} not read ({error}); the per-entry rules that need them say so"
# A read's error in the note column: collapsed to one line and cut to this, the length the
# fold gives a rule's unknown reason; the JSON carries it whole.
NOTE_ERROR_CHARS = readiness_rules.NOTE_EXCERPT_CHARS
# A failed audit-log read is listed under the table in its own block and under
# `readiness.audit_log_failures` in the JSON, apart from `errors`: the member stays graded
# by the rules that did not need the read, the rule that did says `unknown`, and the daily
# watch, which reads a cluster-keyed `errors` entry as a kubectl read that failed and the
# cluster as not graded, never sees it there. It still sets exit code 1, like any read
# that failed.
AUDIT_LOG_FAILURE_LINE = "- audit log not read for {where} ({read} read): {error}; the member stays graded, and the rule that needed the read says unknown"
READ_FAILED_LINE = "- read failed for {where}: {message}"
WHERE_LOCATION = "({location})"
WHERE_CLUSTER = "cluster {cluster}"
WHERE_SEPARATOR = " "
WHERE_NOTHING = "the run"
KUBECONFIG_ENV = "KUBECONFIG"
HERMES_HOME_ENV = "HERMES_HOME"
DEFAULT_HERMES_HOME = "/opt/data"
KUBECONFIG_SUBDIR = ".kubeconfigs"
KUBECONFIG_FILE_FORMAT = "kubeconfig_{project}_{cluster}_{location}.yaml"
# The default, shared directory is this uid's and closed to every other uid
# (0755), the rule the fleet-audit collectors' `collect.prepare_kubeconfig`
# applies to the same directory: the file names are public and fixed, so a
# directory other uids could write would let any login plant an entry at a
# member's name. A hand run over `kubectl exec` lands as root while the agent
# runs as uid 1000: root writes into the agent's directory as it is, and a
# directory another uid made first is moved aside and remade; a wider mode is
# brought back. A `--kubeconfig-dir` the caller named is made under the
# caller's umask.
KUBECONFIG_DIR_MODE = 0o755
KUBECONFIG_DIR_MODE_MASK = 0o7777
# A chmod the filesystem refuses keeps a directory this uid owns alone (owner
# write and search, no other uid's write, 0700 say) at its mode, said on
# stderr, as the shared rule keeps it; any other mode the chmod cannot reach
# is the note.
KUBECONFIG_DIR_OWNER_WRITE_SEARCH = 0o300
KUBECONFIG_DIR_OTHERS_WRITE = 0o022
KUBECONFIG_UNUSABLE_FORMAT = "{name}.unusable-{stamp}"
# The shared rule's notes, as `collect.usable_kubeconfig_dir` words them: the
# reason a directory or file could not be made usable, with the owner named,
# and the stderr line for a directory moved aside.
KUBECONFIG_MOVED_ASIDE_FORMAT = "{directory}: owned by uid {uid}, moved aside to {aside}; this run makes its own"
KUBECONFIG_NOT_MOVED_FORMAT = "{directory} is owned by uid {uid} and this run is uid {euid}; it could not be moved aside ({error})"
KUBECONFIG_NOT_MADE_FORMAT = "{directory} could not be made ({error})"
KUBECONFIG_NOT_READ_FORMAT = "{directory} could not be read ({error})"
KUBECONFIG_MODE_NOT_CHANGED_FORMAT = "{directory} is at mode {mode}, not {wanted}, and could not be changed ({error})"
KUBECONFIG_MODE_KEPT_FORMAT = "{directory} stays at mode {mode}, which could not be changed to {wanted} ({error}): this uid's own and closed to other uids, so usable"
KUBECONFIG_NOT_REPLACED_FORMAT = "{path} is not this uid's writable file and could not be replaced ({error})"
# What may stand at a kubeconfig's name, as the shared rule reads it: this
# uid's own writable regular file. A link is removed, a directory or other
# object moved aside (said on stderr), another uid's or an unwritable file
# removed; each refusal is the note.
KUBECONFIG_FILE_NOT_READ_FORMAT = "{path} could not be read ({error})"
KUBECONFIG_NOT_A_FILE_MOVED_FORMAT = "{path}: not a regular file, moved aside to {aside}; gcloud writes a fresh file"
KUBECONFIG_NOT_A_FILE_FORMAT = "{path} is not a regular file, which a kubeconfig must be, and could not be moved aside ({error})"
KUBECONFIG_NOT_PREPARED_FORMAT = "cannot prepare kubeconfig: {note}"
KUBECONFIG_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# A rename that finds the directory already moved (another run's tick in the
# same second won the move-aside): not a refusal, as the shared rule reads it.
KUBECONFIG_MOVED_BY_ANOTHER_ERRNOS = (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST)
KUBECONFIG_IS_SYMLINK_FORMAT = "{directory} is a symbolic link, which the shared directory may not be; it was not followed"
KUBECONFIG_LINK_NOT_REMOVED_FORMAT = "{path} is a symbolic link, which a kubeconfig may not be, and could not be removed ({error})"
ROOT_UID = 0
# The same reduction platform_mcp_server._kubeconfig_slug applies: real GKE names are
# lowercase alphanumerics and hyphens, so this is lossless for them and keeps a value
# holding `/` or `..` from steering the path out of the directory.
KUBECONFIG_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]")
KUBECONFIG_SLUG_REPLACEMENT = "_"
KUBECONFIG_SLUG_EMPTY = "unset"
# `--dns-endpoint` is added when the cluster record says its DNS endpoint accepts external
# traffic, the decision agents/platform/scripts/gke_endpoint.py makes from `clusters
# describe`; the `clusters list` record already read here carries the same block. The
# flag is not safe unconditionally: gcloud rejects it on a cluster without a DNS endpoint.
DNS_ENDPOINT_FLAG = "--dns-endpoint"
# Readiness table rendering.
READINESS_COLUMNS = (
    "project",
    "cluster",
    "location",
    "readiness",
    "drain-blocking PDBs",
    "fail-closed webhooks",
    "maintenance",
    "node-pool skew",
    "other blockers",
    "risks",
    "note",
)
READINESS_NONE_CELL = "none"
READINESS_READ_FAILED_CELL = "read failed"
READINESS_READ_SKIPPED_CELL = "read skipped"
READINESS_NOT_EVALUATED_CELL = "not evaluated"
READINESS_NO_OPENING_CELL = f"none within {readiness.DAYS_PER_WEEK} days"

# Exit codes. A failed gcloud call is reported per project and per location and does
# not abort the run. The exit code says whether every read that grades the version table,
# the PDB rule or the webhook rule succeeded: the gcloud reads, `get-credentials`, the PDB
# read, the webhook read, and an audit-log read a rule performed. The reads after the
# webhook read (LATER_READS) are recorded per kind in the member's note and JSON and leave
# it untouched.
EXIT_OK = 0
EXIT_PARTIAL = 1
NARROWED_RUN_NOTE = "Narrowed by --cluster: the rollout record was neither read nor written."
# A `--cluster` spec that matched nothing is an error with exit 1, like a project that
# could not be listed: an empty table with exit 0 would read as "nothing to grade".
UNMATCHED_CLUSTER_SPEC = "no cluster matched --cluster {spec!r} in {projects}"
# A `--cluster` spec naming a cluster the install excludes (--scope-exclude-clusters,
# #3081): it exists and was left out on purpose, which is not "no match".
EXCLUDED_CLUSTER_SPEC = "--cluster {spec!r} names {clusters}, which spec.scope.exclude.clusters excludes; it was not checked"
# When a project's listing failed, the run cannot say the spec matched nothing
# there: the line names the projects it did list and the ones it could not,
# rather than sending the operator after a typo or a deleted cluster.
UNMATCHED_CLUSTER_SPEC_PARTIAL = "no cluster matched --cluster {spec!r} in {listed}; {unlisted} could not be listed, so whether it is there is unknown"
UNMATCHED_CLUSTER_SPEC_UNLISTED = "--cluster {spec!r} could not be matched: {unlisted} could not be listed"
EXIT_USAGE = 2
# `--rollout-in-progress` speaks to the rollout record, which a `--cluster` run neither
# reads nor writes: the pair is refused, as `--at` without `--readiness` is, rather than
# accepted and ignored.
ROLLOUT_FLAG_NEEDS_FULL_READ = "--rollout-in-progress needs a full read: a --cluster run neither reads nor writes the rollout record"
# `--cluster` takes `<location>/<name>` or a bare name and no third form. A spec
# with an empty half (`/prod`, `prod/`) is refused before any read: `/prod` would
# otherwise partition to the bare form and match `prod` in every location, the
# opposite of what its slash says, and the unmatched-spec line would then name
# a spec the operator did not type.
CLUSTER_SPEC_SEPARATOR = "/"
# Each half of a spec is a GKE name: lowercase letters, digits and hyphens, never
# a hyphen first, as GKE spells every location and cluster name. Anything else --
# whitespace from a pasted terminal line, an uppercase letter, a stray character,
# a leading hyphen -- can match nothing, so the parser refuses it before any read
# instead of letting the run report a miss that reads as a typo or a deleted
# cluster. The grammar is the tree's GKE name component, character for character
# as `credential_proxy_client._GKE_CONTEXT_COMPONENT` and `scoped_sa_pool._COMPONENT`
# spell it, without their anchors because every use here is `fullmatch`; this
# file does not import the broker's client for a regex, and the tests hold the
# three equal (this half with the broker's component in this skill's suite, the
# pool's copy with it in `test_scoped_sa_pool.py`), so a drift fails rather than
# admitting a half the broker refuses.
CLUSTER_SPEC_HALF_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
INVALID_CLUSTER_SPEC = "--cluster {spec!r} is neither <location>/<name> nor a bare name: each half is a GKE name (lowercase letters, digits and hyphens), a location before the slash and a name after it, or the name alone"


def run_cmd(cmd: list[str], timeout: int = GCLOUD_TIMEOUT_SECONDS, env: dict | None = None) -> tuple[int, str, str]:
    """Runs a command and returns (rc, stdout, stderr); never raises. `env` None inherits."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout, env=env)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout} seconds"
    except Exception as e:  # noqa: BLE001 - a missing binary is a report, not a crash
        return -1, "", str(e)


def run_gcloud_json(cmd: list[str]) -> tuple[list | dict | None, str | None]:
    """Runs a gcloud command with JSON output and returns (parsed, error_message)."""
    rc, stdout, stderr = run_cmd(cmd)
    if rc != 0:
        return None, f"{' '.join(cmd)} failed ({rc}): {stderr.strip()}"
    if not stdout.strip():
        return [], None
    try:
        return json.loads(stdout), None
    except ValueError as e:
        return None, f"{' '.join(cmd)} returned unparsable JSON: {e}"


def refusal_names_project(project: str, stderr: str) -> tuple[bool, str]:
    """Whether an API-disabled refusal is `project`'s own, with why when it is not.

    Only the project's own refusal means it holds no cluster. One naming another
    project -- the credential's quota project -- says nothing about this one, and
    read as empty it would drop every recorded member of the project from the
    rollout record with exit 0. A refusal naming no project, or one whose number
    cannot be compared with this project's, is a failed read.
    """
    numbers = set(REFUSED_PROJECT_NUMBER_RE.findall(stderr))
    if not numbers:
        if re.search(rf"\b(?i:projects?)[ /]['\"\[]?{re.escape(project)}(?![\w-])", stderr):
            return True, ""
        return False, f"the refusal does not name {project!r}"
    rc, stdout, err = run_cmd([*PROJECT_DESCRIBE_CMD, project, PROJECT_NUMBER_FORMAT])
    if rc != 0:
        return False, (
            f"`gcloud projects describe {project}` failed (rc={rc}), so the refusal's project number "
            f"could not be compared: {(err or '').strip()[:PROJECTS_LIST_ERROR_CHARS] or 'no stderr'}"
        )
    if numbers == {stdout.strip()}:
        return True, ""
    return False, f"the Kubernetes Engine API is off in a project other than {project!r}, such as a quota project"


def _normalise_project_id(project: str, listing_errors: list[str] | None = None) -> str | None:
    """Resolves a numeric project number (e.g. from spec.harness.projectId) to its projectId."""
    if not project.isdigit():
        return project
    rc, stdout, stderr = run_cmd([*PROJECT_DESCRIBE_CMD, project, PROJECT_ID_FORMAT])
    if rc == 0 and stdout.strip():
        return stdout.strip()
    if listing_errors is not None:
        listing_errors.append(
            f"`gcloud projects describe {project}` rc={rc}: "
            f"{(stderr or '').strip()[:PROJECTS_LIST_ERROR_CHARS] or 'no stderr'}; "
            "numeric project could not be resolved to a projectId"
        )
    return None


def get_target_projects(cli_projects: list[str] | None = None, listing_errors: list[str] | None = None) -> list[str]:
    """Resolves the projects to enumerate: --project wins, else the declared
    scope the SOP passed from the fleet_scope tool (--scope-projects, swept as
    given with nothing listed), else env unioned with discovery.

    Under a declared scope the only entry appended to `listing_errors` is the
    note naming the declared projects the install could not read
    (--scope-unread), so the run reads as partial for them.

    A failed `gcloud projects list` is appended to `listing_errors` when the
    caller passes one, so the narrowed scope reads as a failed read rather than
    as the whole fleet. So is a listing that succeeds without naming the
    configured project: it is filtered, not complete, as fleet_drift.py treats it.
    """
    if cli_projects:
        # Refused before anything is read: a scoped sandbox without the flags
        # must not describe a project the worker named.
        if declared_scope.args_missing:
            if listing_errors is not None:
                listing_errors.append(declared_scope.empty_error())
            return []
        resolved: set[str] = set()
        if not any(p.strip() for p in cli_projects):
            # `--project "$VAR"` with the variable unset. Under a declared scope
            # say so, with the remedy; without one the "no project" usage line
            # main prints is the right answer, and this remedy would name a
            # scope the install does not have.
            if listing_errors is not None and declared_scope.declared:
                listing_errors.append(fleet_scope_args.EMPTY_PROJECT_OVERRIDE_ERROR)
            return []
        for raw in (p.strip() for p in cli_projects if p.strip()):
            project = _normalise_project_id(raw, listing_errors)
            # Checked on the resolved id, so a number that names a listed project
            # passes; a number the describe could not resolve reaches the holder
            # as digits, which it refuses under a declared scope with the remedy
            # and lets through without one, as before, with the failure recorded.
            resolved.add(project or raw)
        # On the resolved ids: a project number given here names the same
        # project the tool lists by id.
        override_errors = [declared_scope.override_error(p) for p in sorted(resolved)]
        if any(override_errors):
            if listing_errors is not None:
                listing_errors.extend(e for e in override_errors if e)
            return []
        return sorted(resolved)
    if declared_scope.declared:
        # The declared scope the agent carried from the fleet_scope tool: the
        # sweep as given, nothing listed, the unread rows as the one note. A
        # declared scope with nothing readable enumerates nothing and says so,
        # rather than widening to the listing.
        if not declared_scope.projects:
            if listing_errors is not None:
                listing_errors.append(declared_scope.empty_error())
            return []
        if listing_errors is not None and declared_scope.note():
            listing_errors.append(declared_scope.note())
        return sorted(declared_scope.projects)

    # Parsed before it is tested, so a blank or separator-only value reads as
    # unset rather than as an override that names nothing and skips discovery.
    monitored = set(os.environ.get(MONITORED_PROJECTS_ENV, "").replace(",", " ").split())
    raw_projects = set(monitored)
    for env_var in PROJECT_ENV_VARS:
        val = os.environ.get(env_var, "").strip()
        if val:
            raw_projects.add(val)
    if monitored:
        return sorted({_normalise_project_id(p) or p for p in raw_projects})

    # The host project is always in a discovered scope, whether or not a
    # `GCP_PROJECT_ID`-style variable also names one.
    rc, stdout, _ = run_cmd(list(CONFIG_PROJECT_CMD))
    if rc == 0 and stdout.strip():
        raw_projects.add(stdout.strip())
    projects = {
        resolved
        for p in sorted(raw_projects)
        if (resolved := _normalise_project_id(p, listing_errors)) is not None
    }
    rc, stdout, stderr = run_cmd(list(PROJECTS_LIST_CMD))
    if rc != 0 and listing_errors is not None:
        listing_errors.append(
            f"rc={rc}: {(stderr or '').strip()[:PROJECTS_LIST_ERROR_CHARS] or 'no stderr'}; "
            "the scope fell back to the configured project and other projects were not read"
        )
    if rc == 0:
        listed = {line.strip() for line in stdout.splitlines() if line.strip()}
        omitted = sorted(projects - listed)
        if omitted and listing_errors is not None:
            listing_errors.append(
                f"rc=0 but did not name {', '.join(omitted)}; the listing is filtered, "
                "so other projects may not have been read"
            )
        projects |= listed
    return sorted(projects or raw_projects)


def parse_version(text: str | None) -> tuple[int, int, int, int] | None:
    """`1.30.5-gke.1355000` -> (1, 30, 5, 1355000); None when it does not parse."""
    if not isinstance(text, str):
        return None
    m = VERSION_RE.match(text.strip())
    if not m:
        return None
    major, minor, patch, build = m.groups()
    return int(major), int(minor), int(patch), int(build or 0)


def minor_gap(target: tuple[int, int, int, int], member: tuple[int, int, int, int]) -> int | None:
    """Minors the member trails the target by; negative when ahead, None across majors."""
    if target[0] != member[0]:
        return None
    return target[1] - member[1]


def compare(target: tuple[int, int, int, int], member: tuple[int, int, int, int]) -> str:
    """One component's verdict against the target."""
    if member < target:
        return STATUS_LAGGING
    if member > target:
        return STATUS_AHEAD
    return STATUS_CURRENT


def lowest_node_pool(node_pools: list[dict]) -> tuple[dict | None, str | None]:
    """The pool with the lowest parsable version, and a note naming any pool skipped.

    A pool whose version does not parse is skipped and named, not allowed to hide a real
    lag on the pools that do parse; the result is None only when no pool parses.
    """
    if not isinstance(node_pools, list) or not node_pools:
        return None, "no nodePools in the cluster record"
    parsed = []
    skipped = []
    for pool in node_pools:
        if not isinstance(pool, dict):
            skipped.append(UNNAMED_POOL)
            continue
        version = parse_version(pool.get("version"))
        if version is None:
            skipped.append(f"{pool.get('name') or UNNAMED_POOL} ({pool.get('version')!r})")
            continue
        parsed.append((version, pool))
    note = f"node pool version unparsable, skipped: {', '.join(skipped)}" if skipped else None
    if not parsed:
        return None, note
    version, pool = min(parsed, key=lambda item: item[0])
    return {"name": pool.get("name", ""), "version": pool.get("version"), "status": pool.get("status", "")}, note


class ServerConfigCache:
    """`get-server-config` once per (project, location), as the SOP asks."""

    def __init__(self):
        self._cache: dict[tuple[str, str], tuple[dict | None, str | None]] = {}
        self.errors: list[dict] = []

    def get(self, project: str, location: str) -> tuple[dict | None, str | None]:
        key = (project, location)
        if key not in self._cache:
            cmd = [GCLOUD, "container", "get-server-config", f"--location={location}", f"--project={project}", JSON_FORMAT_FLAG]
            data, error = run_gcloud_json(cmd)
            if error is None and not isinstance(data, dict):
                data, error = None, f"{' '.join(cmd)} returned no server config"
            if error is not None:
                self.errors.append({"project": project, "location": location, "message": error})
            self._cache[key] = (data, error)
        return self._cache[key]


def channel_default(server_config: dict, channel: str) -> str | None:
    """The `defaultVersion` of the named channel in a server config, or None."""
    for entry in server_config.get("channels", []) or []:
        if isinstance(entry, dict) and entry.get("channel") == channel:
            return entry.get("defaultVersion")
    return None


def resolve_target(cluster: dict, project: str, explicit_target: str | None, cache: ServerConfigCache) -> tuple[str | None, str, str | None]:
    """(target_version, target_source, reason_when_missing) for one member."""
    if explicit_target:
        return explicit_target, TARGET_SOURCE_FLAG, None
    channel = (cluster.get("releaseChannel") or {}).get("channel", "") or ""
    if channel in NO_CHANNEL_VALUES:
        return None, EMPTY_CELL, "no release channel; pass --target-version"
    location = cluster.get("location", "")
    server_config, error = cache.get(project, location)
    if server_config is None:
        return None, CHANNEL_DEFAULT_LABEL.format(channel=channel), f"get-server-config failed for {location}"
    default = channel_default(server_config, channel)
    if not default:
        return None, CHANNEL_DEFAULT_LABEL.format(channel=channel), f"channel {channel} not in get-server-config for {location}"
    return default, CHANNEL_DEFAULT_LABEL.format(channel=channel), None


def grade_member(cluster: dict, project: str, explicit_target: str | None, cache: ServerConfigCache) -> dict:
    """One row: versions read, target chosen, and the verdict."""
    channel = (cluster.get("releaseChannel") or {}).get("channel", "") or ""
    master_text = cluster.get("currentMasterVersion")
    node_pools = cluster.get("nodePools") or []
    lowest, pool_reason = lowest_node_pool(node_pools)
    target_text, target_source, target_reason = resolve_target(cluster, project, explicit_target, cache)

    member = {
        "project": project,
        "cluster": cluster.get("name", ""),
        "location": cluster.get("location", ""),
        "channel": channel or None,
        "cluster_status": cluster.get("status", ""),
        "control_plane_version": master_text,
        "node_pools": [
            {"name": p.get("name", ""), "version": p.get("version"), "status": p.get("status", "")}
            for p in node_pools
            if isinstance(p, dict)
        ],
        "lowest_node_pool": lowest,
        "target_version": target_text,
        "target_source": target_source,
        "gap_minors": None,
        "status": STATUS_UNKNOWN,
        "note": "",
    }

    notes = []
    master = parse_version(master_text)
    target = parse_version(target_text) if target_text else None
    if master is None:
        notes.append(f"control plane version unparsable: {master_text!r}")
    if pool_reason:
        notes.append(pool_reason)
    if target_text is None:
        notes.append(target_reason)
    elif target is None:
        notes.append(f"target version unparsable: {target_text!r}")

    in_flight = [cluster.get("status", "")] + [p.get("status", "") for p in member["node_pools"]]
    if any(s in IN_FLIGHT_STATUSES for s in in_flight):
        notes.append("upgrade in flight (RECONCILING/PROVISIONING)")

    if master is not None and lowest is not None and target is not None:
        pool_version = parse_version(lowest["version"])
        lowest_component = min(master, pool_version)
        verdicts = {compare(target, master), compare(target, pool_version)}
        member["gap_minors"] = minor_gap(target, lowest_component)
        if STATUS_LAGGING in verdicts:
            # Below the target somewhere: a minor or major behind is lagging; the same
            # minor with a lower patch or build is patch-behind.
            member["status"] = STATUS_LAGGING if member["gap_minors"] != 0 else STATUS_PATCH_BEHIND
        elif verdicts == {STATUS_CURRENT}:
            member["status"] = STATUS_CURRENT
        else:
            member["status"] = STATUS_AHEAD
        if member["gap_minors"] is None:
            notes.append("major version differs from the target; minor gap undefined")

    member["note"] = NOTE_SEPARATOR.join(n for n in notes if n)
    return member


def default_kubeconfig_dir() -> str:
    """`$HERMES_HOME/.kubeconfigs`, resolved at run time so the environment decides."""
    return os.path.join(os.environ.get(HERMES_HOME_ENV, DEFAULT_HERMES_HOME), KUBECONFIG_SUBDIR)


def _kubeconfig_slug(value) -> str:
    return KUBECONFIG_SLUG_RE.sub(KUBECONFIG_SLUG_REPLACEMENT, str(value or "")) or KUBECONFIG_SLUG_EMPTY


def kubeconfig_path(kubeconfig_dir: str, project: str, cluster: str, location: str) -> str:
    """One kubeconfig per (project, cluster, location), named as platform_mcp_server names them."""
    name = KUBECONFIG_FILE_FORMAT.format(project=_kubeconfig_slug(project), cluster=_kubeconfig_slug(cluster), location=_kubeconfig_slug(location))
    return os.path.join(kubeconfig_dir, name)


def dns_endpoint_args(cluster: dict) -> list[str]:
    """`[--dns-endpoint]` when the record's DNS endpoint accepts external traffic, else nothing."""
    dns = ((cluster.get("controlPlaneEndpointsConfig") or {}).get("dnsEndpointConfig") or {})
    if dns.get("endpoint") and dns.get("allowExternalTraffic") is True:
        return [DNS_ENDPOINT_FLAG]
    return []


def get_credentials_cmd(cluster: dict, project: str) -> list[str]:
    return [
        GCLOUD, "container", "clusters", "get-credentials", cluster.get("name", ""),
        f"--location={cluster.get('location', '')}", f"--project={project}",
        *dns_endpoint_args(cluster),
    ]


def _kubectl_get(resources: str, env: dict, namespaced: bool) -> tuple[list | None, str | None]:
    """(items, error) of one `kubectl get <resources> [-A] -o json`; a list the credential
    proxy cut at its output cap is an error naming the cap."""
    cmd = [KUBECTL, "get", resources, *([ALL_NAMESPACES_FLAG] if namespaced else []), *JSON_OUTPUT_ARGS]
    rc, stdout, stderr = run_cmd(cmd, KUBECTL_TIMEOUT_SECONDS, env)
    if rc != 0:
        return None, f"{' '.join(cmd)} failed ({rc}): {stderr.strip()}"
    if PROXY_TRUNCATION_MARKER in stderr:
        return None, KUBECTL_OUTPUT_CUT.format(cmd=" ".join(cmd))
    try:
        data = json.loads(stdout) if stdout.strip() else {}
    except ValueError as e:
        return None, f"{' '.join(cmd)} returned unparsable JSON: {e}"
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None, f"{' '.join(cmd)} returned no items list"
    return items, None


def prepare_kubeconfig(kubeconfig_dir: str, path: str) -> str | None:
    """The kubeconfig directory usable before `get-credentials` writes `path`
    into it, and whatever stands at `path` that is not this uid's own
    writable file out of the way. The default shared directory follows
    `collect.prepare_kubeconfig`'s rule and its contract: made at
    `KUBECONFIG_DIR_MODE` whatever the umask, brought to it when this uid
    owns it at another mode (or kept at that mode, said on stderr, when the
    chmod fails and it is this uid's alone), moved aside (said on stderr) and
    remade when another uid's; a link at the file's name removed, a directory
    moved aside, another uid's or an unwritable file removed; None when it is
    usable, else why not, with the owner named, which the caller reports as
    the member's read error. A directory the caller named is made under the
    caller's umask."""
    shared = os.path.realpath(kubeconfig_dir) == os.path.realpath(default_kubeconfig_dir())
    if not shared:
        try:
            os.makedirs(kubeconfig_dir, exist_ok=True)
        except OSError as exc:
            return KUBECONFIG_NOT_MADE_FORMAT.format(directory=kubeconfig_dir, error=exc)
    else:
        euid = os.geteuid()
        if os.path.islink(kubeconfig_dir):
            # Never followed, as the shared rule reads it: a root run would
            # otherwise chmod and write into whatever it points at.
            return KUBECONFIG_IS_SYMLINK_FORMAT.format(directory=kubeconfig_dir)
        try:
            current = os.stat(kubeconfig_dir)
        except FileNotFoundError:
            current = None
        except OSError as exc:
            # A parent this uid cannot search, or a file where the directory
            # should be: the member's read error, as the old guard made it.
            return KUBECONFIG_NOT_READ_FORMAT.format(directory=kubeconfig_dir, error=exc)
        if current is not None and current.st_uid != euid and euid != ROOT_UID:
            aside = os.path.join(os.path.dirname(kubeconfig_dir), KUBECONFIG_UNUSABLE_FORMAT.format(name=os.path.basename(kubeconfig_dir), stamp=datetime.now(timezone.utc).strftime(KUBECONFIG_STAMP_FORMAT)))
            try:
                os.rename(kubeconfig_dir, aside)
            except OSError as exc:
                if exc.errno not in KUBECONFIG_MOVED_BY_ANOTHER_ERRNOS:
                    return KUBECONFIG_NOT_MOVED_FORMAT.format(directory=kubeconfig_dir, uid=current.st_uid, euid=euid, error=exc)
                # Another run moved it first: read what stands there now and
                # go on, as the shared rule does.
                try:
                    current = os.stat(kubeconfig_dir)
                except FileNotFoundError:
                    current = None
                except OSError as read_exc:
                    return KUBECONFIG_NOT_READ_FORMAT.format(directory=kubeconfig_dir, error=read_exc)
            else:
                print(KUBECONFIG_MOVED_ASIDE_FORMAT.format(directory=kubeconfig_dir, uid=current.st_uid, aside=os.path.basename(aside)), file=sys.stderr)
                current = None
        if current is None:
            # Made at the caller's umask and chmodded below: the umask is
            # process-wide, and a toggle here would race the other threads.
            try:
                os.makedirs(kubeconfig_dir, exist_ok=True)
                current = os.stat(kubeconfig_dir)
            except OSError as exc:
                return KUBECONFIG_NOT_MADE_FORMAT.format(directory=kubeconfig_dir, error=exc)
        if (current.st_mode & KUBECONFIG_DIR_MODE_MASK) != KUBECONFIG_DIR_MODE:
            # Through a descriptor opened without following, as the shared rule.
            try:
                opened = os.open(kubeconfig_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError as exc:
                return KUBECONFIG_NOT_READ_FORMAT.format(directory=kubeconfig_dir, error=exc)
            try:
                os.fchmod(opened, KUBECONFIG_DIR_MODE)
            except OSError as exc:
                # Judged on the directory the descriptor holds, as the shared rule.
                actual = os.fstat(opened)
                mode = actual.st_mode & KUBECONFIG_DIR_MODE_MASK
                private = actual.st_uid == euid and (mode & KUBECONFIG_DIR_OWNER_WRITE_SEARCH) == KUBECONFIG_DIR_OWNER_WRITE_SEARCH and not mode & KUBECONFIG_DIR_OTHERS_WRITE
                if not private:
                    return KUBECONFIG_MODE_NOT_CHANGED_FORMAT.format(directory=kubeconfig_dir, mode=oct(mode), wanted=oct(KUBECONFIG_DIR_MODE), error=exc)
                print(KUBECONFIG_MODE_KEPT_FORMAT.format(directory=kubeconfig_dir, mode=oct(mode), wanted=oct(KUBECONFIG_DIR_MODE), error=exc), file=sys.stderr)
            finally:
                os.close(opened)
    try:
        current = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return KUBECONFIG_FILE_NOT_READ_FORMAT.format(path=path, error=exc)
    if stat.S_ISLNK(current.st_mode):
        # A link planted at the file's name is removed, never written
        # through, as the shared rule removes it.
        try:
            os.unlink(path)
        except OSError as exc:
            return KUBECONFIG_LINK_NOT_REMOVED_FORMAT.format(path=path, error=exc)
    elif not stat.S_ISREG(current.st_mode):
        # A directory or other object planted at the name: moved aside,
        # never entered, as the shared rule moves it.
        aside = os.path.join(os.path.dirname(path), KUBECONFIG_UNUSABLE_FORMAT.format(name=os.path.basename(path), stamp=datetime.now(timezone.utc).strftime(KUBECONFIG_STAMP_FORMAT)))
        try:
            os.rename(path, aside)
        except OSError as exc:
            return KUBECONFIG_NOT_A_FILE_FORMAT.format(path=path, error=exc)
        print(KUBECONFIG_NOT_A_FILE_MOVED_FORMAT.format(path=path, aside=os.path.basename(aside)), file=sys.stderr)
    elif current.st_uid != os.geteuid() or not os.access(path, os.W_OK):
        # Another uid's file, or one this uid cannot write: replaced.
        try:
            os.unlink(path)
        except OSError as exc:
            return KUBECONFIG_NOT_REPLACED_FORMAT.format(path=path, error=exc)
    return None

def parse_node_lines(stdout: str) -> tuple[list[dict] | None, str | None]:
    """(nodes, error): the projected Node objects from the jsonpath read's lines."""
    nodes = []
    for line in stdout.split(NODE_LINE_SEPARATOR):
        if not line.strip():
            continue
        fields = line.split(NODE_FIELD_SEPARATOR)
        if len(fields) != NODE_FIELD_COUNT:
            return None, f"node line has {len(fields)} field(s), not {NODE_FIELD_COUNT}: {line[:NOTE_ERROR_CHARS]!r}"
        try:
            labels, taints, unschedulable, info, allocatable, conditions = (json.loads(f) if f.strip() else None for f in fields[1:])
        except ValueError as e:
            return None, f"node line unparsable: {e}"
        nodes.append({"kind": NODE_KIND, "metadata": {"name": fields[0], "labels": labels or {}}, "spec": {"taints": taints or [], "unschedulable": bool(unschedulable)}, "status": {"nodeInfo": info or {}, "allocatable": allocatable or {}, "conditions": conditions or []}})
    return nodes, None


def _kubectl_nodes(env: dict) -> tuple[list[dict] | None, str | None]:
    """(nodes, error) of the projected node read; a cut read is an error naming the cap even
    when the cut fell on a line boundary and the lines before it parse."""
    cmd = [KUBECTL, "get", KUBECTL_NODE_RESOURCE, OUTPUT_FLAG, JSONPATH_OUTPUT_FORMAT.format(template=KUBECTL_NODE_JSONPATH)]
    rc, stdout, stderr = run_cmd(cmd, KUBECTL_TIMEOUT_SECONDS, env)
    if rc != 0:
        return None, f"{' '.join(cmd[:3])} failed ({rc}): {stderr.strip()}"
    if PROXY_TRUNCATION_MARKER in stderr:
        return None, KUBECTL_OUTPUT_CUT.format(cmd=" ".join(cmd[:3]))
    return parse_node_lines(stdout)


def _later_read(resources: str, mode: str, env: dict) -> tuple[list | None, str | None]:
    """One read of LATER_READS, by its mode."""
    if mode == READ_MODE_NODE_JSONPATH:
        return _kubectl_nodes(env)
    return _kubectl_get(resources, env, mode == READ_MODE_NAMESPACED_JSON)


def first_read_failed_before_the_server_answered(error: str) -> bool:
    """Whether a first read's error is one of FIRST_READ_SKIP_MARKERS' classes and not an answer
    the API server returned: kubectl prefixes a returned error with `Error from server (...)`, and
    its body may quote a dial failure of the server's own (an InternalError on the path to etcd),
    which the second read may still be answered through."""
    lowered = error.lower()
    return SERVER_ANSWERED_PREFIX not in lowered and any(marker in lowered for marker in FIRST_READ_SKIP_MARKERS)


def read_cluster_objects(cluster: dict, project: str, kubeconfig_dir: str) -> dict:
    """The objects of one member, read through kubectl, as the `read` dict the per-entry rules
    take: `kubeconfig`; `items`, every object the PDB read and the reads after the webhook
    read returned; `error`, the credentials or PDB-read failure (`credentials_error` and
    `directory_error` name those two causes); `webhook_items`, `webhook_error` and
    `webhook_skipped`, the webhook read's outcome; and `read_errors`, keyed by kind for the
    reads after the webhook read, each None when the kind was read, else the error of the read
    that carried it, or, when the kubeconfig, `get-credentials` or the PDB read failed, the
    reason those reads did not run (no read ran, or the reads after the webhook read were
    skipped, which says nothing of the webhook read itself), so a member whose first read
    failed never reads as nine successful reads.

    `get-credentials` writes the member's kubeconfig; then one `kubectl get` reads every
    PodDisruptionBudget, Deployment and StatefulSet, and a second reads the webhook
    configurations, Services, EndpointSlices and Namespaces. A failed credentials fetch fails
    both, and so does a kubeconfig directory that cannot be created. A PDB read that failed
    before the API server answered it (a connection, credential-plugin or certificate failure,
    or the credential proxy unreachable; not the deadline, which in the sandbox measures the
    credential proxy's queue, and not an error the server returned, whatever it quotes) skips
    the webhook read rather than spend a second timeout on it (`webhook_skipped`, with
    `webhook_error` saying so); a PDB read the server answered with an error still gets the
    webhook read, which fails alone. Either way a failed PDB read leaves `items` None, skips
    the reads after the webhook read, grades the member `unknown` on the PDB rule and on every
    per-entry rule that needs an object, and exits 1. Those reads, one per group in
    LATER_READS, each fail on their own: the kinds that answered reach the rules, the failed or
    cut read is recorded per kind for the rules that need it, and the exit code is untouched.
    """
    path = kubeconfig_path(kubeconfig_dir, project, cluster.get("name", ""), cluster.get("location", ""))
    read = {"kubeconfig": path, "items": None, "error": None, "webhook_items": None, "webhook_error": None, "webhook_skipped": False, "credentials_error": None, "directory_error": None, "read_errors": {kind: None for kind in readiness_rules.READ_KINDS}}
    # The kubeconfig rule makes the directory usable, or says why not: its
    # refusal is the directory's, failing both reads as a directory that
    # could not be made does.
    note = prepare_kubeconfig(kubeconfig_dir, path)
    if note:
        read["error"] = read["webhook_error"] = read["directory_error"] = KUBECONFIG_NOT_PREPARED_FORMAT.format(note=note)
        return _later_reads_skipped(read, readiness_rules.NO_READ_RAN_REASON)
    env = {**os.environ, KUBECONFIG_ENV: path}
    cmd = get_credentials_cmd(cluster, project)
    rc, _, stderr = run_cmd(cmd, GCLOUD_TIMEOUT_SECONDS, env)
    if rc != 0:
        read["error"] = read["webhook_error"] = read["credentials_error"] = f"{' '.join(cmd)} failed ({rc}): {stderr.strip()}"
        return _later_reads_skipped(read, readiness_rules.NO_READ_RAN_REASON)
    read["items"], read["error"] = _kubectl_get(KUBECTL_RESOURCES, env, True)
    if read["error"] is not None and first_read_failed_before_the_server_answered(read["error"]):
        read["webhook_skipped"] = True
        read["webhook_error"] = f"{WEBHOOK_READ_SKIPPED} ({read['error']})"
        return _later_reads_skipped(read, readiness_rules.LATER_READS_SKIPPED_REASON)
    read["webhook_items"], read["webhook_error"] = _kubectl_get(KUBECTL_WEBHOOK_RESOURCES, env, True)
    if read["error"] is not None:
        return _later_reads_skipped(read, readiness_rules.LATER_READS_SKIPPED_REASON)
    for kinds, resources, mode in LATER_READS:
        objects, error = _later_read(resources, mode, env)
        if error is not None:
            for kind in kinds:
                read["read_errors"][kind] = error
        else:
            read["items"].extend(objects)
    return read


def _pool_records(cluster: dict, member: dict) -> list[dict]:
    """The pools as the rules read them: name, version, parsed version, and the record's config and autoscaling."""
    records = {p.get("name", ""): p for p in cluster.get("nodePools") or [] if isinstance(p, dict)}
    return [
        {"name": p["name"], "version": p["version"], "parsed": parse_version(p["version"]), "config": records.get(p["name"], {}).get("config") or {}, "autoscaling": records.get(p["name"], {}).get("autoscaling") or {}}
        for p in member["node_pools"]
    ]


def _error_excerpt(error: str) -> str:
    return readiness_rules.one_line(error, NOTE_ERROR_CHARS) if error else ""


def _later_reads_skipped(read: dict, reason: str) -> dict:
    """`read` with every kind of the reads after the webhook read marked as not read, for
    `reason`: no read ran (the kubeconfig directory or `get-credentials` failed), or the PDB
    read failed, whether or not the webhook read ran after it. `read_errors` then says so per
    kind, the note names the kinds, and the JSON cannot be mistaken for nine successful reads."""
    read["read_errors"] = {kind: reason for kind in readiness_rules.READ_KINDS}
    return read


def _read_error_notes(read_errors: dict) -> list[str]:
    """One note per failed read, naming the kinds it carried, in the order the reads run."""
    kinds_by_error: dict[str, list[str]] = {}
    for kind in readiness_rules.READ_KINDS:
        error = read_errors.get(kind)
        if error:
            kinds_by_error.setdefault(error, []).append(kind)
    return [NOTE_READ_FAILED.format(kinds=readiness.LIST_SEPARATOR.join(kinds), error=_error_excerpt(error)) for error, kinds in kinds_by_error.items()]


def readiness_context(member: dict, at: datetime, clusters: list[dict] | None = None, pools: list[dict] | None = None, master=None, autopilot: bool = False) -> dict:
    """The per-member read context the per-entry rules share (readiness_rules documents the
    keys): the member's project, location and name for a log filter, this module's runner
    with its per-call cap, the instant, a cache a rule fills so another reads it once, every
    cluster record the run listed, the member's pools, target, control plane and Autopilot."""
    return {
        readiness_rules.CONTEXT_PROJECT: member["project"],
        readiness_rules.CONTEXT_LOCATION: member["location"],
        readiness_rules.CONTEXT_CLUSTER_NAME: member["cluster"],
        readiness_rules.CONTEXT_RUN_CMD: run_cmd,
        readiness_rules.CONTEXT_TIMEOUT_SECONDS: GCLOUD_TIMEOUT_SECONDS,
        readiness_rules.CONTEXT_AT: at,
        readiness_rules.CONTEXT_CACHE: {},
        readiness_rules.CONTEXT_CLUSTERS: list(clusters) if clusters is not None else [],
        readiness_rules.CONTEXT_POOLS: list(pools) if pools is not None else [],
        readiness_rules.CONTEXT_TARGET_TEXT: member["target_version"],
        readiness_rules.CONTEXT_MASTER: master,
        readiness_rules.CONTEXT_AUTOPILOT: autopilot,
    }


def assess_readiness(cluster: dict, member: dict, read: dict, at: datetime, fleet: list[dict] | None = None) -> dict:
    """The member's `readiness` object: the PDB, maintenance, skew and webhook rules, then
    EXTRA_RULES, against the row's target. `fleet` is every cluster record in the run, for the
    rules that compare peers."""
    target = parse_version(member["target_version"]) if member["target_version"] else None
    master = parse_version(member["control_plane_version"])
    pools = _pool_records(cluster, member)
    autopilot = bool((cluster.get("autopilot") or {}).get("enabled"))
    items, read_error, webhook_items = read["items"], read["error"], read.get("webhook_items")
    pdbs = None if items is None else readiness.grade_pdbs(*readiness.split_items(items))
    webhooks = None if webhook_items is None else readiness.grade_webhooks(*readiness.split_webhook_items(webhook_items))
    maintenance = readiness.evaluate_maintenance(cluster.get("maintenancePolicy"), at, member["target_version"], target, master, pools)
    skew = readiness.evaluate_skew(target, pools, autopilot)
    context = readiness_context(member, at, fleet if fleet is not None else [cluster], pools, master, autopilot)
    rules = readiness.evaluate_extra_rules(cluster, member, read, target, context)
    status = readiness.readiness_status(pdbs, webhooks, maintenance, skew, target is not None, rules)

    notes = []
    if read.get("directory_error"):
        notes.append(NOTE_DIRECTORY_FAILED)
    elif read.get("credentials_error"):
        notes.append(NOTE_CREDENTIALS_FAILED)
    else:
        if read_error:
            notes.append(NOTE_PDB_READ_FAILED)
        if read.get("webhook_skipped"):
            notes.append(NOTE_WEBHOOK_READ_SKIPPED)
        elif read.get("webhook_error"):
            notes.append(NOTE_WEBHOOK_READ_FAILED)
    notes.extend(_read_error_notes(read["read_errors"]))
    if target is None:
        notes.append("no target; exclusion scope and skew not graded")
    if pdbs:
        if pdbs["scaled_to_zero"]:
            notes.append(f"{pdbs['scaled_to_zero']} PDB(s) on scaled-to-zero workloads skipped")
        if pdbs["orphan"]:
            notes.append(f"{pdbs['orphan']} orphan PDB(s) matching no workload skipped")
        if pdbs["unmatched"]:
            notes.append(f"{pdbs['unmatched']} PDB(s) cover pods of no Deployment or StatefulSet; not graded")
    if maintenance["undecided_exclusions"]:
        notes.append(f"exclusion(s) not evaluated: {readiness.LIST_SEPARATOR.join(maintenance['undecided_exclusions'])}")
    if maintenance["window"]["state"] == readiness.WINDOW_NOT_EVALUATED:
        notes.append(maintenance["window"]["detail"])
    if skew["at_ceiling"]:
        notes.append(f"pool(s) at the skew ceiling: {readiness.LIST_SEPARATOR.join(skew['at_ceiling'])}")
    if skew["applicable"] and skew["unknown"] and target is not None:
        notes.append(f"pool version unparsable, skew unknown: {readiness.LIST_SEPARATOR.join(skew['unknown'])}")
    notes.extend(item[readiness.TEXT_KEY] for item in rules["unknown"])
    notes.extend(rules["notes"])

    return {
        "status": status,
        "evaluated_at": at.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT),
        "kubeconfig": read["kubeconfig"],
        "read_error": read_error,
        "webhook_read_error": read.get("webhook_error"),
        "webhook_read_skipped": bool(read.get("webhook_skipped")),
        "read_errors": read["read_errors"],
        "autopilot": autopilot,
        "pdbs": pdbs,
        "webhooks": webhooks,
        "maintenance": maintenance,
        "skew": skew,
        "rules": rules["rules"],
        "rule_blocking": rules["blocking"],
        "risks": rules["risks"],
        "unknown": rules["unknown"],
        "notes": rules["notes"],
        # From the cache as the reads left it: an entry a rule wrote in another shape is that
        # rule's `unknown` (the fold checks the cache around each rule) and is not read here.
        "audit_log": audit_log.summaries(context),
        "note": NOTE_SEPARATOR.join(notes),
    }


def parse_cluster_spec(spec: str) -> tuple[str, str] | None:
    """`(location, name)` for `<location>/<name>`, `("", name)` for a bare name, and
    None for anything else: an empty spec, an empty half, a second slash, or a
    half outside the GKE name grammar (`CLUSTER_SPEC_HALF_RE`), none of which any
    GKE location or cluster name carries. The grammar lives here alone; `main`
    refuses what this rejects before any read."""
    location, slash, name = spec.partition(CLUSTER_SPEC_SEPARATOR)
    if not slash:
        return ("", spec) if CLUSTER_SPEC_HALF_RE.fullmatch(spec) else None
    if not (CLUSTER_SPEC_HALF_RE.fullmatch(location) and CLUSTER_SPEC_HALF_RE.fullmatch(name)):
        return None
    return location, name


def _wanted(cluster: dict, wanted: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Every `--cluster` spec this cluster satisfies: its qualified form, its bare form,
    or both when the caller named it both ways. Empty when it was not asked for."""
    name = cluster.get("name", "")
    location = cluster.get("location", "")
    return [spec for spec in ((location, name), ("", name)) if spec in wanted]


def build_report(projects: list[str], explicit_target: str | None, readiness_options: dict | None = None, clusters: list[str] | None = None) -> dict:
    """Enumerates every project and grades every member; one failure never aborts the rest.

    `readiness_options` (`at`, `kubeconfig_dir`) turns on the per-member readiness read and
    grade; None leaves the report as the version table alone. `clusters`, when given, is the
    names to grade; every other cluster in the projects is skipped, so a caller that wants a
    few members' readiness does not pay for the others' reads, and the report records them
    under `narrowed_to` (`None` on a full run). The per-entry rules still see every cluster
    the projects listed as the member's peers: narrowing selects whom to grade, not whom a
    rule may see.
    """
    # `<location>/<name>` pins one cluster; a bare name admits that name in
    # every location of the projects (GKE names are unique per location).
    # `main` refuses any other form before it gets here; a direct caller is
    # refused the same way rather than matched against every location.
    wanted: set[tuple[str, str]] | None = None
    narrowed_to: list[str] | None = None
    if clusters:
        wanted = set()
        for spec in clusters:
            parsed = parse_cluster_spec(spec)
            if parsed is None:
                raise ValueError(INVALID_CLUSTER_SPEC.format(spec=spec))
            wanted.add(parsed)
        narrowed_to = sorted(set(clusters))
    matched: set[tuple[str, str]] = set()
    cache = ServerConfigCache()
    members: list[dict] = []
    errors: list[dict] = []
    # Projects whose listing failed: a spec can match nothing there, and the
    # unmatched line below says so instead of claiming no match.
    unlisted: list[str] = []
    audit_log_failures: list[dict] = []  # apart from `errors`: a member whose audit-log read failed stays graded
    # Every project is listed before any member is graded, so a rule that compares a
    # member with its peers sees the whole run, not the projects listed so far; and the
    # peers are the whole listing even when `--cluster` narrows who is graded (`found`):
    # narrowing selects whom to grade, not whom a rule may see.
    found: list[tuple[str, dict]] = []
    fleet: list[dict] = []
    for project in projects:
        cmd = [GCLOUD, "container", "clusters", "list", f"--project={project}", JSON_FORMAT_FLAG]
        listed, error = run_gcloud_json(cmd)
        if error is not None or not isinstance(listed, list):
            if error is not None and any(marker in error for marker in API_DISABLED_MARKERS):
                ours, why_not = refusal_names_project(project, error)
                if ours:
                    continue
                error = f"{error} ({why_not})"
            errors.append({"project": project, "location": None, "message": error or f"{' '.join(cmd)} returned no list"})
            unlisted.append(project)
            continue
        # Narrowed by --scope-exclude-clusters, which the agent passes with the
        # fleet_scope tool's collector_args (#3081). The daily upgrade-readiness
        # watch (upgrade_readiness_watch.py) builds its own --project flags and
        # passes no exclusion, so its report still covers excluded clusters
        # (#3026), as the CRD field documents.
        for cluster in declared_scope.drop_excluded(project, listed):
            if not isinstance(cluster, dict):
                continue
            fleet.append(cluster)
            if wanted is not None:
                hits = _wanted(cluster, wanted)
                if not hits:
                    continue
                # Every spec the cluster satisfies counts, not the first: a bare and a
                # qualified spec for one cluster are one request, not a hit and a miss.
                matched.update(hits)
            found.append((project, cluster))
    for project, cluster in found:
        member = grade_member(cluster, project, explicit_target, cache)
        if readiness_options is not None:
            read = read_cluster_objects(cluster, project, readiness_options["kubeconfig_dir"])
            # A credentials failure fills both slots with one message; a skipped webhook read is not a failure and gets no row.
            failed = (read["error"], None if read.get("webhook_skipped") else read.get("webhook_error"))
            for message in dict.fromkeys(m for m in failed if m):
                row = {"project": project, "location": member["location"], "cluster": member["cluster"], "message": message}
                if message != read["error"]:
                    row[ERROR_READ_KEY] = WEBHOOK_READ
                errors.append(row)
            member["readiness"] = assess_readiness(cluster, member, read, readiness_options["at"], fleet)
            for which, log in (member["readiness"].get("audit_log") or {}).items():
                if log.get("error"):
                    audit_log_failures.append({"project": project, "location": member["location"], "cluster": member["cluster"], "read": which, "message": log["error"]})
        members.append(member)
    errors.extend(cache.errors)
    listed = [p for p in projects if p not in unlisted]
    excluded_by_spec: dict[tuple[str, str], list[str]] = {}
    for project in projects:
        for name, location in declared_scope.dropped.get(project, []):
            for hit in _wanted({"name": name, "location": location}, wanted or set()):
                excluded_by_spec.setdefault(hit, []).append(f"{project}/{location}/{name}")
    for location, name in sorted((wanted or set()) - matched):
        spec = f"{location}/{name}" if location else name
        if (location, name) in excluded_by_spec:
            message = EXCLUDED_CLUSTER_SPEC.format(spec=spec, clusters=", ".join(excluded_by_spec[(location, name)]))
        elif not unlisted:
            message = UNMATCHED_CLUSTER_SPEC.format(spec=spec, projects=", ".join(projects))
        elif listed:
            message = UNMATCHED_CLUSTER_SPEC_PARTIAL.format(spec=spec, listed=", ".join(listed), unlisted=", ".join(unlisted))
        else:
            message = UNMATCHED_CLUSTER_SPEC_UNLISTED.format(spec=spec, unlisted=", ".join(unlisted))
        errors.append({"project": None, "location": location or None, "cluster": name, "message": message})
    members.sort(key=lambda m: (m["project"], m["location"], m["cluster"]))
    report = {
        "target_version": explicit_target,
        "projects": list(projects),
        # The `--cluster` specs this run was narrowed to, `None` on a full run,
        # so a reader of `members[]` (the deprecation scan's floor, the audit
        # collector's join) can tell a narrowed file from a full run whose
        # project held one cluster. A `--project` narrowing is already visible
        # in `projects`; this is the one the file otherwise hides.
        "narrowed_to": narrowed_to,
        "members": members,
        "errors": errors,
        "summary": {status: sum(1 for m in members if m["status"] == status) for status in STATUS_ORDER},
    }
    if readiness_options is not None:
        report["readiness"] = {
            "evaluated_at": readiness_options["at"].astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT),
            "kubeconfig_dir": readiness_options["kubeconfig_dir"],
            "summary": {status: sum(1 for m in members if m["readiness"]["status"] == status) for status in readiness.READINESS_ORDER},
            "members_with_risks": sum(1 for m in members if m["readiness"].get("risks")),
            "audit_log_failures": audit_log_failures,
        }
    return report


def _cell(value) -> str:
    """One Markdown cell: the value on one line (a line break would end the row and strand
    the columns after it), with its pipes escaped."""
    if value is None or value == "":
        return EMPTY_CELL
    text = readiness_rules.one_line(value)
    return text.replace("|", "\\|") if text else EMPTY_CELL


def render_table(report: dict) -> str:
    """The Markdown table plus a summary line and any errors, for the chat reply."""
    lines = [
        "| " + " | ".join(TABLE_COLUMNS) + " |",
        "| " + " | ".join(TABLE_SEPARATOR_CELL for _ in TABLE_COLUMNS) + " |",
    ]
    for m in report["members"]:
        lowest = m["lowest_node_pool"]
        lowest_cell = f"{lowest['version']} ({lowest['name']})" if lowest else None
        target_cell = None
        if m["target_version"]:
            target_cell = m["target_version"]
            if m["target_source"] != TARGET_SOURCE_FLAG:
                target_cell = f"{m['target_version']} {m['target_source']}"
        elif m["target_source"] != EMPTY_CELL:
            target_cell = m["target_source"]
        row = (
            m["project"],
            m["cluster"],
            m["location"],
            m["channel"],
            m["control_plane_version"],
            lowest_cell,
            target_cell,
            m["gap_minors"],
            m["status"],
            m["note"],
        )
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    summary = report["summary"]
    lines.append("")
    lines.append(
        f"{len(report['members'])} member(s) across {len(report['projects'])} project(s): "
        + ", ".join(f"{summary[s]} {s}" for s in STATUS_ORDER)
        + (f"; target {report['target_version']}" if report["target_version"] else "; target: each cluster's channel default")
    )
    for err in report["errors"]:
        lines.append(READ_FAILED_LINE.format(where=_where(err), message=err["message"]))
    for failure in (report.get("readiness") or {}).get("audit_log_failures") or []:
        lines.append(AUDIT_LOG_FAILURE_LINE.format(where=_where(failure), read=failure["read"], error=failure["message"]))
    return "\n".join(lines)


def _where(entry: dict) -> str:
    """`project (location) cluster name`, as far as the entry names them: a location stands
    bare when no project precedes it (an unmatched `--cluster` spec names no project), and
    an entry naming nothing is `the run`."""
    parts = [entry["project"]] if entry.get("project") else []
    if entry.get("location"):
        parts.append(WHERE_LOCATION.format(location=entry["location"]) if parts else entry["location"])
    if entry.get("cluster"):
        parts.append(WHERE_CLUSTER.format(cluster=entry["cluster"]))
    return WHERE_SEPARATOR.join(parts) or WHERE_NOTHING


def _pdb_cell(r: dict) -> str:
    if r["pdbs"] is None:
        return READINESS_READ_FAILED_CELL
    if not r["pdbs"]["blocking"]:
        return READINESS_NONE_CELL
    return NOTE_SEPARATOR.join(readiness.describe_finding(f) for f in r["pdbs"]["blocking"])


def _webhook_cell(r: dict) -> str:
    """Blocking findings first (the upgrade's path, or the bootstrap-policy writes a dead webhook reaching `kube-system` or `kube-public` would refuse); unreachable-backend webhooks outside both after them,
    since they are an outage now even though they do not grade the member."""
    webhooks = r["webhooks"]
    if webhooks is None:
        return READINESS_READ_SKIPPED_CELL if r.get("webhook_read_skipped") else READINESS_READ_FAILED_CELL
    findings = webhooks["blocking"] + webhooks["outage"]
    if not findings:
        return READINESS_NONE_CELL
    return NOTE_SEPARATOR.join(readiness.describe_webhook_finding(f) for f in findings)


def _maintenance_cell(r: dict) -> str:
    parts = []
    for e in r["maintenance"]["exclusions"]:
        if e["in_effect"] or e["blocks"] is None:
            parts.append(f"exclusion {e['name']} ({e['scope']}) {e['detail']}")
    if not parts:
        parts.append("no exclusion in effect")
    window = r["maintenance"]["window"]
    if window["kind"] == readiness.WINDOW_NONE:
        parts.append("no maintenance window")
    elif window["state"] == readiness.WINDOW_NOT_EVALUATED:
        parts.append(f"window {window['recurrence']!r} {READINESS_NOT_EVALUATED_CELL}")
    elif window["state"] == readiness.WINDOW_OPEN:
        parts.append(f"window {window['detail']}: open, closes {window['closes_at']}")
    else:
        parts.append(f"window {window['detail']}: closed, next opening {window['next_opening'] or READINESS_NO_OPENING_CELL}")
    return NOTE_SEPARATOR.join(parts)


def _skew_cell(r: dict) -> str:
    skew = r["skew"]
    if not skew["applicable"]:
        return f"{readiness.SKEW_NOT_APPLICABLE} ({skew['reason']})"
    if skew["reason"]:
        return f"{READINESS_NOT_EVALUATED_CELL} ({skew['reason']})"
    parts = []
    for p in skew["pools"]:
        if p["verdict"] == readiness.SKEW_BLOCKS:
            parts.append(f"{p['name']} {p['version']}: {p['detail']}")
        elif p["verdict"] == readiness.SKEW_AT_CEILING:
            parts.append(f"{p['name']} {p['version']}: {readiness.SKEW_AT_CEILING} ({p['minors_behind_target']} minors behind the target)")
    if parts:
        return NOTE_SEPARATOR.join(parts)
    behind = [p["minors_behind_target"] for p in skew["pools"] if p["minors_behind_target"] is not None and p["minors_behind_target"] > 0]
    return f"{readiness.SKEW_OK} (at most {max(behind)} minor(s) behind the target)" if behind else readiness.SKEW_OK


def _rule_cell(r: dict, key: str) -> str:
    """The per-entry rules' blockers or risks, each as `<rule id>: <text>` so the table names
    the rule the way the note column does; `none` when no rule filed one. The rules run
    whether or not the cluster was read, and a rule that could not read grades `unknown`,
    which the note column prints: the cell answers for the rules, never for the PDB read."""
    findings = r[key]
    if findings:
        return NOTE_SEPARATOR.join(readiness.RULE_PREFIX_FORMAT.format(rule=f[readiness.RULE_KEY], text=f[readiness.TEXT_KEY]) for f in findings)
    return READINESS_NONE_CELL


def render_readiness(report: dict) -> str:
    """The readiness table printed after the version table, with its own summary line."""
    lines = [
        "| " + " | ".join(READINESS_COLUMNS) + " |",
        "| " + " | ".join(TABLE_SEPARATOR_CELL for _ in READINESS_COLUMNS) + " |",
    ]
    for m in report["members"]:
        r = m["readiness"]
        row = (m["project"], m["cluster"], m["location"], r["status"], _pdb_cell(r), _webhook_cell(r), _maintenance_cell(r), _skew_cell(r), _rule_cell(r, "rule_blocking"), _rule_cell(r, "risks"), r["note"])
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    summary = report["readiness"]["summary"]
    lines.append("")
    lines.append(
        f"Readiness at {report['readiness']['evaluated_at']}: "
        + ", ".join(f"{summary[s]} {s}" for s in readiness.READINESS_ORDER)
        + f"; {report['readiness']['members_with_risks']} with risks"
        + "; a maintenance exclusion holds back GKE's automatic upgrades only, a drain-blocking PDB, a fail-closed webhook with an unreachable backend in the upgrade's path or able to refuse the control plane's bootstrap RBAC writes (ClusterRoles and ClusterRoleBindings; Roles and RoleBindings in kube-system and kube-public), skew or an other blocker any upgrade; a risk does not block."
    )
    return "\n".join(lines)


def utc_now() -> datetime:
    """The clock every timestamp in the state file comes from; tests patch it."""
    return datetime.now(timezone.utc)


def format_timestamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(text) -> datetime | None:
    """A TIMESTAMP_FORMAT string back to an aware datetime; None when it does not parse."""
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_elapsed(seconds: int) -> str:
    """`2h 15m`, `3d 1h`, `45m`; under a minute is ELAPSED_UNDER_A_MINUTE."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, SECONDS_PER_DAY)
    hours, rest = divmod(rest, SECONDS_PER_HOUR)
    minutes = rest // SECONDS_PER_MINUTE
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return ELAPSED_UNDER_A_MINUTE


def member_key(member: dict) -> str:
    return MEMBER_KEY_SEPARATOR.join((member["project"], member["location"], member["cluster"]))


def state_path(state_dir: str, explicit_target: str | None) -> str:
    """The record for this target: `<state_dir>/<target>.json`, or the channel-default file."""
    return os.path.join(state_dir, (explicit_target or CHANNEL_DEFAULT_STATE_KEY) + STATE_FILE_SUFFIX)


def load_state(path: str) -> tuple[dict | None, str | None]:
    """(previous record, error). No file is (None, None); a bad file is (None, why)."""
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"previous record {path} unreadable, starting a new baseline: {e}"
    if not isinstance(data, dict) or data.get("format_version") != STATE_FORMAT_VERSION or not isinstance(data.get("members"), dict):
        return None, f"previous record {path} is not a format-{STATE_FORMAT_VERSION} record, starting a new baseline"
    return data, None


def write_text_atomically(path: str, text: str, tmp_prefix: str) -> None:
    """Writes `text` whole: a unique temp file in the same directory, then a rename, so a
    reader never meets a partial document and a write that fails leaves the previous file or
    none. Raises what the write raised (OSError) after removing the temp file."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=tmp_prefix, suffix=STATE_FILE_SUFFIX, dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json_atomically(path: str, data: dict, tmp_prefix: str) -> None:
    """`data` as JSON, written whole (`write_text_atomically`): serialised first, so a value JSON
    cannot carry raises before any file is touched and the previous file stays."""
    write_text_atomically(path, json.dumps(data, indent=JSON_INDENT), tmp_prefix)


def save_state(path: str, state: dict) -> None:
    """Writes the record atomically (unique temp file, then rename); raises OSError on failure."""
    write_json_atomically(path, state, STATE_TMP_PREFIX)


def _member_record(member: dict, unchanged_since: str, previous: dict | None) -> dict:
    """The member's entry in the record; an ungraded run keeps the last graded status."""
    lowest = member["lowest_node_pool"]
    status = member["status"]
    if status == STATUS_UNKNOWN and previous and previous.get("status") in GRADED_STATUSES:
        status = previous["status"]
    return {
        "control_plane_version": member["control_plane_version"],
        "lowest_node_pool_version": lowest["version"] if lowest else None,
        "status": status,
        "unchanged_since": unchanged_since,
    }


def _delta(member: dict, previous: dict | None) -> tuple[str, bool]:
    """(progress before the stall rule, whether this observation agrees with the previous)."""
    if previous is None:
        return PROGRESS_NEW, False
    lowest = member["lowest_node_pool"]
    versions_now = (member["control_plane_version"], lowest["version"] if lowest else None)
    versions_before = (previous.get("control_plane_version"), previous.get("lowest_node_pool_version"))
    status_now, status_before = member["status"], previous.get("status")
    moved = versions_now != versions_before
    # `completed` needs evidence of a move: a version change, or a graded behind status
    # before. A member that was already at the target and changed version is at the
    # target again (the channel default moved and it followed), which is a completed
    # upgrade, not a started one. An `unknown` baseline (a failed server-config read)
    # proves nothing on its own.
    if status_now in DONE_STATUSES and (moved or status_before in BEHIND_STATUSES):
        return PROGRESS_COMPLETED, False
    in_flight = [member["cluster_status"]] + [p.get("status", "") for p in member["node_pools"]]
    if moved or any(s in IN_FLIGHT_STATUSES for s in in_flight):
        return PROGRESS_STARTED, False
    # Same versions. A graded status that moved without them (the channel default
    # advanced under a member) starts a new observation series rather than inheriting
    # the old one's clock, so a stall is never dated from before the target it is
    # measured against existed. An `unknown` on either side is not a status change:
    # the versions agree, and that is all the observation says.
    both_graded = status_now in GRADED_STATUSES and status_before in GRADED_STATUSES
    return PROGRESS_UNCHANGED, status_now == status_before or not both_graded


def compute_progress(report: dict, previous: dict | None, now: datetime, rollout_flag: bool, path: str) -> dict:
    """Annotates every member with its progress since `previous`; returns the record to save.

    A member's `progress` is one of PROGRESS_ORDER; `unchanged_since` is the timestamp of
    the first of the consecutive agreeing observations, carried forward across runs, and
    `unchanged_for_seconds` the time since it. `report["rollout"]` gets the run-level view:
    whether the rollout counted as active and why, the per-progress counts, and the members
    of the previous record that have no row this run.
    """
    now_text = format_timestamp(now)
    prior_members = previous["members"] if previous else {}
    failed_projects = {e["project"] for e in report["errors"] if e.get("location") is None and e.get("project")}
    read_projects = set(report["projects"]) - failed_projects

    record: dict[str, dict] = {}
    agreeing: set[str] = set()
    for member in report["members"]:
        key = member_key(member)
        before = prior_members.get(key)
        progress, agrees = _delta(member, before if isinstance(before, dict) else None)
        since = now_text
        if agrees:
            since = before.get("unchanged_since") or (previous or {}).get("recorded_at") or now_text
            agreeing.add(key)
        member["progress"] = progress
        member["unchanged_since"] = since
        since_at = parse_timestamp(since)
        member["unchanged_for_seconds"] = int((now - since_at).total_seconds()) if since_at else None
        record[key] = _member_record(member, since, before if isinstance(before, dict) else None)

    movers = [m for m in report["members"] if m["progress"] in (PROGRESS_STARTED, PROGRESS_COMPLETED)]
    active_reason = None
    if rollout_flag:
        active_reason = ACTIVE_REASON_FLAG
    elif movers:
        active_reason = ACTIVE_REASON_MOVERS
    if active_reason:
        for member in report["members"]:
            if member["progress"] == PROGRESS_UNCHANGED and member["status"] in BEHIND_STATUSES and member_key(member) in agreeing:
                member["progress"] = PROGRESS_STALLED

    # Members of the previous record with no row this run: gone when their project was
    # read cleanly (a deleted cluster leaves the record); carried forward when it was not
    # read at all, so a failed or narrowed read never loses a record or dates a stall
    # from it.
    gone = []
    for key, before in prior_members.items():
        if key in record or not isinstance(before, dict):
            continue
        project = key.split(MEMBER_KEY_SEPARATOR, 1)[0]
        if project in read_projects:
            gone.append({"member": key, "reason": GONE_REASON_ABSENT, "carried_forward": False})
            continue
        why = NOT_READ_FAILED.format(project=project) if project in failed_projects else NOT_READ_OUT_OF_SCOPE.format(project=project)
        gone.append({"member": key, "reason": GONE_REASON_NOT_READ.format(why=why), "carried_forward": True})
        record[key] = before

    report["rollout"] = {
        "state_file": path,
        "recorded_at": now_text,
        "previous_run_at": previous.get("recorded_at") if previous else None,
        "active": active_reason is not None,
        "active_reason": active_reason,
        "summary": {p: sum(1 for m in report["members"] if m["progress"] == p) for p in PROGRESS_ORDER},
        "missing_members": gone,
    }
    return {
        "format_version": STATE_FORMAT_VERSION,
        "target": report["target_version"],
        "recorded_at": now_text,
        "members": dict(sorted(record.items())),
    }


def _versions_cell(control_plane, lowest_pool) -> str:
    return _cell(control_plane) + VERSION_PAIR_SEPARATOR + _cell(lowest_pool)


def render_progress(report: dict, previous: dict | None) -> str:
    """The "Rollout progress" section printed after the table; one line on a first run."""
    rollout = report["rollout"]
    target = f"target {report['target_version']}" if report["target_version"] else "each cluster's channel default"
    if previous is None:
        return f"Rollout progress: no previous run for {target}; baseline recorded at {rollout['state_file']}. Rerun after the next wave to see per-member deltas."
    lines = [
        f"Rollout progress against {target}, compared with the run at {rollout['previous_run_at']} (record: {rollout['state_file']}):",
        "",
        "| " + " | ".join(PROGRESS_COLUMNS) + " |",
        "| " + " | ".join(TABLE_SEPARATOR_CELL for _ in PROGRESS_COLUMNS) + " |",
    ]
    prior_members = previous["members"]
    for m in report["members"]:
        before = prior_members.get(member_key(m))
        before = before if isinstance(before, dict) else {}
        lowest = m["lowest_node_pool"]
        progress = m["progress"]
        if progress == PROGRESS_STALLED:
            progress = f"{PROGRESS_STALLED} (unchanged for {format_elapsed(m['unchanged_for_seconds'] or 0)})"
        row = (
            m["project"],
            m["cluster"],
            m["location"],
            _versions_cell(before.get("control_plane_version"), before.get("lowest_node_pool_version")) if before else EMPTY_CELL,
            _versions_cell(m["control_plane_version"], lowest["version"] if lowest else None),
            m["status"],
            progress,
        )
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    summary = rollout["summary"]
    active = f"rollout active ({rollout['active_reason']})" if rollout["active"] else "no member moved and --rollout-in-progress not passed, so nothing is flagged stalled"
    lines.append("")
    lines.append(f"{len(report['members'])} member(s): " + ", ".join(f"{summary[p]} {p}" for p in PROGRESS_ORDER) + f"; {active}.")
    for gone in rollout["missing_members"]:
        lines.append(f"- {gone['member']}: in the previous record, {gone['reason']}")
    return "\n".join(lines)



def main(argv: list[str] | None = None) -> int:
    # Exact flags only: the bench reads a worker's run of this script by the flag's full word,
    # so argparse's prefix matching (`--readi`) would run a report the reader does not credit.
    parser = argparse.ArgumentParser(description="Per-member GKE version table against a target version.", allow_abbrev=False)
    parser.add_argument("--project", action="append", help="GCP project to enumerate; repeatable. On an install with a declared scope, one the fleet_scope tool lists, passed with its collector_args; omit to sweep --scope-projects when given, else the fleet's configured projects")
    fleet_scope_args.add_scope_arguments(parser)
    parser.add_argument("--target-version", help="Target for every member, e.g. 1.31.4-gke.1183000. Default: each cluster's channel defaultVersion.")
    parser.add_argument("--cluster", action="append", help="Cluster to grade, as <location>/<name> (both halves) or a bare name; repeatable. With it, other clusters in the projects are skipped and the rollout record is neither read nor written (a narrowed read is not a rollout observation). Default: every cluster.")
    parser.add_argument("--output", help="Path to write the report as JSON.")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help=f"Directory holding one record per target from the previous run (default: {DEFAULT_STATE_DIR}).")
    parser.add_argument("--rollout-in-progress", action="store_true", help="Assert a rollout is under way, so an unchanged, behind member is flagged stalled even when no other member moved.")
    parser.add_argument("--readiness", action="store_true", help=f"Also grade each member's readiness for the upgrade: drain-blocking PDBs and fail-closed webhooks with an unreachable backend in the upgrade's path or able to refuse the control plane's bootstrap RBAC writes (ClusterRoles and ClusterRoleBindings; Roles and RoleBindings in kube-system and kube-public), maintenance exclusions and window, node-pool skew, then the per-entry rules of the upgrade-failure catalogue (readiness_rules/; {KUBECTL_READS_PER_MEMBER} kubectl reads per member in all, each after the webhook read failing on its own), whose blockers join the verdict and whose risks get a column of their own.")
    parser.add_argument("--at", help="RFC 3339 instant to evaluate maintenance exclusions and the window at (default: now). Only with --readiness.")
    parser.add_argument("--kubeconfig-dir", help="Directory for the per-member kubeconfig files --readiness writes (default: $HERMES_HOME/.kubeconfigs).")
    args = parser.parse_args(argv)
    declared_scope.set(args.scope_projects, args.scope_unread, args.scope_exclude_clusters)

    if args.target_version and parse_version(args.target_version) is None:
        sys.stderr.write(f"--target-version {args.target_version!r} is not MAJOR.MINOR.PATCH[-gke.BUILD]\n")
        return EXIT_USAGE

    readiness_options = None
    if args.readiness:
        at = utc_now()
        if args.at:
            at = readiness.parse_rfc3339(args.at)
            if at is None:
                sys.stderr.write(f"--at {args.at!r} is not an RFC 3339 timestamp\n")
                return EXIT_USAGE
        readiness_options = {"at": at, "kubeconfig_dir": args.kubeconfig_dir or default_kubeconfig_dir()}
    elif args.at or args.kubeconfig_dir:
        sys.stderr.write("--at and --kubeconfig-dir need --readiness\n")
        return EXIT_USAGE
    if args.cluster and args.rollout_in_progress:
        sys.stderr.write(ROLLOUT_FLAG_NEEDS_FULL_READ + "\n")
        return EXIT_USAGE
    for spec in args.cluster or []:
        if parse_cluster_spec(spec) is None:
            sys.stderr.write(INVALID_CLUSTER_SPEC.format(spec=spec) + "\n")
            return EXIT_USAGE

    listing_errors: list[str] = []
    projects = get_target_projects(args.project, listing_errors)
    scope_label = DECLARED_SCOPE_ERROR_SCOPE if declared_scope.declared else PROJECTS_LIST_ERROR_SCOPE
    if not projects:
        for error in listing_errors:
            sys.stderr.write(f"{scope_label}: {error}\n")
        if not declared_scope.declared:
            sys.stderr.write("no project: pass --project, or set MONITORED_PROJECT_IDS or GCP_PROJECT_ID\n")
        return EXIT_USAGE

    report = build_report(projects, args.target_version, readiness_options, clusters=args.cluster)
    report["errors"][:0] = [
        {"project": scope_label, "location": None, "message": error} for error in listing_errors
    ]
    print(render_table(report))
    if readiness_options is not None:
        print()
        print(render_readiness(report))
    write_failed = False
    if args.cluster:
        # A narrowed read is not a rollout observation: the record would file
        # every member it did not name as gone. Leave the record alone.
        print(f"\n{NARROWED_RUN_NOTE}")
    else:
        path = state_path(args.state_dir, args.target_version)
        previous, state_error = load_state(path)
        state = compute_progress(report, previous, utc_now(), args.rollout_in_progress, path)
        print()
        print(render_progress(report, previous))
        if state_error:
            sys.stderr.write(state_error + "\n")
        try:
            save_state(path, state)
        except OSError as e:
            sys.stderr.write(f"failed to write the record {path}: {e}\n")
            write_failed = True

    if args.output:
        # Serialised whole before the file is opened, so a value JSON cannot carry leaves no
        # half-written file behind for the readers of `--output`; a NaN or infinite float,
        # which `json.dumps` would otherwise write as a bare token no JSON parser reads, is
        # refused the same way.
        try:
            rendered = json.dumps(report, indent=JSON_INDENT, allow_nan=False)
        except (TypeError, ValueError) as e:
            sys.stderr.write(f"failed to serialise the report for {args.output}: {e}\n")
            rendered = None
            write_failed = True
        if rendered is not None:
            try:
                write_text_atomically(args.output, rendered, OUTPUT_TMP_PREFIX)
                print(f"\nWrote {len(report['members'])} member(s) to {args.output}")
            except OSError as e:
                sys.stderr.write(f"failed to write {args.output}: {e}\n")
                write_failed = True

    return EXIT_PARTIAL if report["errors"] or (report.get("readiness") or {}).get("audit_log_failures") or write_failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
