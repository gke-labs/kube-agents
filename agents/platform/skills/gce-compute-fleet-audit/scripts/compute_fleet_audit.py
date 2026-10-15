#!/usr/bin/env python3
"""compute_fleet_audit.py — Procedural collector for the GCE Compute Engine
and MIG fleet audit.

See docs/designs/fleet-audit-collector-manifest.md and
governance/gce_compute_fleet_sop.md.

Ships alongside its SOP as this stream's own collector rather than folded
into `fleet-audit/scripts/collect.py`: that driver enumerates GKE clusters and
fetches per-cluster kubeconfigs, neither of which this stream needs — its
targets are GCP projects, read directly with `gcloud compute`. It emits the
run manifest that design's §2 specifies, the same one `collect.py` emits, so
`audit_report.py finish --manifest-file` cross-checks the model's
`checks_run` against what actually ran. It used to print a whole findings
document instead, which no reader joins on: `adopt_collector_evidence` never
saw it and the model's retyped excerpts shipped in place of the observed ones.

All four of the SOP's checks are implemented here:

- `gce-startup-script-status` enumerates with `compute instances list`, keeps
  the RUNNING instances that actually run a startup script — its own metadata
  or the project's common metadata, read with `compute project-info describe` —
  and measures each with `compute instances get-serial-port-output`, matching
  §2.1's markers (`STARTUP_FAILURE_PATTERN`) in the console text.
- `mig-convergence-stalled` reads `compute instance-groups managed list` and
  holds §2.2's resize-loop condition against each group's `currentActions`
  counters, skipping a group with an update in progress.
- `sole-tenant-headroom` reads `compute sole-tenancy node-groups list`, then
  `list-nodes` per group, and ratios consumed against total vCPU and memory.
- `orphaned-snapshots` cross-references `compute snapshots list` against
  `compute disks list` — a snapshot whose `sourceDisk` names neither a live
  disk's name nor its `selfLink`, was not taken by a snapshot schedule, and is
  older than ninety days.

The roster was five checks and three of them were declared on every target
with a reason saying no code performed them, which claimed coverage the run
did not have. §2.2 and §2.4 are now implemented. A check whose own read fails
on a target is listed in that target's `checks_unevaluated` with a
`limitations` sentence, as `collect.py` does, so the run reads as partial and
keeps that check's findings open; `checks_not_applicable` is reserved for a
check the read positively established has nothing to run against.

`ops-agent-guest-health` left the roster with them rather than being
implemented. §2.3's condition is whether the guest's Ops Agent is reporting,
which lives in Cloud Monitoring or in OS Config inventory, and neither is
reachable: `gcloud monitoring` exposes no metric read the credential proxy's
allowlist could carry, and `osconfig.googleapis.com` is not enabled on the
reference install. Keeping it as a permanent `checks_unevaluated` entry would have kept the
whole stream's ledger partial forever over one unimplementable check, so the
stream now audits the four things it can actually establish. §2.3 of the SOP
records the surface it would need.

Field contracts assumed of `gcloud ... --format=json` output:

- `compute instances list` items carry `name`, `status`, a `zone` selfLink
  whose last segment is the zone, and `metadata.items[].key`. Only `RUNNING`
  instances have serial console output to read.
- `compute project-info describe` carries the project's common metadata under
  `commonInstanceMetadata.items[].key`, the same shape one item deeper.
- `compute instances get-serial-port-output` returns **text, not JSON**, so it
  runs outside `run_and_gate`. A failure on one instance does not fail the
  project closed — a single stopped-mid-run or IAM-refused VM would otherwise
  cost the project its other check — but it does subtract from coverage: the
  instances that could not be read are named in the target's `limitations`,
  and a project where *every* RUNNING instance refused the read lists the slug
  in `checks_unevaluated` rather than reporting a clean fleet.
- `compute disks list` items carry `name` and `selfLink`; a snapshot's
  `sourceDisk` appears in the wild in both forms, so both are indexed.
- `compute snapshots list` items carry `sourceDisk`, `creationTimestamp`
  (RFC-3339, `Z`-suffixed), and `sourceSnapshotSchedulePolicy` /
  `sourceSnapshotSchedulePolicyId` where a snapshot schedule took them.
- `compute instance-groups managed list` items carry `name`, `size`,
  `targetSize`, a `status` object (with `versionTarget.isReached`), a
  `currentActions` object of thirteen
  integer counters, and a `zone` *or* `region` selfLink — regional MIGs carry
  the latter, so `_scope_of` reads whichever is present. Every counter is read
  through `_count`, which defaults a missing or non-integer one to zero: a
  changed contract must read as "no churn observed" rather than cost the
  project its whole MIG check.
- `compute sole-tenancy node-groups list-nodes` items carry `totalResources`
  and `consumedResources`, each an `InstanceConsumptionInfo`
  (`{guestCpus, memoryMb, localSsdGb, minNodeCpus}`). This is the one shape here
  taken from the Compute v1 discovery document rather than from live output —
  the reference install reserves no sole-tenant node groups, so the read returns
  `[]` and the check declares a structural non-applicability instead.
  `check_sole_tenant_headroom` is therefore written to skip any node missing
  either object and to report `measured=False` when none of them yields figures,
  which lists the slug in `checks_unevaluated`.

What this collector deliberately does not do, because it would change what
gets flagged: it applies neither §2.1's GKE-node exclusion, nor §2.2's
pod-driven-scale exclusion, nor §2.5's legal-hold exclusion, nor §2.4's
maintenance-window one. Each is the model's call on the candidate, which is why
those candidates carry a `needs_triage` slug naming the judgment rather than a
`null`. The one exclusion applied here is §2.4's autoscaling limb, because a
node group's `autoscalingPolicy.mode` is a field on the group rather than a
judgment about it.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, NamedTuple

AUDIT_ID = "gce-compute-fleet-audit"

MANIFEST_VERSION = 1

# A digest of this file, published as `checks_revision`. The manifest contract
# (docs/designs/fleet-audit-collector-manifest.md §2) carries it unread today,
# reserved for the run-over-run comparison that tells a finding that stopped
# reproducing from a check that stopped looking.
REVISION_DIGEST_CHARS = 12
CHECKS_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[
    :REVISION_DIGEST_CHARS
]

DEFAULT_TIMEOUT_S = 60
# The return code `timeout(1)` uses, recorded for a read that ran out of time.
TIMEOUT_RC = 124
# How many projects this audit reads at once. Each is a run of gcloud compute
# reads through the credential proxy, which admits four requests at once under
# its child memory budget at the operator's default 1Gi limit
# (docs/designs/credential-proxy-child-memory-budget.md §2.2). A wider pool only
# queues the rest at the proxy, where a read still waiting at its 60 s admission
# bound is refused busy, or hits DEFAULT_TIMEOUT_S here first and reads its
# project as gate-failed -- the SOP's cue to re-read it by hand through the same
# proxy. So the pool is the admitted count, as the fleet-audit collectors and
# stall_watch.py's and cluster_agent_reconcile.py's listing pools are.
MAX_WORKERS = 4
# `audit_report.validate_check_command`'s ceiling, restated rather than
# imported because this script ships standalone. An over-length `command` is
# not a clipped field: `finish` refuses the whole document, so a project with
# enough RUNNING instances to overflow the joined serial-read provenance below
# would publish nothing at all.
MAX_COMMAND_CHARS = 2000
# `audit_report.MAX_EXCERPT_CHARS`, restated for the same reason.
MAX_EXCERPT_CHARS = 2000
# `cross_check_manifest` quotes a target's `error` back clipped to 150 and 200
# characters, so the sentence has to name the check, the command and the rc
# inside this budget.
ERROR_CLIP_CHARS = 300
# How much of a joined command the clip keeps for the counted tail.
JOIN_TAIL_BUDGET_CHARS = 64

GLOBAL_LOCATION = "global"
UNRESOLVED_PROJECT = "unknown"
RUNNING_STATUS = "RUNNING"
OUTCOME_COLLECTED = "collected"
OUTCOME_GATE_FAILED = "gate-failed"

# Where the project scope comes from (`get_target_projects`).
MONITORED_PROJECTS_ENV = "MONITORED_PROJECT_IDS"
PROJECT_ENV_VARS = ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID")
GCLOUD = "gcloud"
PROJECT_ID_FORMAT = "--format=value(projectId)"
PROJECTS_LIST_CMD = (GCLOUD, "projects", "list", PROJECT_ID_FORMAT)
CONFIG_PROJECT_CMD = (GCLOUD, "config", "get-value", "project")
PROJECT_DESCRIBE_CMD = (GCLOUD, "projects", "describe")
PROJECT_NUMBER_FORMAT = "--format=value(projectNumber)"
PROJECT_TARGET_PREFIX = "project/"
# The manifest target standing for every project a run did not enumerate: the
# listing failed, came back filtered, or the operator narrowed the scope on
# purpose. Uppercase because a GCP project id cannot be, so no real project can
# collide with it; fleet_drift.py names the same target, so every stream
# reports the loss alike.
UNENUMERATED_PROJECTS_TARGET = PROJECT_TARGET_PREFIX + "UNENUMERATED_PROJECTS"
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

# The scope this collector was handed, set by main from the two flags.
declared_scope = fleet_scope_args.DeclaredScope()

# A run narrowed on purpose -- `--project-id` or `MONITORED_PROJECT_IDS` --
# skips discovery, so it reads the named projects and no other. Without a row
# saying so the manifest reads as the whole fleet, and `finish` resolves every
# ledger finding on a project the run never looked at.
SCOPED_RUN_NOTE = (
    "scope narrowed to {projects} by {source}: discovery was skipped, so no other project "
    "in this fleet was named or read"
)
UNENUMERATED_TAIL = "How many other projects the fleet holds is unknown."

# gcloud's words for a project whose Compute Engine API is off. Such a project
# holds no instance, group, disk or snapshot, so it contributes no target
# rather than a `gate-failed` one that would hold every finding open for as
# long as the project exists. fleet_drift.py matches the same three forms.
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

STARTUP_SLUG = "gce-startup-script-status"
MIG_SLUG = "mig-convergence-stalled"
SOLE_TENANT_SLUG = "sole-tenant-headroom"
SNAPSHOT_SLUG = "orphaned-snapshots"

# §2.1's fatal markers, as `google_metadata_script_runner` prints them. The
# guest agent current images ship logs `Script "startup-script" failed with
# error: exit status 1` (seen on a Debian 12 VM, 2026-10-06); older agents
# printed `startup-script exit status 1` and a closing `Finished running
# startup scripts with error`. Any non-zero status is a failure, not only 1.
STARTUP_FAILURE_PATTERN = re.compile(
    r'Script "(?:windows-)?startup-script(?:-[a-z0-9]+)?" failed with error'
    r"|startup-script(?:-url)? exit status [1-9]\d*"
    r"|Finished running startup scripts with error"
)
# What `google_metadata_script_runner` prints as each startup-script run
# begins, on old and current guest agents alike.
STARTUP_RUN_MARKER = "Starting startup scripts"

# §2.5's threshold. Measured against the snapshot's own `creationTimestamp`,
# which is what this collector can see; §2.5 words the condition as the source
# disk having been deleted more than ninety days ago, and no read here recovers
# a deleted disk's deletion time. The two coincide whenever the disk outlived
# the snapshot's first ninety days, and the snapshot age is the conservative
# side of the difference — it flags later, never earlier.
ORPHAN_AGE_DAYS = 90
# The fields a scheduled snapshot carries naming the snapshot schedule that
# took it (Compute v1 `Snapshot`).
SNAPSHOT_SCHEDULE_KEYS = ("sourceSnapshotSchedulePolicy", "sourceSnapshotSchedulePolicyId")

# §2.4's threshold, as a percentage of the node group's aggregate capacity.
SOLE_TENANT_UTILISATION_PCT = 90

# The two `NodeGroup.autoscalingPolicy.mode` values that actually add nodes, out
# of the four the Compute v1 discovery document defines. The other two --
# MODE_UNSPECIFIED and OFF -- leave the group a fixed reservation, which is
# exactly what §2.4 measures.
AUTOSCALING_MODES = ("ON", "ONLY_SCALE_OUT")

SEVERITY = {
    STARTUP_SLUG: "critical",
    MIG_SLUG: "major",
    SOLE_TENANT_SLUG: "minor",
    SNAPSHOT_SLUG: "minor",
}

# Structural non-applicability: the enumeration ran, came back empty, and the
# check has no object to hold its condition against. A `checks_not_applicable`
# entry, not a `checks_unevaluated` one: the latter is for a check that applies
# and whose read failed, and it makes the run partial. A project that simply
# runs no MIGs is not a project this run cannot vouch for, and marking it so
# would pin the whole stream's ledger partial over an absence the read
# positively established.
#
# GKE Autopilot node VMs are the reason an empty enumeration has to be declared
# rather than passed over. The Compute API does not show them to the audit
# identity — by design, not a grant this install is missing: as the platform
# GSA a `gk3-*` instance, its MIG and its boot disk all return 404 rather than
# 403, while the same read as project Owner returns them, and no deny policy,
# IAM condition or org policy is involved. See
# https://docs.cloud.google.com/kubernetes-engine/docs/concepts/autopilot-architecture
# ("Visibility"). Google manages those nodes, so §2.1's startup scripts and
# §2.2's convergence are not the operator's to set or to fix and a finding on
# one would carry a recommendation nobody can act on — a real narrowing of the
# universe, not a missed read. What makes it dangerous is that a project whose
# only nodes are Autopilot enumerates *empty*, which is indistinguishable from
# an idle project unless the reason says so.
AUTOPILOT_INVISIBLE_NOTE = (
    "GKE Autopilot node VMs do not appear in the Compute Engine API to this "
    "identity by design rather than for want of a grant, so a project whose "
    "nodes are all Autopilot enumerates empty here; Google manages those nodes "
    "and the condition is not the operator's to act on."
)
NO_MIGS_REASON = (
    "This project runs no Managed Instance Groups visible to the audit "
    "identity: `compute instance-groups managed list` returned an empty array, "
    "so §2.2's convergence condition has nothing to hold against. Structural, "
    "not a missed read. " + AUTOPILOT_INVISIBLE_NOTE
)
NO_SNAPSHOTS_REASON = (
    "This project holds no Persistent Disk snapshots: `compute snapshots list` "
    "returned an empty array, so §2.5 has nothing to attribute to a deleted "
    "source disk. Structural, not a missed read."
)
# §2.1's counterpart to the two below. Without it an Autopilot-only project ran
# the enumeration, found nothing to read a console from, and recorded the slug
# as run and clean across a fleet it never saw — the false all-clear this whole
# stream is being converted to remove, arriving one layer up from the checks.
NO_INSTANCES_REASON = (
    "This project runs no Compute Engine instances visible to the audit "
    "identity: `compute instances list` returned an empty array, so §2.1's "
    "serial console markers have no instance to match against. Structural, not "
    "a missed read. " + AUTOPILOT_INVISIBLE_NOTE
)
# Instances exist and were enumerated, but none is RUNNING. A stopped instance
# serves no serial console output, so there is nothing §2.1 could have read —
# structural in the same way, and still not a `checks_unevaluated` case, because the
# state that rules the check out is one the read positively established.
NO_RUNNING_INSTANCES_REASON = (
    "No Compute Engine instance on this project is RUNNING: `compute instances "
    "list` returned {total} instance(s) visible to the audit identity and none "
    "in RUNNING state, and an instance that is not running serves no serial "
    "console output for §2.1 to read. Structural, not a missed read. "
    + AUTOPILOT_INVISIBLE_NOTE
)
# The metadata keys `google_metadata_script_runner` reads. It runs a startup
# script only when one of these is set on the instance or in the project's
# common metadata, and only then does it log any of the failure lines §2.1
# matches on. Neither set anywhere means none of them can appear.
# The Linux keys and the Windows ones (`windows-startup-script-ps1`, `-cmd`,
# `-bat`, `-url`); the guest agent runs whichever the instance sets.
STARTUP_SCRIPT_KEYS = (
    "startup-script",
    "startup-script-url",
    "windows-startup-script-ps1",
    "windows-startup-script-cmd",
    "windows-startup-script-bat",
    "windows-startup-script-url",
)
# The third structural case for §2.1, and the one that hides best: the
# instances are there, they are RUNNING, their consoles read fine — and not one
# of them runs a startup script, so the marker the check greps for cannot occur
# in any of them. A fleet of GKE Standard nodes is exactly this shape: every
# console reads, none carries a startup-script line of any kind, and no
# instance sets either metadata key. Without this branch that reads as a clean
# pass over the whole fleet.
#
# The reason says "visible to the audit identity" for the argument
# `AUTOPILOT_INVISIBLE_NOTE` makes on the two declarations above. Autopilot
# nodes, which the identity cannot see, *do* set `startup-script` -- to `echo
# startup-script-override`, a stub Google installs to neutralise the hook -- so
# {total} is the visible population rather than the project's, and "a GKE node
# pool sets no startup script" holds for Standard nodes only. Neither changes
# what the check should do, because a stub on a node Google manages is not the
# operator's to act on; both matter to a reader deciding from the ledger
# whether to trust a `not applicable` on this slug.
NO_STARTUP_SCRIPT_REASON = (
    "No Compute Engine instance on this project runs a startup script: none of "
    "the {total} RUNNING instance(s) visible to the audit identity carries a "
    "startup-script key (Linux or Windows) in its metadata and the project's "
    "common metadata sets none, so `google_metadata_script_runner` never "
    "runs and the exit status §2.1 matches on cannot appear in any console. "
    "Structural, not a missed read. A GKE Standard node pool is the ordinary "
    "way to reach this branch: its nodes bootstrap from `user-data` and "
    "`kube-env` and set neither startup-script key, so a project whose only "
    "visible instances are Standard nodes runs none. Autopilot nodes do set "
    "`startup-script`, to a Google-installed stub that neutralises the hook, "
    "but they never appear in {total}. " + AUTOPILOT_INVISIBLE_NOTE
)
NO_NODE_GROUPS_REASON = (
    "This project reserves no sole-tenant node groups: `compute sole-tenancy "
    "node-groups list` returned an empty array, so §2.4's headroom condition "
    "has no reservation to measure. Structural, not a missed read."
)
# The `checks_unevaluated` case for §2.4: the groups exist and were enumerated,
# and for every one of them the per-node read failed or no node carried the
# resource figures the condition needs. Nobody looked, so a stale headroom
# finding must not be called fixed on the strength of this run.
UNMEASURED_NODE_GROUPS_REASON = (
    "§2.4's headroom condition needs each node's `totalResources` and "
    "`consumedResources`, and `compute sole-tenancy node-groups list-nodes` "
    "failed or returned neither for every one of the {groups} node group(s) on "
    "this project. They were enumerated, not measured."
)
UNMEASURED_NODE_GROUPS_LIMITATION = (
    "sole-tenant-headroom could not be evaluated on this project: "
    + UNMEASURED_NODE_GROUPS_REASON
)
# Some node groups were measured and some were not: their `list-nodes` failed
# or carried no figures. The check keeps its verdict for the groups it measured;
# this names the ones it passed over rather than cleared, as
# PARTIAL_SERIAL_LIMITATION does for §2.1.
PARTIAL_NODE_GROUPS_LIMITATION = (
    "sole-tenant-headroom measured {measured} of {total} node group(s) on this "
    "project. `compute sole-tenancy node-groups list-nodes` failed or returned "
    "no resource figures for the rest, which the check passed over rather than "
    "cleared: {names}."
)
UNREAD_SERIAL_REASON = (
    "Every RUNNING instance whose console §2.1 reads on this project refused "
    "`compute instances get-serial-port-output`, so no serial console text was "
    "read and §2.1's markers were matched against nothing."
)

# One or more RUNNING instances refused `get-serial-port-output` while others
# answered. The check keeps its verdict for the instances it read; this names
# the ones it passed over rather than cleared, which is what turns the
# shortfall into a coverage gap instead of a silent clean pass.
PARTIAL_SERIAL_LIMITATION = (
    "gce-startup-script-status read serial console output from {read} of the "
    "{total} RUNNING instance(s) on this project that run a startup script. "
    "`compute instances get-serial-port-output` failed for the rest, which "
    "the check passed over rather than cleared: {names}."
)
# Every RUNNING instance refused the read, so the check reached no verdict at
# all on this project and the slug is listed in `checks_unevaluated` alongside
# this.
UNREAD_SERIAL_LIMITATION = (
    "gce-startup-script-status could not be evaluated on this project: "
    "`compute instances get-serial-port-output` failed for all {total} "
    "RUNNING instance(s) that run a startup script, so no serial console output was read. They were "
    "enumerated, not examined."
)

# §2.1's Do-NOT-flag limb is a GKE-node exclusion this collector does not
# apply, and §2.5's is a legal-hold exclusion it cannot see. `needs_triage` is
# how a candidate says which judgment it is handing back — it is not read by
# `audit_report.py`, it is an instruction to the model.
TRIAGE_GKE_NODE = "gke-managed-node"
TRIAGE_GKE_MIG = "gke-managed-mig"
TRIAGE_MAINTENANCE = "maintenance-window"
TRIAGE_RETENTION_HOLD = "retention-hold"

# Both GKE node-pool MIG spellings: `gke-` for Standard, `gk3-` for Autopilot.
GKE_MIG_PREFIXES = ("gke-", "gk3-")

REDACTED = "[REDACTED]"
# Serial console output is untrusted text from inside the guest, and §2.1's
# excerpt is a line lifted straight out of it. The SOP's red line — "credentials
# in serial port output must never reach an excerpt" — used to be the model's to
# honour, because the model retyped the excerpt; `adopt_collector_evidence`
# overwrites the model's text with this one, so it is now this file's. Shapes
# that a startup script is known to echo, matched before the line is published.
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN[^-]{0,64}PRIVATE KEY-----"),
    # `ya29.c.` is the service-account form a metadata-server token takes.
    re.compile(r"ya29\.(?:c\.)?[A-Za-z0-9_\-]{10,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),
    # The lead admits a prefixed name (`DB_PASSWORD=`, `GITHUB_TOKEN=`), which
    # the bare word misses because `_` is a word character; audit_report's
    # `_SECRET_KEY_RE` carries the same lead for the same reason.
    re.compile(r"(?i)\b(?:[A-Za-z0-9]+[_.\-])*(?:bearer|token|password|passwd|secret|api[_-]?key)\b[\"'\s:=]+\S+"),
    # A long unbroken run of encoded material — a key or a JWT segment. The
    # uppercase-or-`+` lookahead is what keeps this from swallowing the
    # excerpt's diagnostic content: the previous form,
    # `[A-Za-z0-9+/_\-]{40,}`, matched any 40-character run of lowercase,
    # digits and separators, which is precisely the shape of a GKE node name
    # (`gke-prod-cluster-default-pool-9f8a7b6c-abcd`, 43 chars) and of a deep
    # filesystem path. Both were being redacted out of real serial lines — and
    # since `adopt_collector_evidence` overwrites the model's excerpt with this
    # string, the degraded line is what shipped. The node name is the very
    # string this collector's `needs_triage: gke-managed-node` handoff asks the
    # model to judge, and the failing script's path is the only part of a
    # startup-script excerpt that says *what* failed.
    re.compile(r"\b(?=[A-Za-z0-9+/_\-]*[A-Z+])[A-Za-z0-9+/_\-]{40,}={0,2}\b"),
    # Lowercase hex digests, which the lookahead above deliberately excludes.
    # Precise enough not to reach a hostname: `[a-f0-9]` admits none of the
    # letters a DNS label needs, and a hyphen ends the run.
    re.compile(r"\b[a-f0-9]{32,}\b"),
)


def log(msg: str) -> None:
    """Every log line goes to stderr: the SOP redirects stdout to the manifest
    file, so one stray `print` corrupts the JSON."""
    print(f"[compute_fleet_audit] {msg}", file=sys.stderr, flush=True)


class Run(NamedTuple):
    """One subprocess's outcome, in the shape the manifest records it — the
    same shape `collect.py`'s and `networking_audit.py`'s `Run` use, kept as a
    separate definition because this script ships standalone."""

    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]


def _text(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")


def default_run(argv: list[str], *, timeout: int = DEFAULT_TIMEOUT_S) -> Run:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        # On Linux the partial buffers come back as bytes whatever `text=` says,
        # as the fleet-audit collectors' `_text()` helpers note.
        return Run(argv, TIMEOUT_RC, _text(exc.stdout), _text(exc.stderr), time.monotonic() - t0)
    except Exception as exc:  # gcloud missing, permission denied on exec, etc.
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def run_and_gate(argv: list[str], *, run: RunFn = default_run) -> tuple[object | None, Run]:
    """One `gcloud` call behind a fail-closed gate — non-zero exit, empty
    output, or non-JSON output all gate closed (a truncated result must
    never read as "nothing here")."""
    result = run(argv)
    if result.rc != 0 or not result.stdout.strip():
        return None, result
    try:
        return json.loads(result.stdout), result
    except json.JSONDecodeError:
        return None, result


class ComputeApiDisabled(Exception):
    """Raised when a project's own Compute Engine API is off: it holds nothing
    this audit reads, so it contributes no manifest target at all."""


class GateFailure(Exception):
    """Raised when one of a project target's several independent `gcloud`
    reads fails its gate. Fails that whole target closed — one `outcome` per
    manifest entry, not one per check. A shorter `candidates` list from the
    checks that happened to run first is indistinguishable from a clean
    project."""


class SlugReads:
    """The reads that backed one check: their commands, their summed time, and
    a running digest of their output. Each body is hashed as it arrives and not
    kept: a console read can be most of a megabyte, and holding every one a
    project returns until the project is done grows with its instance count,
    times the projects collected at once. Hashing in order equals hashing the
    concatenation, so `output_sha256` is what it was."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.duration_s = 0.0
        self._digest = hashlib.sha256()

    def add(self, command: str, result: Run) -> None:
        self.commands.append(command)
        self.duration_s += result.duration_s
        self._digest.update((result.stdout or "").encode("utf-8"))

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _joined_record(reads: SlugReads) -> dict:
    """One `commands` entry covering every read that backed one check.

    Most of the four checks take more than one read — an enumeration plus a
    serial read per instance, a node-group list plus `list-nodes` per group, an
    enumeration of disks plus one of snapshots — and publishing only the last
    names a command that cannot reproduce the verdict, which is the one thing
    this field exists to allow.

    Joined with ` && ` so the field stays a line a reader can paste. `rc` is 0
    because only reads that succeeded are appended: a gated failure raises
    `GateFailure` before reaching this, and a serial, `project-info` or
    `list-nodes` read that failed is never recorded.
    """
    parts = list(reads.commands)
    joined = " && ".join(parts)
    if len(joined) > MAX_COMMAND_CHARS:
        # Clipped at a join boundary, and the tail is counted rather than
        # dropped: a `commands` entry silently listing three of a project's
        # ninety serial reads claims narrower coverage than the run had.
        kept = [parts[0]]
        for part in parts[1:]:
            if len(" && ".join(kept + [part])) > MAX_COMMAND_CHARS - JOIN_TAIL_BUDGET_CHARS:
                break
            kept.append(part)
        joined = (
            " && ".join(kept)
            + f"  # and {len(parts) - len(kept)} more read(s) of the same shape"
        )
    return {
        "command": joined[:MAX_COMMAND_CHARS],
        "rc": 0,
        "duration_s": round(reads.duration_s, 2),
        "output_sha256": reads.hexdigest(),
    }


