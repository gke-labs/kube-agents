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
OOMKilled termination, the healthy workloads are Ready, the idle pool's node is
Ready and tainted, slot b's control plane is still one minor behind its
channel and on no patch newer than that channel offers, slots a, b and d keep
the master authorized-networks block slot c is the drift outlier against, and
slot c's cluster is past the drift SOP's 24-hour age gate -- and reports the
roles whose fixture is there but not in the shape the cases depend on. One
role asserts the room the others schedule into:
`default-pool-headroom` reads seeded-a's default-pool nodes and every pod
bound to them and fails when the busier node drops below the headroom margin
the scan keeps for the next fixtures, ten of them at 10m (#3607: a 10m
DaemonSet pod stranded for three days and read as drift on its own role,
which a re-apply cannot fix).

It runs in three places. `scripts/verify_ci_pool_project.py` runs it after the
presence pass and fails a project whose fixtures have drifted. The CI health
bot's hourly scan (`scripts/eval_dashboard/fixture_state.py`, #1550) runs it
against every pool project with `--report`, which writes each role's verdict
as JSON beside the summary so the scan need not parse the warnings. And
`hack/ci-eval-pr.sh` runs it with `--report` against its leased project,
where `bench/kube_agents_bench/fixture_drift.py` turns each `drifted` role
into a `fixture-drift:<role>` infrastructure record for the cases that need
it. Detection is this script's whole job: it repairs nothing. The reconcile's
re-apply (hack/fleet_reconcile.py) repairs drift, and only the reconcile's
reset of the startup-fail VM repairs that VM's rotated serial console
(`serial_contains`); the serial-flood VM's own flood pass repairs its missing
filler, and a one-off hand reset is the repair when that flood breaks.
`--wait` is for a fixture that has just been rescheduled -- the crashloop needs
its first restart before OOMKilled evidence exists -- and keeps re-reading a
role that is positively out of shape until it converges or the deadline
passes. A role whose reads fail does not hold the wait: nothing about it can
converge, so it is re-read only while some other role is worth waiting for. A
role still drifted at the deadline gets a `<role>.drift` file beside its
kubeconfig, one failed assertion per line with what was observed, which is the
output contract a consumer reads.