def _last_segment(url: str) -> str:
    return (url or "").rstrip("/").split("/")[-1]


def redact(text: str) -> str:
    """Blank out anything in a serial console line that looks like a secret.

    Conservative in the direction that matters: a redaction that swallows a
    harmless token costs a reader some context, while a miss publishes a
    credential into a GitHub issue.
    """
    out = text
    for pattern in SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out[:MAX_EXCERPT_CHARS]


def _normalise_project_id(
    project: str, notes: list[str] | None = None, *, run: RunFn = default_run
) -> str | None:
    """A numeric project number (e.g. from spec.harness.projectId) resolved to its
    projectId. None, with why in `notes`, when it cannot be resolved."""
    if not project.isdigit():
        return project
    result = run([*PROJECT_DESCRIBE_CMD, project, PROJECT_ID_FORMAT])
    if result.rc == 0 and result.stdout.strip():
        return result.stdout.strip()
    if notes is not None:
        notes.append(
            f"`gcloud projects describe {project}` rc={result.rc}: "
            f"{result.stderr.strip()[:ERROR_CLIP_CHARS] or 'no stderr'}; "
            "numeric project could not be resolved to a projectId"
        )
    return None


def get_target_projects(
    cli_project: str | None = None,
    notes: list[str] | None = None,
    *,
    run: RunFn = default_run,
) -> list[str]:
    """Resolves all target GCP projects to audit.

    Everything that narrows or loses part of the fleet is appended to `notes`
    when the caller passes one, and `collect_fleet` turns them into one
    `project/UNENUMERATED_PROJECTS` target so the run reads as partial rather
    than as the whole fleet:

    - `--project-id` narrows the scope on purpose and skips discovery;
    - `--scope-projects`, passed by the SOP from the fleet_scope tool, is the
      install's declared scope: swept as given, nothing listed, and a declared
      project the install could not read (`--scope-unread`) is the one note;
    - without either, a non-empty `MONITORED_PROJECT_IDS` narrows the scope on
      purpose and skips discovery;
    - a failed `gcloud projects list` leaves only the env/config project;
    - a listing that succeeds without naming the configured project is
      filtered rather than complete, as fleet_drift.py treats it.

    When discovery runs, the host project from `gcloud config get-value project`
    is always part of the scope, alongside any `GCP_PROJECT_ID`-style variable.
    """
    if cli_project and cli_project.strip():
        raw = cli_project.strip()
        # Refused before anything is read: a scoped sandbox without the flags
        # must not describe a project the worker named, since the refusal that
        # follows says the collector may not touch it.
        if declared_scope.args_missing:
            if notes is not None:
                notes.append(declared_scope.empty_error())
            return []
        resolved = _normalise_project_id(raw, notes, run=run)
        project = resolved or raw
        # On the resolved id: a project number given here names the same
        # project the tool lists by id. A number the describe could not resolve
        # reaches the holder as digits, which it refuses under a declared scope
        # with the remedy, and lets through without one, as it always did, with
        # the describe failure already recorded.
        override_error = declared_scope.override_error(project)
        if override_error:
            if notes is not None:
                notes.append(override_error)
            return []
        if notes is not None:
            notes.append(SCOPED_RUN_NOTE.format(projects=project, source="`--project-id`"))
        return [project]
    if declared_scope.declared:
        # The declared scope the agent carried from the fleet_scope tool: the
        # sweep as given, nothing listed, the unread rows as the one note. A
        # declared scope with nothing readable sweeps nothing and says so,
        # rather than widening to the listing.
        if not declared_scope.projects:
            if notes is not None:
                notes.append(declared_scope.empty_error())
            return []
        if notes is not None and declared_scope.note():
            notes.append(declared_scope.note())
        return sorted(declared_scope.projects)

    env_projects = {os.environ.get(var, "").strip() for var in PROJECT_ENV_VARS} - {""}
    # Parsed before it is tested, so a blank or separator-only value reads as
    # unset rather than as an override that names nothing and skips discovery.
    monitored = set(os.environ.get(MONITORED_PROJECTS_ENV, "").replace(",", " ").split())
    if monitored:
        projects = {_normalise_project_id(p, run=run) or p for p in monitored | env_projects}
        if notes is not None:
            notes.append(
                SCOPED_RUN_NOTE.format(
                    projects=", ".join(sorted(projects)), source=f"`{MONITORED_PROJECTS_ENV}`"
                )
            )
        return sorted(projects)

    raw_projects = set(env_projects)
    result = run(list(CONFIG_PROJECT_CMD))
    if result.rc == 0 and result.stdout.strip():
        raw_projects.add(result.stdout.strip())
    projects = {
        resolved
        for p in sorted(raw_projects)
        if (resolved := _normalise_project_id(p, notes, run=run)) is not None
    }

    listing = run(list(PROJECTS_LIST_CMD))
    if listing.rc != 0 and notes is not None:
        notes.append(
            f"`gcloud projects list` rc={listing.rc}: "
            f"{listing.stderr.strip()[:ERROR_CLIP_CHARS] or 'no stderr'}; "
            "the scope fell back to the configured project"
        )
    if listing.rc == 0:
        listed = {line.strip() for line in listing.stdout.splitlines() if line.strip()}
        omitted = sorted(projects - listed)
        if omitted and notes is not None:
            notes.append(
                f"`gcloud projects list` rc=0 did not name {', '.join(omitted)}, so the listing is filtered"
            )
        projects |= listed

    return sorted(projects or raw_projects)


def refusal_names_project(project: str, stderr: str, *, run: RunFn = default_run) -> tuple[bool, str]:
    """Whether an API-disabled refusal is `project`'s own, with why when it is not.

    Only the project's own refusal means it holds nothing to audit. One naming
    another project -- the credential's quota project -- says nothing about this
    one, and read as empty it would drop the project from the manifest
    altogether. A refusal naming no project, or one whose number cannot be
    compared with this project's, is a failed read.
    """
    numbers = set(REFUSED_PROJECT_NUMBER_RE.findall(stderr))
    if not numbers:
        if re.search(rf"\b(?i:projects?)[ /]['\"\[]?{re.escape(project)}(?![\w-])", stderr):
            return True, ""
        return False, f"the refusal does not name {project!r}"
    result = run([*PROJECT_DESCRIBE_CMD, project, PROJECT_NUMBER_FORMAT])
    if result.rc != 0:
        return False, (
            f"`gcloud projects describe {project}` failed (rc={result.rc}), so the refusal's "
            f"project number could not be compared: "
            f"{result.stderr.strip()[:ERROR_CLIP_CHARS] or 'no stderr'}"
        )
    if numbers == {result.stdout.strip()}:
        return True, ""
    return False, f"the API is off in a project other than {project!r}, such as a quota project"