A project-scoped role (`cluster_slot: null`: the startup-fail VM, the
serial-flood VM, the world-open firewall rule and its VM, the health-check
shadow rules on that VM's tag, the peered jumbo-frame network, the two orphaned snapshots) is published as
`<role>.confirmed` alone, and its subjects
are read with `gcloud compute instances list`, `gcloud compute firewall-rules
list`, `gcloud compute networks list` or `gcloud compute snapshots list` in the
project. A cluster role can carry one such subject too, `reservation/<prefix>*`,
read with `gcloud compute reservations list`. `serial_contains` reads an instance subject's serial port 1 output as
well, with `gcloud compute instances get-serial-port-output`: the startup-fail
VM's failure line is printed at boot only, and the buffer that holds it rotates
within weeks; the serial-flood VM's flood filler is the opposite designed
state, the console whose boot lines have rotated out, which the VM itself
renews daily. `source_disk_gone` reads the project's disks for the name a
snapshot's `sourceDisk` ends in.

One kind of assertion is a clock rather than a shape: `ready_after_hours`,
on the orphaned-snapshot roles, whose snapshots the GCE compute audit ignores
until they are more than 90 full days old, and `creationTimestamp` cannot be
backdated; and on the finished-Job family, whose Jobs the cost audit ignores
until they finished 7 days ago. A role failing that and nothing else is NOT READY, reported with
the time its clock passes, never drifted: a project applied today is
registrable, the hourly scan shows the date, and no re-apply is asked for.

A `repository/<path>` subject is a file in the pool project's GitOps
repository (`gke-agentic/<project>-infra`), the one place a fixture lives
that is neither on a cluster nor in the project: the declared-intent note
the declared-* roles rest on. Its one op, `as_seeded`, holds the file's git
blob sha to the one provisioning seeds, read through the pool verifier
(scripts/verify_ci_pool_project.py, `gitops_seeded_file_state`), which owns
the seeded content and the repository read; one contents read per path per
pass serves every role on it. Forty registered projects carried the
five-item note after a sixth declaration landed, and the cases on it graded
`fail` on a correct report because this script read the node pools only
(#5827). A note that is not the seeded one, or is missing from a repository
that answers, is drift; a repository the credential cannot see (gh and the
API answer 404 for that too) or a transient leaves the role not checked. A
pass with no credential at all does not make the assertion: the role rests
on what the identity can read and the report's `unasserted` list names the
subject, so the hourly scan, which holds no credential for the pool
repositories by design, still reads the roles healthy and can still end an
incident on them. The presubmit and the nightly read the note with their
grading token (hack/ci-eval-pr.sh, scan_lease_fixtures). Only an operator's
replace clears a note drift, so it never holds a `--wait`.

What it never does: mutate anything (every read is `kubectl get -o json`,
`gcloud container clusters describe`, `gcloud compute ... list`, `gcloud
compute instances get-serial-port-output` or a GET of the contents API),
address a cluster it discovered itself
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
    U not checked, R not yet ready (project P)

With `--report PATH` the same verdicts are written as JSON, one entry per
catalog role: `converged`, `drifted`, `unchecked` or `not_ready` with the
lines above as `detail`, `unpublished` for a role the runner did not publish
(no kubeconfig, or for a project-scoped role no `.confirmed` list), and
`no_state` for one that declares no assertions; beside them, `unasserted`
lists the subjects the pass had no credential to read, with their roles and
the reason.

Exit 0 whenever the fleet was looked at, whatever it found; 1 on a repository
bug (unreadable catalog, malformed `state` entry, missing directory).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The pool verifier, which owns what provisioning seeds into a pool
# repository and the read of it; loaded on the first repository subject only,
# so a catalog with none never pays for it (see _verifier).
VERIFIER_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_ci_pool_project.py"
VERIFIER_MODULE_NAME = "verify_ci_pool_project"

# Files the runner (hack/fleet-kubeconfigs.sh) writes and this script reads or
# writes beside them. `.drift` is this script's own output, one file per
# drifted role; the other three names are the runner's.
KUBECONFIG_SUFFIX = ".kubeconfig"
# What the runner publishes for a project-scoped role, which has no
# kubeconfig: the list of what its probes saw.
CONFIRMED_SUFFIX = ".confirmed"
DRIFT_SUFFIX = ".drift"
CONTEXT_FILE = ".fleet-context"
MARKER_FILE = ".kube-agents-fleet-kubeconfigs"

# The subject that means "the GKE cluster carrying this role itself", read
# with `gcloud container clusters describe` rather than kubectl. Slots b and c
# carry control-plane defects that no in-cluster object shows.
CLUSTER_SUBJECT = "cluster"

# `repository/<path>`: a file in the pool project's GitOps repository, read
# through the verifier. The path is relative, slash-separated, with no `..`
# step and none of the shell's characters; and it must be one provisioning
# seeds (the verifier's GITOPS_SEEDED_FILES), or the assertion has nothing to
# hold the file to. scripts/test_eval_rosters.py reads REPOSITORY_SUBJECT to
# learn which roles the scan guards in the pool repository, and
# bench/kube_agents_bench/fixture_drift.py matches drift lines on it.
REPOSITORY_SUBJECT = "repository"
# Every step starts with a letter or digit, so `..`, a hidden file and a
# leading slash are refused by shape.
REPOSITORY_SUBJECT_RE = re.compile(
    rf"{REPOSITORY_SUBJECT}/(?P<path>[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*)"
)
# How much of a blob sha a drift line shows: git's own short form.
BLOB_SHA_SHORT = 8

# A project-scoped role (`cluster_slot: null`) is a Compute Engine object no
# cluster carries. Its subjects are `<kind>/<name-prefix>*`, read with
# `gcloud compute <collection> list` in the project, and this is the kind to
# collection map. The runner's probe grammar (hack/fleet-kubeconfigs.sh,
# PROJECT_PROBE) accepts the first four kinds. A network's listing carries
# its `peerings`, which is how the declared-mtu-peering role reads both
# halves of its peering, one list per network.
PROJECT_COLLECTIONS = {"instance": "instances", "firewall": "firewall-rules", "network": "networks", "snapshot": "snapshots", "reservation": "reservations"}
PROJECT_SUBJECT_RE = re.compile(r"(instance|firewall|network|snapshot|reservation)/(?P<prefix>[a-z0-9][a-z0-9-]*)\*")
# A reservation is the project's, and the node pool that names it is a
# cluster's. So this one project subject is also for a cluster role: each of
# the two reservation roles (missing-reservation-pool and
# missing-reservation-autoscaled) asserts on its pool and on the absence of
# the reservation that the pool names. It is a state subject only: no probe takes
# it, because a probe passes when an object is there.
RESERVATION_KIND = "reservation"
# The collection `source_disk_gone` reads beside a snapshot subject.
DISKS_COLLECTION = "disks"
# What gcloud prints, with exit 0, when a part of a list did not answer (a zone
# that failed, a refused request): the list is then not the whole project's.
PARTIAL_LIST_WARNING = "Some requests did not succeed"
# Under that warning gcloud lists each part that failed as one item with this
# start, and this is the reason when it lists none.
PARTIAL_LIST_ITEM_START = "- "
PARTIAL_LIST_NO_ITEM = "gcloud named no failed part"
# The exit status of a tool that is there and cannot be started, as a shell
# has it for a file that is not executable.
STATUS_CANNOT_EXECUTE = 126

# Keys in `.fleet-context`, as the runner writes them: `project=<id>`; per
# discovered slot, `cluster.<slot>=<name>` and `location.<slot>=<location>`;
# and per catalog role, `slot.<role>=<slot>` (read by the verifier, not here).
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
# One path step: a key, optionally followed by `[*]`, `[N]`, a
# `[?(@.key=='value')]` filter, or a `["quoted.key"]` sub-key for a name the
# bare form cannot carry -- a label or selector key with dots and a slash,
# `metadata.labels["pod-security.kubernetes.io/enforce-version"]` -- so one
# key of a map is asserted without comparing the whole map. Bare keys allow
# the hyphens GKE puts in exclusion names (`maintenanceExclusions.hold-the-minor-lag`).
PATH_STEP_RE = re.compile(
    r"(?P<key>[A-Za-z0-9_-]+)"
    r"(?:\[(?:(?P<all>\*)|(?P<index>\d+)|\?\(@\.(?P<fkey>[A-Za-z0-9_-]+)=='(?P<fval>[^']*)'\)|\"(?P<qkey>[^\"]+)\")\])?"
)

# Comparison operators, applied to the LIST of values a path matched. `eq`,
# `ge`, `after_now`, `age_hours_ge`, `minor_behind_channel_default` and
# `not_ahead_of_channel` want exactly one value;
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
# `not_ahead_of_channel` wants a version the cluster's own release channel
# offers, or one it has moved past: some patch of the same minor it offers is
# newer (or, when it offers none of that minor, some version at all). A
# version newer than all of those is ahead of the channel. seeded-b ran
# 1.34.12-gke.1011000, newer than both 1.34 patches REGULAR carried, and agents
# compared it with them and called it current or ahead (#3025, #3269). A patch
# the channel has moved past is not drift: upgrades cases read it.
OP_NOT_AHEAD = "not_ahead_of_channel"
# `age_hours_ge` wants an RFC 3339 timestamp at least `value` hours in the
# past. The drift SOP leaves a cluster whose `createTime` is under 24 hours old
# out of every cohort (agents/platform/governance/fleet_consistency_drift_sop.md
# §1), so a replaced seeded cluster silences the drift finding for a day.
OP_AGE_HOURS_GE = "age_hours_ge"
# `node_cpu_spare_ge` reads the matched nodes whole plus the pods bound to each
# (one all-namespaces pod read per role per pass) and wants every node to keep
# at least `value` millicores of allocatable CPU unrequested, counting a pod
# the way the scheduler does: the larger of its containers' sum (restartable
# init containers included) and its biggest ordinary init container, plus the
# pod's overhead; Succeeded and Failed pods excluded. No node matched fails it;
# a matched node that reports no allocatable yet (still registering) leaves the
# role not checked for the pass rather than drifted.
OP_NODE_CPU_SPARE = "node_cpu_spare_ge"
# `serial_contains` reads the serial port 1 output of every instance a
# project-scoped `instance/<prefix>*` subject matched and wants `value`, a
# literal string, in at least one of them. No instance matched fails it; a
# console read that failed, with no other console holding the string, leaves
# the role not checked for the pass rather than drifted. The output is a ring
# buffer of about 1 MiB, so a line the guest prints at boot only is there
# until later output pushes it out.
OP_SERIAL_CONTAINS = "serial_contains"
# `as_seeded` reads a `repository/<path>` subject whole and takes no value:
# the file's blob sha is the one provisioning seeds at that path (the
# verifier's gitops_seeded_blob_sha). No object (a 404 from a repository that
# answers) fails it: a missing note files the posture as a finding as a stale
# one does.
OP_AS_SEEDED = "as_seeded"
# `ready_after_hours` is `age_hours_ge` for a fixture whose audit has an age
# gate the stack cannot shortcut: the orphaned-snapshot roles' snapshots are
# invisible to the GCE compute audit's orphaned-snapshots check until they are
# more than 90 full days old (`(now - creationTimestamp).days > 90`, its
# collector's ORPHAN_AGE_DAYS), and creationTimestamp is server-set. The
# finished-Job family is the second user: the cost audit files a finished Job
# once its `status.completionTime` is 7 days old. Failing
# it is not drift: the role is NOT READY, reported with the time the clock
# passes, so a project applied today is registrable and the hourly scan shows
# a date rather than a fixture out of shape. An unparseable timestamp is a
# broken object, which is drift.
OP_READY_AFTER_HOURS = "ready_after_hours"
# `source_disk_gone` reads the project's disks for the name each matched
# snapshot's `sourceDisk` ends in and wants none: the orphaned-snapshots check
# files a snapshot only when its source names no live disk, by name or by
# selfLink, so a disk re-created under that name un-orphans it. No snapshot
# matched fails it, and so does a snapshot with no `sourceDisk` at all, which
# the check never flags; a failed disk list, with no live disk found, leaves
# the role not checked for the pass rather than drifted.
OP_SOURCE_DISK_GONE = "source_disk_gone"
# The words a not-ready line carries before the time its gate passes, which
# the hourly scan's document shows as the role's detail.
READY_AT = "ready at"
# Ops a `--wait` cannot see converge, so their drift does not hold the pass:
# the clock clears an age, in hours (and the one drift `ready_after_hours`
# writes, a timestamp that is not one, is a second snapshot under the prefix
# or a broken one, which a hand deletion clears, or a Job with no
# `completionTime`, which failed or has not finished); a `serial_contains` drift
# clears per role on a clock no scan should wait on (only a reboot puts the
# startup-fail VM's failure line back, and the serial-flood VM's flood pass
# restores its filler a few minutes after a boot, longer than a pass waits);
# only the next reconcile's re-run of the planter deletes a re-created source
# disk, and only an operator's replace puts the seeded file back.
OPS_A_WAIT_CANNOT_CLEAR = frozenset({OP_AGE_HOURS_GE, OP_READY_AFTER_HOURS, OP_SERIAL_CONTAINS, OP_SOURCE_DISK_GONE, OP_AS_SEEDED})
OPS_WITH_VALUE = frozenset({OP_EQ, OP_GE, OP_ANY_EQ, OP_ANY_GE, OP_NONE_EQ, OP_MINOR_BEHIND, OP_NODE_CPU_SPARE, OP_AGE_HOURS_GE, OP_SERIAL_CONTAINS, OP_READY_AFTER_HOURS})
# Ops whose `value` must be a number; anything else is a catalog bug.
OPS_WITH_NUMERIC_VALUE = frozenset({OP_AGE_HOURS_GE, OP_READY_AFTER_HOURS})
# Ops whose `value` must be a non-empty string.
OPS_WITH_STRING_VALUE = frozenset({OP_SERIAL_CONTAINS})
OPS_WITHOUT_VALUE = frozenset({OP_ABSENT, OP_AFTER_NOW, OP_NOT_AHEAD, OP_AS_SEEDED, OP_SOURCE_DISK_GONE})
OPS = OPS_WITH_VALUE | OPS_WITHOUT_VALUE
# Ops that read the subject's objects whole and therefore take no path.
OPS_WITHOUT_PATH = frozenset({OP_ABSENT, OP_NODE_CPU_SPARE, OP_SERIAL_CONTAINS, OP_AS_SEEDED, OP_SOURCE_DISK_GONE})
# The repository subject's only op, and that op's only subject.
REPOSITORY_ONLY_OPS = frozenset({OP_AS_SEEDED})
# Ops whose subject must be a project-scoped `instance/<prefix>*`.
INSTANCE_SUBJECT_ONLY_OPS = frozenset({OP_SERIAL_CONTAINS})
INSTANCE_KIND = "instance"
# Ops whose subject must be a project-scoped `snapshot/<prefix>*`.
SNAPSHOT_SUBJECT_ONLY_OPS = frozenset({OP_SOURCE_DISK_GONE})
SNAPSHOT_KIND = "snapshot"
# The `sourceDisk` field of a `compute snapshots list` item: a URL ending in
# the disk's name (or, in some payloads, the bare name).
SNAPSHOT_SOURCE_FIELD = "sourceDisk"
# The serial port the guest agent and systemd write to; gcloud's own default,
# named so the command line says which console was read.
SERIAL_PORT = 1
# A Compute Engine zone as the instance list's `zone` URL ends in it.
ZONE_RE = re.compile(r"[a-z]+-[a-z]+[0-9]+-[a-z]")
# A Compute Engine instance name, as GCE defines it: a letter first, and
# hyphens anywhere inside, `--` included. No flag or shell metacharacter fits.
INSTANCE_NAME_RE = re.compile(r"[a-z]([-a-z0-9]*[a-z0-9])?")
# A disk name has the same shape; `source_disk_gone` puts one on a command line.
DISK_NAME_RE = INSTANCE_NAME_RE
# Ops whose subject must be a `node?<selector>`.
NODE_SELECTOR_ONLY_OPS = frozenset({OP_NODE_CPU_SPARE})
NODE_KIND = "node"
POD_KIND = "pod"
ALL_NAMESPACES_FLAG = "-A"
# A pod in a terminal phase holds no CPU on its node.
POD_PHASES_RELEASED = frozenset({"Succeeded", "Failed"})
# An init container with this restartPolicy is a sidecar: it keeps running
# beside the containers, so its request adds to theirs.
INIT_RESTART_SIDECAR = "Always"
# Kubernetes CPU quantity suffixes, as millicores per unit; a bare number is cores.
CPU_SUFFIX_MILLICORES = {"m": 1.0, "u": 1e-3, "n": 1e-6}
MILLICORES_PER_CORE = 1000.0
SECONDS_PER_HOUR = 3600.0
# RFC 3339's UTC designator, and the offset Python's parser wants for it.
RFC3339_UTC_SUFFIX = "Z"
ISO_UTC_OFFSET = "+00:00"
# Only the cluster subject can be compared against its channel's versions.
CLUSTER_ONLY_OPS = frozenset({OP_MINOR_BEHIND, OP_NOT_AHEAD})

# GKE version strings: `1.33.4-gke.1134000`. The minor-behind operator reads
# only the minor; `not_ahead_of_channel` orders whole versions, and refuses
# anything else rather than misorder it (bench/tf/fleet/seeded_b_pin.py
# parses the same shape).
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.")
GKE_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-gke\.(\d+)$")
# The (major, minor) prefix of a parsed version.
MINOR_PREFIX_LEN = 2
# Fields of a `clusters describe` document the two channel operators read
# beside the path they were given, and of the get-server-config channel entry.
RELEASE_CHANNEL_PATH = "releaseChannel.channel"
SERVER_CONFIG_CHANNELS = "channels"
SERVER_CONFIG_CHANNEL = "channel"
SERVER_CONFIG_DEFAULT = "defaultVersion"
SERVER_CONFIG_VALID = "validVersions"

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
# the four counts off it and test_verify_ci_pool_project.py renders this
# format string to prove its regex still matches.
SUMMARY_FORMAT = (
    "Seeded-fleet fixture state: {converged} role(s) in their designed state, "
    "{drifted} drifted, {unchecked} not checked, {not_ready} not yet ready (project {project})"
)

# The warning for a subject the pass had no credential to read, one per
# subject. scripts/verify_ci_pool_project.py (FLEET_STATE_UNASSERTED_MARK)
# recognises it on the script's stderr; a test holds the two together.
UNASSERTED_WARNING = (
    "{subject} was not asserted in {project} (no credential to read it with: {reason}); "
    "roles {roles} are judged on their other assertions"
)

# `--report` verdicts, one per catalog role. The first four are the summary
# line's four counts; the last two are roles the summary does not count.
REPORT_SCHEMA_VERSION = 1
VERDICT_CONVERGED = "converged"
VERDICT_DRIFTED = "drifted"
VERDICT_UNCHECKED = "unchecked"
VERDICT_NOT_READY = "not_ready"
VERDICT_UNPUBLISHED = "unpublished"
VERDICT_NO_STATE = "no_state"

EXIT_OK = 0
EXIT_REPOSITORY_BUG = 1


class CatalogError(ValueError):
    """A `state` entry this script cannot evaluate: a repository bug."""


class Unreadable(Exception):
    """The subject could not be read this pass; nothing is known about it."""


class Unasserted(Exception):
    """The pass has nothing to read the subject with, so the assertion was
    not made: a repository subject with no GitHub credential at all. Not a
    failed read -- the role is judged on what this identity can read, and the
    report names what it left out -- because the hourly scan holds no such
    credential by design, and a role it could never read as healthy would
    hold every incident on it open and the digest's clean line out of reach."""


class NotReady(Exception):
    """A `ready_after_hours` assertion that fails only because the fixture's
    clock has not run: the line says when it passes. check_role files it apart
    from drift, and a role failing nothing else is NOT READY."""


def _warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


_VERIFIER = None


def _verifier():
    """scripts/verify_ci_pool_project.py as a module, loaded once: the one
    copy of what provisioning seeds into a pool repository (GITOPS_SEEDED_FILES,
    gitops_seeded_blob_sha), the read of it (gitops_seeded_file_state) and
    the repair it prints (gitops_seeded_file_repair)."""
    global _VERIFIER
    if _VERIFIER is None:
        spec = importlib.util.spec_from_file_location(VERIFIER_MODULE_NAME, VERIFIER_SCRIPT)
        if spec is None or spec.loader is None:
            raise CatalogError(f"cannot load the pool verifier at {VERIFIER_SCRIPT}, which a repository subject reads through")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _VERIFIER = module
    return _VERIFIER


def names_reservations(subject) -> bool:
    """Does `subject` name the project's reservations? That kind is the project
    subject on each role, a cluster role too, and never a kubectl kind, so a
    `reservation?<selector>` is refused as malformed."""
    return isinstance(subject, str) and subject.startswith((f"{RESERVATION_KIND}/", f"{RESERVATION_KIND}?"))


def repository_path(subject: str) -> str | None:
    """The path a `repository/<path>` subject names; None for any other subject."""
    match = REPOSITORY_SUBJECT_RE.fullmatch(str(subject))
    return match.group("path") if match else None


def _note(message: str) -> None:
    print(f"NOTE: {message}", file=sys.stderr)


# --- Catalog ------------------------------------------------------------------


def parse_path(path: str) -> list[tuple]:
    """`status.conditions[?(@.type=='Ready')].status` -> a list of steps.

    Each step is ("key", name) followed optionally by ("all",), ("index", n),
    ("filter", key, value) or, for `["quoted.key"]`, a second ("key", name).
    Raises CatalogError on anything else, so a typo in the catalog fails the
    run rather than matching nothing forever.
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
        elif m.group("qkey"):
            steps.append(("key", m.group("qkey")))
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
        project_scoped = slot is None
        if not project_scoped and (not isinstance(slot, str) or not NAME_RE.fullmatch(slot)):
            raise CatalogError(f"role {role!r} names a malformed cluster slot {slot!r}")
        namespace = spec.get("namespace")
        if namespace is not None and not NAME_RE.fullmatch(str(namespace)):
            raise CatalogError(f"role {role!r} names a malformed namespace {namespace!r}")
        if project_scoped and namespace is not None:
            raise CatalogError(f"role {role!r} is project-scoped and names a namespace {namespace!r}")
        state = spec.get("state")
        if not isinstance(state, list):
            raise CatalogError(f"role {role!r} declares no `state` list")
        entries = []
        for i, entry in enumerate(state):
            where = f"role {role!r} state[{i}]"
            if not isinstance(entry, dict):
                raise CatalogError(f"{where} is not a mapping")
            subject = entry.get("subject")
            in_repository = repository_path(subject) if isinstance(subject, str) else None
            if in_repository is not None:
                # A seeded file in the pool repository, on a cluster role or a
                # project-scoped one alike (the repository is the project's):
                # the verifier must know what provisioning puts there, or
                # there is nothing to hold it to.
                if in_repository not in _verifier().GITOPS_SEEDED_FILES:
                    raise CatalogError(
                        f"{where} names {in_repository!r}, which provisioning does not seed "
                        f"(the verifier's GITOPS_SEEDED_FILES: {sorted(_verifier().GITOPS_SEEDED_FILES)})"
                    )
            elif project_scoped or names_reservations(subject):
                # Only the Compute Engine shape: a project-scoped role has no
                # cluster to describe and no kubeconfig to read objects with,
                # and a reservation is read in the project on a cluster role too.
                if not isinstance(subject, str) or not PROJECT_SUBJECT_RE.fullmatch(subject):
                    raise CatalogError(f"{where} declares a malformed project subject {subject!r}")
            elif subject != CLUSTER_SUBJECT and (
                not isinstance(subject, str) or not SUBJECT_RE.fullmatch(subject)
            ):
                raise CatalogError(f"{where} declares a malformed subject {subject!r}")
            op = entry.get("op")
            if op not in OPS:
                raise CatalogError(f"{where} names unknown op {op!r} (known: {sorted(OPS)})")
            if (op in REPOSITORY_ONLY_OPS) != (in_repository is not None):
                raise CatalogError(f"{where}: a `{REPOSITORY_SUBJECT}/<path>` subject takes op {OP_AS_SEEDED!r} and nothing else does")
            if op in CLUSTER_ONLY_OPS and subject != CLUSTER_SUBJECT:
                raise CatalogError(f"{where}: op {op!r} applies to the cluster subject only")
            if op in NODE_SELECTOR_ONLY_OPS and not str(subject).startswith(f"{NODE_KIND}?"):
                raise CatalogError(f"{where}: op {op!r} applies to a `{NODE_KIND}?<selector>` subject only")
            if op in INSTANCE_SUBJECT_ONLY_OPS and not (
                project_scoped and str(subject).startswith(f"{INSTANCE_KIND}/")
            ):
                raise CatalogError(f"{where}: op {op!r} applies to a project-scoped `{INSTANCE_KIND}/<prefix>*` subject only")
            if op in SNAPSHOT_SUBJECT_ONLY_OPS and not (
                project_scoped and str(subject).startswith(f"{SNAPSHOT_KIND}/")
            ):
                raise CatalogError(f"{where}: op {op!r} applies to a project-scoped `{SNAPSHOT_KIND}/<prefix>*` subject only")
            if op in OPS_WITH_STRING_VALUE and not (isinstance(entry.get("value"), str) and entry["value"]):
                raise CatalogError(f"{where}: op {op!r} takes a non-empty string")
            if (op in OPS_WITH_VALUE) != ("value" in entry):
                raise CatalogError(
                    f"{where}: op {op!r} {'takes' if op in OPS_WITH_VALUE else 'takes no'} value"
                )
            numeric = _number(entry.get("value")) if op in OPS_WITH_NUMERIC_VALUE else 0.0
            if numeric is None or not math.isfinite(numeric):
                raise CatalogError(f"{where}: op {op!r} takes a number")
            path = entry.get("path")
            if path is None:
                if op not in OPS_WITHOUT_PATH:
                    raise CatalogError(f"{where}: op {op!r} needs a path")
                steps = None
            elif op in NODE_SELECTOR_ONLY_OPS or op in INSTANCE_SUBJECT_ONLY_OPS or op in REPOSITORY_ONLY_OPS or op in SNAPSHOT_SUBJECT_ONLY_OPS:
                raise CatalogError(f"{where}: op {op!r} reads the objects whole and takes no path")
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
    except OSError as exc:
        # Not executable, the wrong binary format, a path through a file: the
        # read failed before the tool ran, and the text says how.
        return STATUS_CANNOT_EXECUTE, "", str(exc)
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
    return _objects(kind, out)


def read_pods_all_namespaces(kubeconfig: Path) -> list[dict]:
    """Every pod in the cluster, the read behind `node_cpu_spare_ge`: a node's
    requested CPU is the sum over the pods bound to it in any namespace."""
    cmd = ["kubectl", f"--kubeconfig={kubeconfig}", f"--request-timeout={KUBECTL_REQUEST_TIMEOUT}", "get", POD_KIND, ALL_NAMESPACES_FLAG, "-o", "json"]
    rc, out, err = _run(cmd, KUBECTL_TIMEOUT_SECONDS)
    if rc != 0:
        raise Unreadable(f"kubectl get {POD_KIND} {ALL_NAMESPACES_FLAG} failed ({rc}): {err.strip().splitlines()[-1] if err.strip() else 'no output'}")
    return _objects(POD_KIND, out)


def _objects(kind: str, out: str) -> list[dict]:
    """The object or the items of a list kubectl printed as JSON."""
    try:
        doc = json.loads(out)
    except ValueError as exc:
        raise Unreadable(f"kubectl get {kind} printed something other than JSON: {exc}") from exc
    if isinstance(doc, dict) and isinstance(doc.get("items"), list):
        return [item for item in doc["items"] if isinstance(item, dict)]
    if isinstance(doc, dict):
        return [doc]
    raise Unreadable(f"kubectl get {kind} printed an unexpected document")


def _partial_list_reasons(err: str) -> str:
    """Why gcloud read a list only in part: the items under its warning, one for
    each part that failed. The last line of stderr is not the reason: after an
    answer with no item, gcloud ends with a warning about the filter's key."""
    lines = err.splitlines()
    start = next(i for i, line in enumerate(lines) if PARTIAL_LIST_WARNING in line)
    items = [
        line.strip()[len(PARTIAL_LIST_ITEM_START):].strip()
        for line in lines[start + 1 :]
        if line.strip().startswith(PARTIAL_LIST_ITEM_START)
    ]
    return "; ".join(items) or PARTIAL_LIST_NO_ITEM


def _compute_list(project: str, collection: str, name_filter: str) -> list[dict]:
    """`gcloud compute <collection> list` in `project`, filtered on the name.
    None there is an observation (an empty list), which an assertion may fail
    on; a failed list is Unreadable. The filter's name part comes from the
    catalog's grammar or from a validated object name, never raw."""
    cmd = [
        "gcloud", "compute", collection, "list", "--project", project,
        f"--filter=name ~ {name_filter}", "--format", "json",
    ]
    rc, out, err = _run(cmd, GCLOUD_TIMEOUT_SECONDS)
    if rc != 0:
        raise Unreadable(f"compute {collection} list failed ({rc}): {err.strip().splitlines()[-1] if err.strip() else 'no output'}")
    if PARTIAL_LIST_WARNING in err:
        # An empty or short list here is not an observation: an `absent` or
        # `none_eq` assertion would pass on what gcloud could not read.
        raise Unreadable(f"compute {collection} list was partial: {_partial_list_reasons(err)}")
    try:
        doc = json.loads(out)
    except ValueError as exc:
        raise Unreadable(f"compute {collection} list printed something other than JSON: {exc}") from exc
    if not isinstance(doc, list):
        raise Unreadable(f"compute {collection} list printed an unexpected document")
    return [item for item in doc if isinstance(item, dict)]


def read_compute(project: str, subject: str) -> list[dict]:
    """The Compute Engine objects a project-scoped `<kind>/<name-prefix>*`
    subject names: every object of the kind in `project` whose name starts
    with the prefix."""
    match = PROJECT_SUBJECT_RE.fullmatch(subject)
    if not match:
        raise Unreadable(f"{subject!r} is not a project subject")
    collection = PROJECT_COLLECTIONS[subject.split("/", 1)[0]]
    return _compute_list(project, collection, f"^{match.group('prefix')}")


def read_disks_named(project: str, name: str) -> list[dict]:
    """The project's disks named exactly `name`, in any zone, as the
    orphaned-snapshots check indexes live disks by name. The name comes from a
    snapshot's `sourceDisk`; one outside the GCE shape is refused rather than
    put on a command line."""
    if not DISK_NAME_RE.fullmatch(name):
        raise Unreadable(f"source disk {name!r} is not a name this script reads")
    return _compute_list(project, DISKS_COLLECTION, f"^{name}$")


def read_serial(project: str, name: str, zone: str) -> str:
    """One instance's serial port 1 output. The name and zone come from the
    instance list; anything outside their shapes is refused rather than put on
    a command line. A failed or empty read is Unreadable."""
    if not INSTANCE_NAME_RE.fullmatch(name) or not ZONE_RE.fullmatch(zone):
        raise Unreadable(f"instance {name!r} in zone {zone!r} is not a name this script reads")
    cmd = [
        "gcloud", "compute", "instances", "get-serial-port-output", name,
        f"--zone={zone}", f"--port={SERIAL_PORT}", f"--project={project}",
    ]
    rc, out, err = _run(cmd, GCLOUD_TIMEOUT_SECONDS)
    if rc != 0:
        raise Unreadable(f"get-serial-port-output {name} failed ({rc}): {err.strip().splitlines()[-1] if err.strip() else 'no output'}")
    if not out.strip():
        raise Unreadable(f"get-serial-port-output {name} returned no output")
    return out


def read_repository(project: str, path: str) -> list[dict]:
    """The file a `repository/<path>` subject names in `project`'s GitOps
    repository, as `[{"sha": <blob sha>}]`; `[]` when the repository answered
    and the path is not there (an observation the assertion fails on). The
    verifier does the read and tells a missing file from a repository this
    credential cannot see; with no credential at all the read is not made
    (Unasserted); anything else it could not settle is Unreadable."""
    verifier = _verifier()
    state, detail = verifier.gitops_seeded_file_state(project, path)
    if state == verifier.GITHUB_PATH_PRESENT:
        return [{"sha": detail}]
    if state == verifier.GITHUB_PATH_ABSENT:
        return []
    if state == verifier.GITHUB_PATH_NO_CREDENTIAL:
        raise Unasserted(detail)
    raise Unreadable(f"{path} in the pool repository: {detail}")


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


def read_channel(project: str, location: str, channel: str) -> dict | None:
    """`channel`'s entry in get-server-config at `location` (its
    `defaultVersion` and `validVersions`), or None if not listed."""
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
            return entry
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
    a string status without caring which type the API chose, and compares an
    object or a list structurally, so the catalog's key order does not matter."""
    if isinstance(observed, bool) or isinstance(expected, bool):
        return str(observed).lower() == str(expected).lower()
    if isinstance(observed, (int, float)) and isinstance(expected, (int, float)):
        return float(observed) == float(expected)
    if isinstance(observed, (dict, list)) or isinstance(expected, (dict, list)):
        return observed == expected
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


def _gke_version(version) -> tuple[int, ...] | None:
    """`1.34.11-gke.1209000` -> (1, 34, 11, 1209000), so patches order
    numerically; None for anything else."""
    m = GKE_VERSION_RE.match(str(version or ""))
    return tuple(int(n) for n in m.groups()) if m else None


def _timestamp(value) -> datetime | None:
    """An RFC 3339 timestamp as an aware datetime (UTC when it names no
    offset); None for anything else."""
    try:
        when = datetime.fromisoformat(str(value).replace(RFC3339_UTC_SUFFIX, ISO_UTC_OFFSET))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _millicores(quantity) -> float:
    """A Kubernetes CPU quantity in millicores; 0 for anything that is not one."""
    text = str(quantity if quantity is not None else "").strip()
    if not text:
        return 0.0
    # float() admits "inf", "NaN" and an overflowing exponent, none of which
    # the API server serialises; they count as nothing rather than taking the
    # pass down when the difference is converted to an integer.
    for suffix, factor in CPU_SUFFIX_MILLICORES.items():
        if text.endswith(suffix):
            n = _number(text[: -len(suffix)])
            return n * factor if n is not None and math.isfinite(n) else 0.0
    n = _number(text)
    return n * MILLICORES_PER_CORE if n is not None and math.isfinite(n) else 0.0


def _pod_cpu_request(pod: dict) -> float:
    """What the scheduler reserves for a pod: the larger of its containers' sum
    (sidecars included) and the biggest ordinary init container (plus the
    sidecars started before it), plus the pod's overhead."""
    spec = pod.get("spec") or {}

    def request(container) -> float:
        resources = container.get("resources") if isinstance(container, dict) else None
        return _millicores(((resources or {}).get("requests") or {}).get("cpu"))

    sidecars = 0.0
    init_peak = 0.0
    for init in spec.get("initContainers") or []:
        if isinstance(init, dict) and init.get("restartPolicy") == INIT_RESTART_SIDECAR:
            sidecars += request(init)
        else:
            init_peak = max(init_peak, request(init) + sidecars)
    containers = sum(request(c) for c in spec.get("containers") or []) + sidecars
    overhead = _millicores((spec.get("overhead") or {}).get("cpu"))
    return max(containers, init_peak) + overhead


def _spare_by_node(nodes: list, pods_by_node: dict) -> dict[str, int]:
    """Per matched node, allocatable CPU minus what the pods bound to it request, in millicores, by node name."""
    spare: dict[str, int] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = str((node.get("metadata") or {}).get("name") or "")
        allocatable_cpu = ((node.get("status") or {}).get("allocatable") or {}).get("cpu")
        if allocatable_cpu is None:
            raise Unreadable(f"node {name or '(unnamed)'} reports no allocatable CPU yet; still registering")
        allocatable = _millicores(allocatable_cpu)
        requested = sum(
            _pod_cpu_request(pod)
            for pod in pods_by_node.get(name, [])
            if isinstance(pod, dict) and (pod.get("status") or {}).get("phase") not in POD_PHASES_RELEASED
        )
        spare[name] = int(round(allocatable - requested))
    return dict(sorted(spare.items()))


def _describe(values: list) -> str:
    if not values:
        return "observed nothing"
    shown = json.dumps(values if len(values) > 1 else values[0], default=str)
    return f"observed {shown}"


def evaluate(entry: dict, objects: list, *, channel_default=None, channel_valid=None, pods_by_node=None, serial_for=None, seeded_for=None, disks_named=None) -> str | None:
    """None when the assertion holds; otherwise one line saying what was seen.
    A `ready_after_hours` assertion that fails on its clock alone raises
    NotReady with that line instead.

    `channel_default` is a callable returning the channel default version for
    the cluster document, used by the minor-behind operator only, and
    `channel_valid` one returning the channel's valid versions (None when the
    channel is not listed), used by `not_ahead_of_channel` only; both are
    resolved lazily so a role that never needs server-config never pays for it.
    `pods_by_node` is a callable returning {node name: [pod, ...]} for every
    bound pod in the cluster, used by `node_cpu_spare_ge` only, lazy for the
    same reason. `serial_for` is a callable returning one listed instance's
    serial port 1 output, used by `serial_contains` only. `seeded_for` is a
    callable returning (the blob sha provisioning seeds at a repository path,
    a callable from the live file's sha to the repair to name), used by
    `as_seeded` only.
    `disks_named` is a callable returning the project's disks of one name,
    used by `source_disk_gone` only.
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
    elif op == OP_AGE_HOURS_GE:
        when = _timestamp(values[0]) if len(values) == 1 else None
        if when is None:
            return f"{label}: {_describe(values)}, not one RFC 3339 timestamp"
        hours = (datetime.now(timezone.utc) - when).total_seconds() / SECONDS_PER_HOUR
        if hours < float(expected):
            return f"{label}: {_describe(values)}, {hours:.1f} hours old"
        ok = True
    elif op == OP_READY_AFTER_HOURS:
        when = _timestamp(values[0]) if len(values) == 1 else None
        if when is None:
            return f"{label}: {_describe(values)}, not one RFC 3339 timestamp"
        hours = (datetime.now(timezone.utc) - when).total_seconds() / SECONDS_PER_HOUR
        if hours < float(expected):
            ready = (when + timedelta(hours=float(expected))).astimezone(timezone.utc)
            raise NotReady(f"{label}: {_describe(values)}, {hours:.1f} hours old; {READY_AT} {ready.isoformat()}")
        ok = True
    elif op == OP_SOURCE_DISK_GONE:
        if disks_named is None:  # pragma: no cover - check_role always supplies it
            raise CatalogError(f"op {op!r} needs the disk reader")
        if not objects:
            return f"{label}: no snapshot matched"
        live, failed = [], []
        for obj in objects:
            source = str(obj.get(SNAPSHOT_SOURCE_FIELD) or "").rstrip("/").rsplit("/", 1)[-1]
            if not source:
                return f"{label}: {obj.get('name') or 'a snapshot'} carries no {SNAPSHOT_SOURCE_FIELD}"
            try:
                if disks_named(source):
                    live.append(source)
            except Unreadable as exc:
                failed.append(str(exc))
        if live:
            # Positive evidence wins over a read that failed elsewhere.
            return f"{label}: {SNAPSHOT_SOURCE_FIELD} {', '.join(live)} is a live disk"
        if failed:
            raise Unreadable("; ".join(failed))
        return None
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
    elif op == OP_NOT_AHEAD:
        channel = walk(parse_path(RELEASE_CHANNEL_PATH), objects)
        name = str(channel[0]) if len(channel) == 1 else None
        if name is None:
            return f"{label}: {_describe(values)}; release channel unset"
        valid = channel_valid(name) if channel_valid else None
        if valid is None:
            return f"{label}: {_describe(values)}; get-server-config lists no {name} channel"
        ok = False
        if len(values) == 1:
            version = _gke_version(values[0])
            if version is None:
                return f"{label}: {_describe(values)}, not a GKE version"
            # Any entry outside the GKE shape refuses the roster, as
            # seeded_b_pin.py does: ordering against the rest could drop the
            # newest patch and read the master as ahead.
            offered = []
            for v in valid:
                key = _gke_version(v)
                if key is None:
                    raise Unreadable(f"{name}'s {SERVER_CONFIG_VALID} entry {v!r} is not a GKE version, so nothing can be ordered against the roster")
                offered.append((key, str(v)))
            same_minor = [(key, v) for key, v in offered if key[:MINOR_PREFIX_LEN] == version[:MINOR_PREFIX_LEN]]
            ok = str(values[0]) in valid or any(key > version for key, _ in same_minor or offered)
            if not ok:
                line = ".".join(str(n) for n in version[:MINOR_PREFIX_LEN])
                where = f"every {line} patch {name} offers (newest {max(same_minor)[1]})" if same_minor else f"every version {name} offers"
                return f"{label}: {_describe(values)}, newer than {where}"
    elif op == OP_NODE_CPU_SPARE:
        if pods_by_node is None:  # pragma: no cover - check_role always supplies it
            raise CatalogError(f"op {op!r} needs the pod reader")
        want = _number(expected)
        spare = _spare_by_node(objects, pods_by_node()) if objects else {}
        ok = bool(spare) and want is not None and all(v >= want for v in spare.values())
        return None if ok else f"{label}: {_describe([spare] if spare else [])}"
    elif op == OP_SERIAL_CONTAINS:
        if serial_for is None:  # pragma: no cover - check_role always supplies it
            raise CatalogError(f"op {op!r} needs the serial reader")
        if not objects:
            return f"{label}: no instance matched"
        searched, failed = [], []
        for obj in objects:
            name = str(obj.get("name") or "")
            zone = str(obj.get("zone") or "").rstrip("/").rsplit("/", 1)[-1]
            try:
                text = serial_for(name, zone)
            except Unreadable as exc:
                failed.append(str(exc))
                continue
            if expected in text:
                return None
            searched.append(f"{name} ({zone}, {len(text)} characters)")
        if failed:
            # Only positive evidence is drift: a console nobody read may hold it.
            raise Unreadable("; ".join(failed))
        return f"{label}: not in the serial port {SERIAL_PORT} output of {', '.join(searched)}"
    elif op == OP_AS_SEEDED:
        if seeded_for is None:  # pragma: no cover - check_role always supplies it
            raise CatalogError(f"op {op!r} needs the seeded-file reader")
        seeded, repair_for = seeded_for(repository_path(entry["subject"]))
        observed = [str(obj.get("sha") or "") for obj in objects if isinstance(obj, dict)]
        if len(observed) == 1 and observed[0] == seeded:
            return None
        if observed:
            seen = f"observed blob {observed[0][:BLOB_SHA_SHORT]}, seeded blob {seeded[:BLOB_SHA_SHORT]}"
        else:
            seen = _describe([])
        return f"{label}: {seen}; {repair_for(observed[0] if observed else '')}"
    else:  # pragma: no cover - load_assertions rejects unknown ops
        raise CatalogError(f"unknown op {op!r}")
    return None if ok else f"{label}: {_describe(values)}"


# --- The pass ------------------------------------------------------------------


class Reader:
    """Reads for one pass, with the server-config lookups memoised.

    A pass over every catalog role must not pay one get-server-config per assertion;
    a location's channel versions do not change between two reads seconds
    apart.
    """

    def __init__(self, directory: Path, project: str, context: dict[str, str]):
        self.directory = directory
        self.project = project
        self.context = context
        # A failed read is kept too, so the pass pays one get-server-config
        # per location and channel whichever way it went.
        self._channels: dict[tuple[str, str], dict | None | Unreadable] = {}
        self._pods: dict[Path, dict[str, list]] = {}
        self._serial: dict[tuple[str, str], str | Unreadable] = {}
        # One contents read per seeded path per pass, whichever way it went:
        # the declared-* roles share the note.
        self._repository: dict[str, list | Unreadable | Unasserted] = {}
        self._disks: dict[str, list | Unreadable] = {}

    def objects_for(self, role: str, spec: dict, subject: str) -> list:
        kubeconfig = self.directory / f"{role}{KUBECONFIG_SUFFIX}"
        path = repository_path(subject)
        if path is not None:
            if path not in self._repository:
                try:
                    self._repository[path] = read_repository(self.project, path)
                except (Unreadable, Unasserted) as exc:
                    self._repository[path] = exc
            cached = self._repository[path]
            if isinstance(cached, (Unreadable, Unasserted)):
                raise cached
            return cached
        if spec["cluster_slot"] is None or names_reservations(subject):
            return read_compute(self.project, subject)
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

    def _channel(self, spec: dict, channel: str) -> dict | None:
        slot = spec["cluster_slot"]
        location = self.context.get(f"{CONTEXT_LOCATION_PREFIX}{slot}")
        if not location:
            raise Unreadable(f"{CONTEXT_FILE} records no location for slot {slot!r}")
        key = (location, channel)
        if key not in self._channels:
            try:
                self._channels[key] = read_channel(self.project, location, channel)
            except Unreadable as exc:
                self._channels[key] = exc
        cached = self._channels[key]
        if isinstance(cached, Unreadable):
            raise cached
        return cached

    def channel_default_for(self, spec: dict):
        def lookup(channel: str) -> str | None:
            default = (self._channel(spec, channel) or {}).get(SERVER_CONFIG_DEFAULT)
            return str(default) if default else None

        return lookup

    def channel_valid_for(self, spec: dict):
        def lookup(channel: str) -> list[str] | None:
            entry = self._channel(spec, channel)
            if entry is None:
                return None
            valid = entry.get(SERVER_CONFIG_VALID)
            # An empty roster is a failed read, as bench/tf/fleet/seeded_b_pin.py
            # treats it: every master would otherwise read as ahead of it.
            if not isinstance(valid, list) or not valid:
                raise Unreadable(f"get-server-config's {channel} entry carries no {SERVER_CONFIG_VALID} list")
            return [str(v) for v in valid]

        return lookup

    def serial_for(self, name: str, zone: str) -> str:
        """One instance's serial port 1 output, read once per pass."""
        key = (name, zone)
        if key not in self._serial:
            try:
                self._serial[key] = read_serial(self.project, name, zone)
            except Unreadable as exc:
                self._serial[key] = exc
        cached = self._serial[key]
        if isinstance(cached, Unreadable):
            raise cached
        return cached

    def seeded_for(self, path: str):
        """(the blob sha provisioning seeds at `path`, a callable giving the
        repair to name for this project once the live file's sha is known)."""
        verifier = _verifier()

        def repair(sha: str) -> str:
            return verifier.gitops_seeded_file_repair(self.project, path, sha)

        return verifier.gitops_seeded_blob_sha(path), repair

    def disks_named(self, name: str) -> list:
        """The project's disks of one name, read once per pass."""
        if name not in self._disks:
            try:
                self._disks[name] = read_disks_named(self.project, name)
            except Unreadable as exc:
                self._disks[name] = exc
        cached = self._disks[name]
        if isinstance(cached, Unreadable):
            raise cached
        return cached

    def pods_by_node_for(self, role: str):
        """The cluster's bound pods grouped by node, read once per role per pass."""
        kubeconfig = self.directory / f"{role}{KUBECONFIG_SUFFIX}"

        def lookup() -> dict[str, list]:
            if kubeconfig not in self._pods:
                grouped: dict[str, list] = {}
                for pod in read_pods_all_namespaces(kubeconfig):
                    node = (pod.get("spec") or {}).get("nodeName")
                    if node:
                        grouped.setdefault(str(node), []).append(pod)
                self._pods[kubeconfig] = grouped
            return self._pods[kubeconfig]

        return lookup


def check_role(reader: Reader, role: str, spec: dict) -> tuple[list[str], list[str], list[tuple[str, str]], bool, list[str]]:
    """(drift lines, unreadable lines, unasserted (subject, reason) pairs,
    worth waiting for, not-ready lines) for one role in one pass.

    Objects are read once per distinct subject, so two assertions on the same
    pod list cost one kubectl. An assertion whose subject could not be read
    lands in the second list; the caller decides what that means once it
    knows whether anything else in the role was positively out of shape. One
    the pass had nothing to read with (Unasserted) lands in the third, and
    says nothing about the role's verdict. The flag is true when some failed
    assertion could converge within a wait, which an age gate, a rotated
    serial console and a seeded file cannot (`OPS_A_WAIT_CANNOT_CLEAR`). A
    `ready_after_hours` assertion that failed on its clock lands in the last
    list, apart from drift.
    """
    drift: list[str] = []
    unreadable: list[str] = []
    unasserted: list[tuple[str, str]] = []
    not_ready: list[str] = []
    settling = False
    cache: dict[str, list] = {}
    for entry in spec["state"]:
        subject = entry["subject"]
        if subject not in cache:
            try:
                cache[subject] = reader.objects_for(role, spec, subject)
            except (Unreadable, Unasserted) as exc:
                cache[subject] = exc
        objects = cache[subject]
        if isinstance(objects, Unasserted):
            unasserted.append((subject, str(objects)))
            continue
        if isinstance(objects, Unreadable):
            unreadable.append(f"{subject}: {objects}")
            continue
        try:
            problem = evaluate(
                entry,
                objects,
                channel_default=reader.channel_default_for(spec),
                channel_valid=reader.channel_valid_for(spec),
                pods_by_node=reader.pods_by_node_for(role),
                serial_for=reader.serial_for,
                seeded_for=reader.seeded_for,
                disks_named=reader.disks_named,
            )
        except Unreadable as exc:
            unreadable.append(f"{subject}: {exc}")
            continue
        except NotReady as exc:
            not_ready.append(str(exc) + (f" (why: {entry['why']})" if entry["why"] else ""))
            continue
        if problem:
            drift.append(problem + (f" (why: {entry['why']})" if entry["why"] else ""))
            settling = settling or entry["op"] not in OPS_A_WAIT_CANNOT_CLEAR
    return drift, unreadable, unasserted, settling, not_ready


def published(directory: Path, role: str, spec: dict) -> bool:
    """Did the runner publish the role? A cluster role by its kubeconfig; a
    project-scoped role, which has none, by its `.confirmed` list."""
    suffix = CONFIRMED_SUFFIX if spec["cluster_slot"] is None else KUBECONFIG_SUFFIX
    return (directory / f"{role}{suffix}").is_file()


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
        if spec["state"] and published(directory, role, spec)
    }
    converged: list[str] = []
    last: dict[str, tuple[list[str], list[str], list[str]]] = {}
    settling: dict[str, bool] = {}
    # {subject: (reason, roles)} for the assertions this pass had nothing to
    # read with, kept out of every verdict and named once in the report.
    unasserted: dict[str, tuple[str, set[str]]] = {}
    deadline = time.monotonic() + max(wait, 0.0)
    while True:
        reader = Reader(directory, project, context)
        for role in sorted(pending):
            drift, unreadable, left_out, settling[role], not_ready = check_role(reader, role, pending[role])
            last[role] = (drift, unreadable, not_ready)
            for subject, reason in left_out:
                unasserted.setdefault(subject, (reason, set()))[1].add(role)
            if not drift and not unreadable and not not_ready:
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
        # An age gate is the same exception: a cluster under a day old is still
        # under it when the wait runs out. So is a rotated serial console: only
        # a reboot puts the line back. A role that is not ready is never
        # settling: its clock is not drift, and no wait this short moves it.
        waiting = [role for role in pending if settling[role]]
        if not waiting or remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    drifted = 0
    unchecked = 0
    not_yet_ready = 0
    verdicts: dict[str, tuple[str, list[str]]] = {role: (VERDICT_CONVERGED, []) for role in converged}
    for role in sorted(pending):
        drift, unreadable, not_ready = last[role]
        # Positive evidence wins: an assertion that was READ and failed is drift
        # whatever else in the role went unread or unaged. Only a role whose
        # every failure was a failed read is "not checked", and it gets no
        # drift file -- a fixture nobody saw must not be reported as out of
        # shape. A role whose only failure is its clock is "not ready": no
        # drift file either, and a NOTE rather than a WARNING, because nothing
        # is wrong with it and no re-apply is owed.
        if drift:
            drifted += 1
            lines = drift + [f"not ready: {n}" for n in not_ready] + [f"unread: {u}" for u in unreadable]
            path = directory / f"{role}{DRIFT_SUFFIX}"
            path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
            os.chmod(path, 0o600)
            verdicts[role] = (VERDICT_DRIFTED, lines)
            _warn(
                f"fixture role '{role}' is present but not in its designed state in "
                f"{project}; the cases that depend on it cannot be graded against it: "
                + "; ".join(lines)
            )
        elif unreadable:
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
        else:
            not_yet_ready += 1
            verdicts[role] = (VERDICT_NOT_READY, list(not_ready))
            try:
                (directory / f"{role}{DRIFT_SUFFIX}").unlink()
            except FileNotFoundError:
                pass
            _note(
                f"fixture role '{role}' is planted in {project} but not yet ready; the cases "
                f"that depend on it wait for its clock: " + "; ".join(not_ready)
            )
    left_out = [
        {"subject": subject, "roles": sorted(role_set), "reason": reason}
        for subject, (reason, role_set) in sorted(unasserted.items())
    ]
    for entry in left_out:
        _warn(UNASSERTED_WARNING.format(subject=entry["subject"], project=project, reason=entry["reason"], roles=", ".join(entry["roles"])))
    print(
        SUMMARY_FORMAT.format(
            converged=len(converged), drifted=drifted, unchecked=unchecked, not_ready=not_yet_ready, project=project
        ),
        file=sys.stderr,
    )
    if report is not None:
        write_report(report, project, roles, verdicts, left_out, converged=len(converged), drifted=drifted, unchecked=unchecked, not_ready=not_yet_ready)
    return EXIT_OK


def write_report(path: Path, project: str, roles: dict[str, dict], verdicts: dict[str, tuple[str, list[str]]], unasserted: list[dict], **counts: int) -> None:
    """Every catalog role's verdict as JSON, for a caller that runs this
    script per project and must not parse its warnings. A role the runner
    published no kubeconfig for is `unpublished` (the runner already said
    why on its own stderr); one with no `state` list is `no_state`.
    `unasserted` lists the subjects the pass had nothing to read with, each
    with the roles it sits on and the reason, outside every verdict."""
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
        "unasserted": unasserted,
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