# --------------------------------------------------------------------------- #
# Check bodies: pure functions over already-read `gcloud` output. Each returns
# either one hit or `None`, or a list of hits, and knows nothing about the
# manifest.
# --------------------------------------------------------------------------- #


def check_startup_script(instance_name: str, zone: str, serial_text: str) -> dict | None:
    """`serial_text` is one instance's `get-serial-port-output` body. Flags the
    first line carrying one of §2.1's fatal markers (`STARTUP_FAILURE_PATTERN`).

    First match wins and the scan stops, the same as the pre-manifest revision:
    a boot that failed twice is one degraded instance, not two findings.

    The `object` carries the zone because a GCE instance name is unique per
    zone, not per project: `web-1` in `us-central1-a` and `web-1` in
    `us-central1-b` are two different VMs one project can hold at once.
    Unqualified they derive the same finding id, and `validate_findings`
    refuses the *whole* document over the collision — so the run publishes
    nothing at all rather than one merged finding. `networking_audit.py` scopes
    `Router/<region>/<name>` and `ForwardingRule/<scope>/<name>` for the same
    reason.
    """
    lines = serial_text.splitlines()
    # The console buffer outlives a guest reboot, so a failure an operator has
    # since fixed is still in it. Only the last startup-script run counts.
    starts = [index for index, line in enumerate(lines) if STARTUP_RUN_MARKER in line]
    for line in lines[starts[-1] if starts else 0 :]:
        if STARTUP_FAILURE_PATTERN.search(line):
            return {
                "object": f"ComputeInstance/{zone}/{instance_name}",
                "excerpt": redact(line.strip()),
                "impact": (
                    f"Instance {instance_name} failed initialization and may be in a "
                    "degraded or unbootstrapped state."
                ),
                "needs_triage": TRIAGE_GKE_NODE,
            }
    return None


def _metadata_keys(payload: dict, field: str) -> set[str]:
    """The metadata keys set on one `compute` payload.

    `instances list` items carry them under `metadata`, `project-info describe`
    under `commonInstanceMetadata`; both use the same `{"items": [{"key": ...}]}`
    shape, so `field` is the only difference.
    """
    items = ((payload or {}).get(field) or {}).get("items") or []
    return {
        item.get("key", "") for item in items if isinstance(item, dict)
    }


def project_sets_startup_script(project_info: object) -> bool:
    """Does the project's common metadata set a startup script?

    A common-metadata startup script runs on every instance in the project, so
    one here makes the per-instance question moot.
    """
    if not isinstance(project_info, dict):
        return False
    return bool(
        _metadata_keys(project_info, "commonInstanceMetadata")
        & set(STARTUP_SCRIPT_KEYS)
    )


def running_instances(
    instances: list, *, require_startup_script: bool = False
) -> list[tuple[str, str]]:
    """The `(name, zone)` pairs §2.1 has console output to read.

    A non-RUNNING instance has no live serial console, and an item missing
    either field cannot be addressed by a `get-serial-port-output` call.

    `require_startup_script` additionally drops an instance whose own metadata
    sets neither key in `STARTUP_SCRIPT_KEYS`. No script ran on it, so its
    console cannot carry an exit status for one, and reading it is a guaranteed
    miss recorded as a pass. The caller leaves this False when the project's
    common metadata sets a script, because that one runs everywhere.
    """
    out = []
    for inst in instances or []:
        if not isinstance(inst, dict):
            continue
        name = inst.get("name", "")
        zone = _last_segment(inst.get("zone", ""))
        if inst.get("status", "") != RUNNING_STATUS or not name or not zone:
            continue
        if require_startup_script and not (
            _metadata_keys(inst, "metadata") & set(STARTUP_SCRIPT_KEYS)
        ):
            continue
        out.append((name, zone))
    return out


def active_disk_index(disks: list) -> set[str]:
    """Every spelling a live disk answers to.

    A snapshot's `sourceDisk` arrives as a bare name in some payloads and as a
    full `selfLink` in others, so both go in and the membership test matches
    whichever form the snapshot used.
    """
    index: set[str] = set()
    for disk in disks or []:
        if not isinstance(disk, dict):
            continue
        for key in ("name", "selfLink"):
            value = disk.get(key, "")
            if value:
                index.add(value)
    return index


def check_orphaned_snapshot(snapshot: dict, active_disks: set[str], now: datetime.datetime) -> dict | None:
    """One item from `compute snapshots list`. Flags a snapshot whose source
    disk is gone, that no snapshot schedule took, and that is older than
    ninety days.

    A snapshot with no `sourceDisk` at all is never flagged: it is an import or
    a hand-made image, and there is no deleted disk to attribute it to. An
    unparseable `creationTimestamp` is likewise not flagged — an age nobody
    could compute is not an age over the threshold.
    """
    name = snapshot.get("name", "")
    source_disk = snapshot.get("sourceDisk", "")
    source_disk_name = _last_segment(source_disk) if source_disk else ""
    created = snapshot.get("creationTimestamp", "")
    if not source_disk_name:
        return None
    if source_disk_name in active_disks or source_disk in active_disks:
        return None
    # A snapshot a schedule took carries the schedule's policy; Snapshot has no
    # `resourcePolicies` field, which is the disk's. Retained by policy, not
    # orphaned by neglect.
    if any(snapshot.get(key) for key in SNAPSHOT_SCHEDULE_KEYS):
        return None
    if not created:
        return None
    try:
        stamp = datetime.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        age_days = (now - stamp).days
    except (ValueError, TypeError):
        # A timestamp that will not parse, or one with no UTC offset to
        # subtract `now` from. An age nobody could compute is not an age over
        # the threshold — the same silence the pre-manifest revision kept.
        return None
    if age_days <= ORPHAN_AGE_DAYS:
        return None
    return {
        "object": f"Snapshot/{name}",
        "excerpt": (
            f'{{"name": "{name}", "sourceDisk": "{source_disk_name}", '
            f'"creationTimestamp": "{created}"}}'
        ),
        "impact": (
            f"Snapshot {name} incurs ongoing storage charges without active source disk."
        ),
        "needs_triage": TRIAGE_RETENTION_HOLD,
    }


def _scope_of(resource: dict) -> str:
    """The zone or region a scoped Compute resource lives in.

    Carried into every `object` this file derives for a MIG or a node group,
    for the reason `check_startup_script` spells out at length: a Compute name
    is unique per zone, not per project, so two same-named groups in different
    zones derive one finding id and `validate_findings` refuses the whole
    document rather than merging them.
    """
    for key in ("zone", "region"):
        value = resource.get(key)
        if value:
            return _last_segment(str(value))
    return UNRESOLVED_PROJECT


def _count(actions: dict, key: str) -> int:
    """One `currentActions` counter as an int, defaulting to 0.

    The API publishes all thirteen counters on every group, but a changed
    contract or a partial response must read as "no churn observed" rather than
    raise — the alternative is one malformed group costing the project its
    whole MIG check.
    """
    try:
        return int(actions.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def check_mig_convergence(mig: dict) -> dict | None:
    """§2.2's condition against one `instance-groups managed list` entry.

    `creating` and `deleting` both non-zero — the group is adding and removing
    instances at the same moment. A scale-up only creates and a scale-down only
    deletes, so both at once is the resize loop §2.2 was written about, caught
    in the act.

    Skipped while an update is in progress (`status.versionTarget.isReached`
    false): a rolling update with surge creates the new instance and deletes
    the old one at once by design, and a GKE surge upgrade does the same.

    `creatingWithoutRetries` is not a limb. It counts instances the group will
    try once each to create, and a creation that fails lowers `targetSize`, so
    a group that gave up reads as converged on its smaller target: no single
    read can see that stall.

    What is deliberately *not* the condition is `status.isStable == false` on
    its own. Instability is the normal state of any group mid-scale, so flagging
    it would report every healthy autoscaler under load. §2.2's original
    wording was a rate (repeated resizes inside fifteen minutes) and no `gcloud`
    read carries a MIG's resize history to count one; the resize loop caught in
    the act is the part of that intent a single read can establish, which is
    why the slug says `convergence-stalled` rather than `autoscaler-flapping`.
    """
    name = str(mig.get("name", "")).strip()
    actions = mig.get("currentActions")
    if not name or not isinstance(actions, dict):
        return None
    status = mig.get("status") if isinstance(mig.get("status"), dict) else {}
    version_target = status.get("versionTarget")
    if isinstance(version_target, dict) and version_target.get("isReached") is False:
        return None

    creating = _count(actions, "creating")
    deleting = _count(actions, "deleting")
    if not (creating > 0 and deleting > 0):
        return None

    # §2.2's Do-NOT-flag limb excuses GKE node pools undergoing pod-driven
    # scale events. The prefix is mechanical, but whether a given churn is
    # pod-driven is not, so the judgment goes back to the model.
    triage = TRIAGE_GKE_MIG if name.startswith(GKE_MIG_PREFIXES) else None
    scope = _scope_of(mig)
    return {
        "object": f"ManagedInstanceGroup/{scope}/{name}",
        "excerpt": redact(
            f"{name} ({scope}): size={mig.get('size', '?')} "
            f"targetSize={mig.get('targetSize', '?')} "
            f"isStable={status.get('isStable')} "
            f"currentActions creating={creating}, deleting={deleting}"
        )[:MAX_EXCERPT_CHARS],
        "impact": (
            "The group is adding and removing instances at the same time, "
            "which is a resize loop rather than a scale event. Every cycle "
            "pays a full instance boot and the capacity actually serving "
            "traffic oscillates underneath it."
        ),
        "needs_triage": triage,
    }


def autoscales(group: dict) -> bool:
    """§2.4's Do-NOT-flag limb: a node group that grows itself is not short of
    headroom, it is between sizes. Decided from the group's own field, so it is
    applied rather than handed back as triage, and before any per-node read.

    Named positively rather than as "anything but OFF". The Compute v1
    discovery document gives `mode` four values — MODE_UNSPECIFIED, OFF, ON,
    ONLY_SCALE_OUT — and only the last two actually add nodes. Excluding on
    "not OFF" would drop a MODE_UNSPECIFIED group out of the check silently,
    reporting headroom it never measured as headroom it found adequate.
    """
    policy = group.get("autoscalingPolicy")
    return str((policy or {}).get("mode", "")).strip().upper() in AUTOSCALING_MODES


def check_sole_tenant_headroom(group: dict, nodes: list) -> tuple[dict | None, bool]:
    """§2.4's condition against one node group and its `list-nodes` result.

    Returns `(candidate, measured)`. `measured` is False when not one node
    carried both `totalResources` and `consumedResources` — the read ran and no
    figure came back, which is what `checks_unevaluated` exists for. A
    group whose nodes read cleanly and sit under the threshold returns
    `(None, True)`: a verdict, not a missing one. Keeping the two apart is the
    whole point — collapsing them is how a stream reports a clean fleet on the
    strength of reads that never happened.

    The condition is §2.4's conjunction, both halves off the same read:
    utilisation at or above `SOLE_TENANT_UTILISATION_PCT` of aggregate capacity,
    *and* less than one node's worth of vCPU still free. The second half is what
    "without failover host headroom" means — a group at 90% across ten nodes
    still has a whole node spare and survives losing one.
    """
    name = str(group.get("name", "")).strip()
    if not name:
        return None, False

    # §2.4's Do-NOT-flag limb (`autoscales`). `collect_project` does not read
    # such a group's nodes at all; this keeps the function right on its own.
    if autoscales(group):
        return None, True
    # A group holding no nodes has no host to lose: the read established that,
    # which is a verdict rather than a missing one.
    if not nodes:
        return None, True

    total_cpus = consumed_cpus = 0
    total_mem = consumed_mem = 0
    measured_nodes = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        total = node.get("totalResources")
        consumed = node.get("consumedResources")
        if not isinstance(total, dict) or not isinstance(consumed, dict):
            continue
        try:
            node_cpus = int(total.get("guestCpus") or 0)
            node_mem = int(total.get("memoryMb") or 0)
            used_cpus = int(consumed.get("guestCpus") or 0)
            used_mem = int(consumed.get("memoryMb") or 0)
        except (TypeError, ValueError):
            continue
        if node_cpus <= 0:
            # A node reporting no capacity cannot contribute a ratio, and
            # counting it would drag the denominator down and manufacture a
            # utilisation figure out of a malformed record.
            continue
        total_cpus += node_cpus
        total_mem += node_mem
        consumed_cpus += used_cpus
        consumed_mem += used_mem
        measured_nodes += 1

    if not measured_nodes or total_cpus <= 0:
        return None, False

    cpu_pct = 100.0 * consumed_cpus / total_cpus
    mem_pct = 100.0 * consumed_mem / total_mem if total_mem > 0 else 0.0
    one_node_cpus = total_cpus / measured_nodes
    free_cpus = total_cpus - consumed_cpus

    if max(cpu_pct, mem_pct) < SOLE_TENANT_UTILISATION_PCT:
        return None, True
    if free_cpus >= one_node_cpus:
        return None, True

    scope = _scope_of(group)
    return (
        {
            "object": f"NodeGroup/{scope}/{name}",
            "excerpt": redact(
                f"{name} ({scope}): {measured_nodes} node(s), "
                f"vCPU {consumed_cpus}/{total_cpus} ({cpu_pct:.0f}%), "
                f"memory {consumed_mem}/{total_mem} MB ({mem_pct:.0f}%), "
                f"{free_cpus} vCPU free against {one_node_cpus:.0f} per node"
            )[:MAX_EXCERPT_CHARS],
            "impact": (
                "The reservation is at capacity with less than one node's "
                "worth of vCPU spare, so losing a single host leaves nowhere "
                "for its VMs to land. Sole-tenant VMs do not spill onto shared "
                "hardware — they stay down until capacity is added."
            ),
            "needs_triage": TRIAGE_MAINTENANCE,
        },
        True,
    )


def _emit(slug: str, hit: dict, command: str = "") -> dict:
    emitted = {
        "check": slug,
        "namespace": "",
        "object": hit["object"],
        "severity": hit.get("severity") or SEVERITY[slug],
        "excerpt": hit["excerpt"],
        "impact": hit["impact"],
        "needs_triage": hit.get("needs_triage"),
    }
    if command:
        # The read that produced *this* candidate, for a check that issues one
        # per instance or node group. The entry's `commands` record joins and
        # clips every read of the slug, so without it a finding's evidence is
        # a chain of other instances' reads, possibly clipped before its own.
        # `adopt_collector_evidence` prefers this field when it is set.
        emitted["command"] = command
    return emitted


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #


def _serial_argv(instance: str, zone: str, project: str) -> list[str]:
    # `--port=1` is gcloud's own default, so naming it changes nothing that is
    # read; it is spelled out because §2.1's command and its evidence example
    # both spell it, and the model copies this string into `checks_run`.
    return [
        "gcloud", "compute", "instances", "get-serial-port-output", instance,
        f"--zone={zone}", "--port=1", f"--project={project}",
    ]


def collect_project(project: str, *, run: RunFn = default_run) -> dict | None:
    """The single manifest entry for one project (the manifest's `clusters[]` shape,
    reused for a target that is a project rather than a GKE cluster).

    The name is `project/<id>`, the spelling `networking_audit.py` uses, which
    `audit_report.target_kind` reads as a project. An instance's finding
    identity is `ComputeInstance/<zone>/<name>`: a GCE instance name is unique
    per zone, not per project, so the unqualified form lets two VMs derive one
    finding id, and `validate_findings` refuses the whole document over the
    collision (`check_startup_script`).

    No `autopilot` key: the target stands for a project, and a `false` there
    would read as a fleet of Standard clusters.

    None for a project whose own Compute Engine API is off: it holds nothing to
    audit, and a `gate-failed` row would be a coverage gap on every run.
    """
    name = f"{PROJECT_TARGET_PREFIX}{project}"
    # Every read that backed each slug, in the order it ran.
    reads: dict[str, SlugReads] = {}
    candidates: list[dict] = []
    not_applicable: list[dict] = []
    unevaluated: list[dict] = []
    limitations: list[str] = []

    def gated(argv: list[str], slug: str) -> list:
        parsed, result = run_and_gate(argv, run=run)
        if parsed is None:
            stderr = result.stderr.strip()
            if any(marker in stderr for marker in API_DISABLED_MARKERS):
                ours, why_not = refusal_names_project(project, stderr, run=run)
                if ours:
                    raise ComputeApiDisabled(project)
                # Ahead of the stderr, so the clip below cannot take it.
                stderr = f"({why_not}) {stderr}"
            raise GateFailure(
                f"{slug}: {' '.join(argv)} failed (rc={result.rc}): "
                f"{stderr[:ERROR_CLIP_CHARS]}"
            )
        if not isinstance(parsed, list):
            # `--format=json` on a `list` sub-command returns an array. A dict
            # here is an error envelope or a changed contract, and reading zero
            # items off it would report an empty, healthy project.
            raise GateFailure(
                f"{slug}: {' '.join(argv)} returned "
                f"{type(parsed).__name__}, not a JSON array"
            )
        reads.setdefault(slug, SlugReads()).add(" ".join(argv), result)
        return parsed

    try:
        # --- 2.1 startup script failures ---------------------------------- #
        instances = gated(
            ["gcloud", "compute", "instances", "list", "--project", project, "--format=json"],
            STARTUP_SLUG,
        )
        running = running_instances(instances)
        # A startup script is set either on the instance or, project-wide, in
        # the common metadata — and a project-wide one runs on every instance,
        # so the per-instance filter below is only sound once this read says
        # there is none. A read that fails leaves `project_wide` True and every
        # RUNNING instance a target, which is what this collector did before
        # the filter existed: reading a console nobody needed costs seconds,
        # while guessing the other way would declare §2.1 inapplicable over a
        # fleet that does run scripts. No `limitations` entry for the failure —
        # the fallback inflates no coverage claim, and a limitation on this
        # stream's only target would hold every GCE finding open.
        info_argv = [
            "gcloud", "compute", "project-info", "describe",
            "--project", project, "--format=json",
        ]
        info_parsed, info_result = run_and_gate(info_argv, run=run)
        if isinstance(info_parsed, dict):
            reads.setdefault(STARTUP_SLUG, SlugReads()).add(" ".join(info_argv), info_result)
        project_wide = (
            True
            if not isinstance(info_parsed, dict)
            else project_sets_startup_script(info_parsed)
        )
        targets = (
            running
            if project_wide
            else running_instances(instances, require_startup_script=True)
        )
        unread: list[str] = []
        for instance_name, zone in targets:
            argv = _serial_argv(instance_name, zone, project)
            result = run(argv)
            if result.rc != 0 or not result.stdout.strip():
                # Not a project-level gate failure. One VM that stopped
                # mid-sweep, or one the identity cannot read the console of,
                # must not cost the project its snapshot check — but it is not
                # a pass either, so it is named in `limitations` below.
                unread.append(f"{instance_name} ({zone})")
                continue
            hit = check_startup_script(instance_name, zone, result.stdout)
            reads.setdefault(STARTUP_SLUG, SlugReads()).add(" ".join(argv), result)
            if hit:
                candidates.append(_emit(STARTUP_SLUG, hit, " ".join(argv)))

        if not instances:
            not_applicable.append(
                {"check": STARTUP_SLUG, "reason": NO_INSTANCES_REASON}
            )
        elif not running:
            not_applicable.append(
                {
                    "check": STARTUP_SLUG,
                    "reason": NO_RUNNING_INSTANCES_REASON.format(
                        total=len(instances)
                    ),
                }
            )
        elif not targets:
            not_applicable.append(
                {
                    "check": STARTUP_SLUG,
                    "reason": NO_STARTUP_SCRIPT_REASON.format(
                        total=len(running)
                    ),
                }
            )
        elif len(unread) == len(targets):
            # Nothing was examined. The enumeration ran, so the slug has a
            # `commands` entry pending — dropped at emit time, because a
            # recorded command corroborates exactly the `checks_run` claim
            # this listing exists to refuse.
            unevaluated.append({"check": STARTUP_SLUG, "reason": UNREAD_SERIAL_REASON})
            limitations.append(UNREAD_SERIAL_LIMITATION.format(total=len(targets)))
        elif unread:
            limitations.append(
                PARTIAL_SERIAL_LIMITATION.format(
                    read=len(targets) - len(unread),
                    total=len(targets),
                    names=", ".join(sorted(unread)),
                )
            )

        # --- 2.2 MIG convergence -------------------------------------------- #
        migs = gated(
            [
                "gcloud", "compute", "instance-groups", "managed", "list",
                "--project", project, "--format=json",
            ],
            MIG_SLUG,
        )
        if not migs:
            not_applicable.append({"check": MIG_SLUG, "reason": NO_MIGS_REASON})
        else:
            for mig in migs:
                if not isinstance(mig, dict):
                    continue
                hit = check_mig_convergence(mig)
                if hit:
                    candidates.append(_emit(MIG_SLUG, hit))

        # --- 2.4 sole-tenant headroom --------------------------------------- #
        groups = gated(
            [
                "gcloud", "compute", "sole-tenancy", "node-groups", "list",
                "--project", project, "--format=json",
            ],
            SOLE_TENANT_SLUG,
        )
        if not groups:
            not_applicable.append(
                {"check": SOLE_TENANT_SLUG, "reason": NO_NODE_GROUPS_REASON}
            )
        else:
            measured_any = False
            excluded = 0
            unmeasured: list[str] = []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_name = str(group.get("name", "")).strip()
                if not group_name:
                    continue
                if autoscales(group):
                    # Excluded by §2.4, so neither read nor counted unmeasured:
                    # a refused `list-nodes` on it must not make the run partial.
                    measured_any = True
                    excluded += 1
                    continue
                nodes_argv = [
                    "gcloud", "compute", "sole-tenancy", "node-groups",
                    "list-nodes", group_name, f"--zone={_scope_of(group)}",
                    f"--project={project}", "--format=json",
                ]
                parsed, result = run_and_gate(nodes_argv, run=run)
                # Not gated to the project: one node group whose nodes refuse
                # to list must not cost the project its snapshot check, the way
                # one unreadable serial console does not. It costs this check
                # its verdict only if *every* group comes back unmeasurable.
                if not isinstance(parsed, list):
                    unmeasured.append(f"{group_name} ({_scope_of(group)})")
                    continue
                reads.setdefault(SOLE_TENANT_SLUG, SlugReads()).add(" ".join(nodes_argv), result)
                hit, measured = check_sole_tenant_headroom(group, parsed)
                measured_any = measured_any or measured
                if not measured:
                    unmeasured.append(f"{group_name} ({_scope_of(group)})")
                if hit:
                    candidates.append(_emit(SOLE_TENANT_SLUG, hit, " ".join(nodes_argv)))
            if not measured_any:
                unevaluated.append(
                    {
                        "check": SOLE_TENANT_SLUG,
                        "reason": UNMEASURED_NODE_GROUPS_REASON.format(groups=len(groups)),
                    }
                )
                limitations.append(UNMEASURED_NODE_GROUPS_LIMITATION.format(groups=len(groups)))
            elif unmeasured:
                limitations.append(
                    PARTIAL_NODE_GROUPS_LIMITATION.format(
                        measured=len(groups) - len(unmeasured) - excluded,
                        total=len(groups) - excluded,
                        names=", ".join(sorted(unmeasured)),
                    )
                )

        # --- 2.5 orphaned snapshots ---------------------------------------- #
        # Both reads are gated: a `disks list` that failed used to skip the
        # snapshot check silently, which left the project in scope carrying one
        # check and no sign that the other had been abandoned. It is now what
        # it is — a read that failed, and a project the run could not cover.
        disks = gated(
            ["gcloud", "compute", "disks", "list", "--project", project, "--format=json"],
            SNAPSHOT_SLUG,
        )
        snapshots = gated(
            ["gcloud", "compute", "snapshots", "list", "--project", project, "--format=json"],
            SNAPSHOT_SLUG,
        )
        active = active_disk_index(disks)
        now = datetime.datetime.now(datetime.timezone.utc)
        # A project with no snapshots at all is the same shape 00657eab fixed
        # for workloads: the check ran, found nothing to attribute, and was
        # recorded as a clean pass over the snapshots — of which there were
        # none. The reference install holds zero, so §2.5 published that pass
        # every run.
        if not snapshots:
            not_applicable.append({"check": SNAPSHOT_SLUG, "reason": NO_SNAPSHOTS_REASON})
        else:
            for snapshot in snapshots:
                if not isinstance(snapshot, dict):
                    continue
                hit = check_orphaned_snapshot(snapshot, active, now)
                if hit:
                    candidates.append(_emit(SNAPSHOT_SLUG, hit))
    except ComputeApiDisabled:
        log(f"{project}: Compute Engine API is not enabled; nothing here to audit")
        return None
    except GateFailure as exc:
        return {
            "name": name,
            "project": project,
            "location": GLOBAL_LOCATION,
            "outcome": OUTCOME_GATE_FAILED,
            "error": str(exc)[:ERROR_CLIP_CHARS],
        }

    # Neither kind of declaration records a command: one that ran corroborates
    # exactly the `checks_run` claim each exists to refuse.
    not_applicable_slugs = {entry["check"] for entry in not_applicable + unevaluated}
    entry = {
        "name": name,
        "project": project,
        "location": GLOBAL_LOCATION,
        "outcome": OUTCOME_COLLECTED,
        "commands": [
            {"check": slug, **_joined_record(slug_reads)}
            for slug, slug_reads in reads.items()
            if slug not in not_applicable_slugs
        ],
        "candidates": candidates,
        # Empty rather than absent where nothing applies: the reader tolerates
        # either, and tests subscript this key directly.
        "checks_not_applicable": not_applicable,
    }
    if unevaluated:
        entry["checks_unevaluated"] = unevaluated
    if limitations:
        entry["limitations"] = "; ".join(limitations)
    return entry


def crashed_entry(project: str, exc: BaseException) -> dict:
    """The `clusters[]` entry for a worker that raised something unmodelled.

    `future.result()` re-raises, so one unhandled exception on one project
    would abort `collect_fleet` — and the SOP invokes this collector as
    `compute_fleet_audit.py > manifest_gce-compute-fleet-audit.json`, so by
    then the shell has already truncated the file. The run would lose every
    project to one bad object instead of one.
    """
    log(f"{project}: collector raised {type(exc).__name__}: {exc}")
    return {
        "name": f"{PROJECT_TARGET_PREFIX}{project}",
        "project": project,
        "location": GLOBAL_LOCATION,
        "outcome": OUTCOME_GATE_FAILED,
        "error": f"collector raised {type(exc).__name__}: {exc}"[:ERROR_CLIP_CHARS],
    }


def unresolved_entry() -> dict:
    """The stand-in for a run that enumerated no project at all.

    An empty `clusters` list reads as a fleet with nothing in it, which is a
    clean, fully covered scope. The SOP routes this name to `scope.skipped`.
    """
    return {
        "name": f"{PROJECT_TARGET_PREFIX}{UNRESOLVED_PROJECT}",
        "project": UNRESOLVED_PROJECT,
        "location": GLOBAL_LOCATION,
        "outcome": OUTCOME_GATE_FAILED,
        "error": (
            "No GCP project resolved from --project-id, MONITORED_PROJECT_IDS, "
            "`gcloud projects list`, GCP_PROJECT_ID/GKE_PROJECT_ID/PROJECT_ID "
            "or `gcloud config get-value project`."
        ),
    }


def unenumerated_entry(notes: list[str], declared_sweep: bool = False) -> dict:
    """The target standing for every project this run did not enumerate.

    A narrowed or failed discovery leaves the rest of the fleet unnamed, and a
    warning on stderr is not in the manifest: without this row the run reads
    as the whole fleet and `finish` resolves every ledger finding on a project
    it never looked at. One row however many notes, because `finish` keys the
    manifest by name.
    """
    # Under a declared scope the fleet's size is known exactly, so the tail says
    # so rather than calling it unknown.
    tail = fleet_scope_args.DECLARED_SCOPE_TAIL if declared_sweep else UNENUMERATED_TAIL
    return {
        "name": UNENUMERATED_PROJECTS_TARGET,
        "project": "",
        "location": GLOBAL_LOCATION,
        "outcome": OUTCOME_GATE_FAILED,
        # The notes are clipped, not the tail: the tail is what says whether
        # the fleet's size is known, and a long `projects list` refusal would
        # otherwise push it out.
        "error": f"{'; '.join(notes)[: ERROR_CLIP_CHARS - len(tail) - 2]}. {tail}",
    }


def collect_fleet(
    project: str | None = None,
    *,
    run: RunFn = default_run,
    max_workers: int = MAX_WORKERS,
) -> dict:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    notes: list[str] = []
    projects = get_target_projects(project, notes, run=run)
    log(f"auditing {len(projects)} project(s)")
    for note in notes:
        log(f"WARNING: {note}")

    entries: list[dict | None] = [{} for _ in projects]
    if projects:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(collect_project, p, run=run): i for i, p in enumerate(projects)}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    entries[index] = future.result()
                except Exception as exc:  # noqa: BLE001 — see crashed_entry
                    entries[index] = crashed_entry(projects[index], exc)
    else:
        # Under a declared scope the reason is in `notes` (nothing readable, the
        # flags missing, an override outside the scope); the generic row would
        # name remedies the scope forbids and the top-level error would take it.
        entries = [] if declared_scope.declared else [unresolved_entry()]
    # `collect_project` returns None only for a project whose own Compute API
    # is off. It contributes no target, but the reason is kept: when nothing
    # else was read, it is the cause the top-level `error` has to name.
    api_off = [project for project, entry in zip(projects, entries) if entry is None]
    entries = [entry for entry in entries if entry is not None]
    if notes:
        entries.append(unenumerated_entry(notes, declared_scope.sweep_is_declared((project or "").strip())))

    manifest = {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
        "audit": AUDIT_ID,
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clusters": entries,
    }
    # No target was read. The design's top-level `error` says so, and the SOP
    # answers it by not calling `finish`: the run has nothing to publish, and
    # `finish` would refuse it on shape rather than on this reason.
    if not any(entry.get("outcome") == OUTCOME_COLLECTED for entry in entries):
        reasons = [str(entry.get("error", "")) for entry in entries if entry.get("error")]
        if api_off:
            reasons.insert(
                0,
                f"the Compute Engine API is off in {', '.join(sorted(api_off))}, "
                "which holds nothing for this audit to read",
            )
        manifest["error"] = (
            f"no project could be read: {len(entries)} target(s), none collected"
            + (f"; {reasons[0]}" if reasons else "")
        )[:ERROR_CLIP_CHARS]
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project-id",
        help=(
            "single project to audit (on an install with a declared scope, one the fleet_scope tool lists, passed with "
            "its collector_args); omit to sweep --scope-projects when given, else MONITORED_PROJECT_IDS, or else the "
            "configured project plus every project `gcloud projects list` returns"
        ),
    )
    fleet_scope_args.add_scope_arguments(parser)
    parser.add_argument(
        "--output",
        help="also write the manifest here; it goes to stdout either way",
    )
    args = parser.parse_args(argv)
    declared_scope.set(args.scope_projects, args.scope_unread)
    manifest = collect_fleet(args.project_id)
    text = json.dumps(manifest, indent=2)
    if args.output:
        try:
            directory = os.path.dirname(os.path.abspath(args.output))
            os.makedirs(directory, exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            log(f"failed to write {args.output}: {exc}")
            return 1
    print(text)
    return 1 if manifest.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
