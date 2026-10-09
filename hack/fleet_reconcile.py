#!/usr/bin/env python3
"""Re-apply the seeded fleet stack in pool projects, from outside any run.

bench/tf/fleet plants the defects the presubmit's fixture scenarios assert on,
and the fleet drifts: GKE auto-upgrade heals seeded-b's version lag once its
maintenance exclusion lapses, a node repair leaves a fixture Pending, a cleanup
deletes the orphan disk. The stack's own README says the reconcile is a
re-apply on a schedule; this is that schedule's script.

Runs from Prow, only ever from `main`, as an identity of its own
(docs/ci-pool-projects.md, section 6.2): a postsubmit on every merge that
touches the stack, and a daily pass over every project, which is also what
rolls seeded-b's exclusion forward. A presubmit runs the pull request's own
scripts, so a credential that can rewrite the fleet is never mounted there.

Each project is acquired from Boskos out of `free` into `reconciling` for the
minutes the apply takes and released back, so a project a run holds is never
applied under it and a run arriving mid-apply waits at its own acquire. `--all`
asks for every mapped project by name and keeps asking for the busy ones until
the run's budget is spent; `--workers` holds several at once, each under its
own lease. The plan is inspected before it is applied: creates and in-place
updates are applied; a delete or a replace only when bench/tf/fleet's
allowlist declares that address (reconcile-allow.json, reviewed with the
change that needs it); anything else is refused and named. A plan whose only
change is seeded-b's exclusion re-stamp is `converged`, decided on the fields
that changed, not the address.

A run never starts a project it cannot finish inside its budget, and stops
when main's fleet tree is no longer the one it checked out, so two runs applying
different stacks never apply over each other. `--report` writes
fleet-reconcile.json (mode, commit, tree, per-project outcomes with times, a
summary), under $ARTIFACTS when Prow sets it, for the CI health bot
(scripts/eval_dashboard/periodics.py); each applied project also gets
`applied.json` in its state bucket, the record of which commit it is at.
"""

import argparse
import atexit
import collections
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import boskos_pool  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from eval_dashboard import fixture_state  # noqa: E402

FLEET_DIR = REPO_ROOT / "bench" / "tf" / "fleet"
# The convention every apply of the stack has used (bench/tf/fleet/README.md,
# "State and reconcile"; scripts/provision_ci_pool_project.sh step 2.2).
STATE_BUCKET_TEMPLATE = "{project}-tf-state"
STATE_PREFIX = "seeded-fleet"
TOFU = "tofu"
PLAN_FILE = "reconcile.tfplan"
# Non-interactive, and tofu's own retries rather than a prompt on a held lock.
TOFU_ENV = {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
# `init` records the backend it was given in the data dir and `plan` and
# `apply` read it back, so workers sharing bench/tf/fleet/.terraform would
# bind one project's plan to another's state: one data dir per project, in
# its temp dir. The providers come from one cache per run, which only one
# `init` at a time may write (the cache is not safe under concurrent inits).
TOFU_DATA_DIR = ".terraform"
_INIT_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_PLUGIN_CACHE = []
# `plan -detailed-exitcode`: 0 nothing to do, 2 changes planned, 1 an error.
PLAN_NO_CHANGES = 0
PLAN_HAS_CHANGES = 2
# The actions `tofu show -json` reports per resource. A create and an in-place
# update are applied; a delete or a replace (delete paired with a create,
# either order) only on an address the allowlist below declares; a forget, or
# any action a later tofu adds, is refused.
ACTION_NOOP = "no-op"
ACTION_READ = "read"
ACTION_CREATE = "create"
ACTION_UPDATE = "update"
IGNORED_ACTIONS = ([ACTION_NOOP], [ACTION_READ])
APPLIED_ACTIONS = ([ACTION_CREATE], [ACTION_UPDATE])
ACTION_DELETE = "delete"
REPLACE_ACTIONS = ([ACTION_DELETE, ACTION_CREATE], [ACTION_CREATE, ACTION_DELETE])
# What a re-apply may delete or replace is declared beside the stack, one
# entry per address with its reason, reviewed with the change that needs it
# (bench/tf/fleet/README.md, "State and reconcile"). A `standing` entry is
# permanent (the no-surge pool's replace on a minor roll); any other is
# reported unused once the plan no longer needs it, so a revert's entry is
# removed by its follow-up.
ALLOWLIST_FILE = FLEET_DIR / "reconcile-allow.json"
ALLOW_KEY_ADDRESS = "address"
ALLOW_KEY_WHY = "why"
ALLOW_KEY_STANDING = "standing"
ALLOW_KEYS = frozenset({ALLOW_KEY_ADDRESS, ALLOW_KEY_WHY, ALLOW_KEY_STANDING})
# A resource address as `tofu show -json` prints it: optional module path,
# type, name, optional index. Anything else can never match a plan.
# The index is the two shapes tofu prints, a count or a quoted key: an
# approximation here admits an address no plan can ever match.
ALLOW_INDEX_RE = r'\[(0|[1-9][0-9]*|"(?:[^"\\]|\\.)*")\]'
# The index tofu prints on an instance of a count or for_each resource.
_INSTANCE_INDEX_RE = re.compile(ALLOW_INDEX_RE + "$")
ALLOW_ADDRESS_RE = re.compile(r"^(module\.[A-Za-z0-9_-]+(%s)?\.)*[a-z][a-z0-9_]*\.[A-Za-z_][A-Za-z0-9_-]*(%s)?$" % (ALLOW_INDEX_RE, ALLOW_INDEX_RE))
AllowEntry = collections.namedtuple("AllowEntry", "address why standing")
# seeded-b's maintenance exclusion is re-stamped from `timestamp()` on every
# plan (bench/tf/fleet/main.tf), so a converged project still plans one
# in-place update. A plan whose only changed fields are those two, on that
# address, is `converged`; list indices are dropped from the paths compared.
RESTAMP_ADDRESS = "google_container_cluster.seeded_b"
RESTAMP_PATHS = frozenset({"maintenance_policy.maintenance_exclusion.start_time", "maintenance_policy.maintenance_exclusion.end_time"})

# Boskos: this script's hold state and owner. A project is held for one apply.
HOLD_STATE = "reconciling"
DEFAULT_OWNER = "fleet-reconcile"
# The longest one project may take, init through apply, as one deadline.
# seeded-b's control plane can be walked a patch forward, which is the slow
# case; nothing else here runs past a few minutes. A run killed mid-hold leaves
# its project in HOLD_STATE, so the stranded reset must outlast this ceiling
# plus the interrupt grace, or a live apply's project would be handed to a
# presubmit; the hold is also heartbeat so the pool's reaper does not take it.
PROJECT_TIMEOUT_SECONDS = 3600
STRANDED_AFTER = "65m"
# On a termination signal or the ceiling, tofu gets SIGINT and this long to
# finish the operation in flight and release its state lock before it is
# killed. Prow's entrypoint sends SIGINT and its own grace period, so the
# job's grace_period must exceed WORKER_DRAIN_SECONDS below plus the releases
# and the report write.
INTERRUPT_GRACE_SECONDS = 120
TERMINATION_SIGNALS = boskos_pool.TERMINATION_SIGNALS
# `--all` asks Boskos for the projects still busy again after this long, and
# re-reads main's fleet tree at most this often, until the budget is spent.
POLL_INTERVAL_SECONDS = 120
# The stray check's acquires stop at the first project it sees twice; the
# bound only matters if Boskos never repeats itself.
STRAY_CHECK_EXTRA_ACQUIRES = 3
MAIN_CHECK_INTERVAL_SECONDS = 60
DEFAULT_WORKERS = 1
# How long the threads get to return their projects after a termination
# interrupted their applies, before the children are killed outright.
WORKER_DRAIN_SECONDS = INTERRUPT_GRACE_SECONDS + 30
WORKER_JOIN_STEP_SECONDS = 0.2
FLEET_SUBDIR = "bench/tf/fleet"
# The files that change what a run applies: the stack and its lock file, and
# the allowlist the inspection reads. README.md and fixtures.json live beside
# them and tofu never reads them, so they do not move the fleet tree: a
# docs-only merge neither stops a running reconcile nor needs one. A new input
# kind (a tfvars file, a templatefile source) goes here, into the postsubmit's
# run_if_changed in oss-test-infra, and into docs/ci-pool-projects.md 6.2.
FLEET_INPUT_SUFFIXES = (".tf", ".hcl")
FLEET_INPUT_NAMES = ("reconcile-allow.json",)
GIT_TIMEOUT_SECONDS = 120
# Prow's identifiers for the build, kept in the report and the markers.
BUILD_ID_ENV = "BUILD_ID"
JOB_NAME_ENV = "JOB_NAME"
# The marker each reconciled project keeps: which commit its fleet is at, from
# which build; in the state bucket the reconciler already writes.
APPLIED_OBJECT_TEMPLATE = "gs://{project}-tf-state/seeded-fleet/applied.json"
APPLIED_SCHEMA_VERSION = 1
# The clock, the pause, and the subprocess entry points, as module attributes
# so a test can stand in for each.
clock = time.monotonic
pause = time.sleep
gcloud_runner = subprocess.run

# Where the CI health bot publishes its scan (docs/ci-health.md, "The
# seeded-fleet scan"); `--drifted` applies the projects it lists.
DEFAULT_FIXTURE_STATE = "gs://kube-agents-dashboards/evals/fixture-state.json"
# The run's report, for the CI health bot (scripts/eval_dashboard/periodics.py
# reads it from the job's artifacts): under Prow, ARTIFACTS is the directory
# the pod utilities upload, so the default lands it there without a flag.
REPORT_FILE = "fleet-reconcile.json"
REPORT_SCHEMA_VERSION = 1
ARTIFACTS_ENV = "ARTIFACTS"
GCS_PREFIX = "gs://"
GCLOUD_TIMEOUT_SECONDS = 60

OUTCOME_APPLIED = "applied"
# The plan's only change was seeded-b's exclusion re-stamp: the project is at
# the tree this run applies, and the re-stamp was applied.
OUTCOME_CONVERGED = "converged"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"
OUTCOME_BUSY = "busy"
OUTCOME_PLANNED = "planned"
# The project a termination landed in: its line names it, because the
# recovery for an apply killed past its grace is against that project's state.
OUTCOME_INTERRUPTED = "interrupted"
# Never started: the budget had less than a ceiling left, main's fleet tree
# moved, or the project stayed busy. The next run takes it; not a failure.
OUTCOME_NOT_REACHED = "not_reached"
OUTCOMES = (OUTCOME_APPLIED, OUTCOME_CONVERGED, OUTCOME_UNCHANGED, OUTCOME_PLANNED, OUTCOME_BUSY, OUTCOME_REFUSED, OUTCOME_FAILED, OUTCOME_INTERRUPTED, OUTCOME_NOT_REACHED)
# The outcomes that red the job: a project whose fleet is not what the stack
# declares and stays that way. Busy is not one; the next run gets it.
FAILING_OUTCOMES = frozenset({OUTCOME_REFUSED, OUTCOME_FAILED})
# The outcomes that mean the project's fleet is at this run's tree, and get
# the applied.json marker.
AT_TREE_OUTCOMES = frozenset({OUTCOME_APPLIED, OUTCOME_CONVERGED, OUTCOME_UNCHANGED})
# A project the run held and planned: everything but the two it never leased.
VISITED_OUTCOMES = frozenset(OUTCOMES) - {OUTCOME_BUSY, OUTCOME_NOT_REACHED}
OUTPUT_TAIL_CHARS = 600
REASON_UNMAPPED = "not a mapped pool project (gitops_repo_for_project in hack/ci-deploy.sh)"
# Boskos's 404 does not say which; a mapped project lands here between its
# mapping row and its Boskos registration, and reads busy until registered.
REASON_BUSY = "not free in Boskos, or not registered there yet"
# The runbook's rule (docs/ci-pool-projects.md 6.2): the next run tells
# whether the state needs force-unlock; the bot keys on that word.
REASON_INTERRUPTED = "terminated (%s) while tofu ran; an apply cut past its grace leaves the state locked, and the next run tells whether it needs force-unlock"
REASON_CEILING = "did not finish within %ds; tofu was interrupted, and killed if it did not stop within %ds, which leaves the state locked; the next run tells whether it needs force-unlock"
# A cut inside init: no state was touched, so no lock word for the bot to key
# on. The queued seconds are the inits of other projects it waited behind
# (they run one at a time), which its ceiling was charged for.
REASON_CEILING_INIT = "did not finish within %ds: its init was cut after %ds queued for the init slot behind other projects' inits; nothing was changed and nothing is locked"
REASON_RUNNER = "could not run tofu (%s: %s)"
REASON_NOT_REACHED_BUDGET = "not started: %ds left in the run's budget, under the %ds per-project ceiling; the next run takes it"
REASON_NOT_REACHED_BUDGET_MARK = "left in the run's budget"
REASON_NOT_REACHED_MOVED = "not started: the fleet stack under bench/tf/fleet on %s is now %s and this run applies %s; the next run takes it"
REASON_NOT_REACHED_MOVED_AWAY = "not started: bench/tf/fleet on %s no longer holds the stack this run applies (%s); the next run takes it"
# Appended to a definite no-stack reading at the first check, and only there: a
# fetch that failed says nothing about the checkout or the ref.
HINT_NO_STACK = "not a kube-agents checkout, or the wrong ref"
# How git's bytes are decoded and the hash input re-encoded: the same encoding
# and error handler on both sides, whatever the process locale, so a path git
# prints that is not UTF-8 survives the round trip and two processes hash one
# tree alike.
GIT_TEXT_ENCODING = "utf-8"
GIT_TEXT_ERRORS = "surrogateescape"
REASON_NOT_REACHED_BUSY = "not free in Boskos before the run's budget ran out; the next run takes it"
REASON_NOT_REACHED_TERMINATED = "not started: the run was terminated; the next run takes it"
REASON_NOT_REACHED_RUN_ERROR = "not started: the run stopped on an error (%s); the next run takes it"
REASON_INTERRUPTED_BEFORE = "terminated (%s) before tofu %s started; nothing was changed and nothing is locked"
REASON_RUN_ERROR = "the run hit an error at this project: %s"
REASON_VISITED_NONE = "visited no project of the %d asked for: every one was busy for the whole budget, is not registered in Boskos under its mapped name, or the budget ran out polling them"
REASON_RESTAMP = "re-stamp of seeded-b's maintenance exclusion only"
WARNING_MARKER = "applied.json not written: %s"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NAMES = {EXIT_OK: "ok", EXIT_FAILED: "failed", boskos_pool.TERMINATED_EXIT_CODE: "terminated"}
EXIT_NAME_ERROR = "error"
MODE_PROJECT = "project"
MODE_DRIFTED = "drifted"
MODE_ALL = "all"
ISO_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ReconcileError(Exception):
    """A fault that stops one project's reconcile. The caller reports it."""


def _is_budget_reason(reason):
    return REASON_NOT_REACHED_BUDGET_MARK in (reason or "")


def visited_count(outcomes, run=None):
    """How many of the projects the run set out to visit it held. A stray
    registration recorded failed is outside that set, and so is a named
    project failed as unmapped before Boskos was asked: neither was held."""
    names = run.mapped_names if run is not None and run.mapped_names is not None else None
    return sum(
        1
        for p, (o, d) in outcomes.items()
        if o in VISITED_OUTCOMES and not (o == OUTCOME_FAILED and d == REASON_UNMAPPED) and (names is None or p in names)
    )


def _plugin_cache():
    """The run's provider cache directory, made on first use and removed at exit."""
    with _CACHE_LOCK:
        if not _PLUGIN_CACHE:
            path = tempfile.mkdtemp(prefix="fleet-reconcile-providers-")
            atexit.register(shutil.rmtree, path, ignore_errors=True)
            _PLUGIN_CACHE.append(path)
        return _PLUGIN_CACHE[0]


def _project_env(tmp):
    """The tofu environment for one project: its own data dir, the run's provider cache."""
    return {"TF_DATA_DIR": os.path.join(tmp, TOFU_DATA_DIR), "TF_PLUGIN_CACHE_DIR": _plugin_cache()}


def tofu_runner(argv, cwd=None, timeout=None, env=None, **_):
    """subprocess.run for tofu, with a graceful stop.

    A killed tofu leaves the state locked and the next run failing on the lock,
    so on a termination signal or the ceiling it gets SIGINT and a grace period
    first. Its own session, so a terminal's Ctrl-C reaches this process alone
    and tofu sees one interrupt, the forwarded one: a second interrupt makes
    tofu exit at once, mid-operation.
    """
    merged = dict(os.environ)
    merged.update(TOFU_ENV)
    merged.update(env or {})
    proc = None
    try:
        # A termination while the child is being started would leave it
        # running with no handle to interrupt or kill; deferred until Popen
        # has returned, it lands below with `proc` in hand.
        boskos_pool._hold_signals(True)
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=merged, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
            )
            # A termination whose forward ran before this registration missed
            # the child; it gets its one interrupt here, and exits on it
            # instead of at the drain's kill.
            if _children_add(proc):
                proc.send_signal(signal.SIGINT)
        finally:
            boskos_pool._hold_signals(False)
        out, err = proc.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, boskos_pool.Terminated) as first:
        if proc is None:
            raise
        # A raised termination holds later ones back (boskos_pool.terminate),
        # so a second signal between the catch and the forward is read at the
        # forward as a "stop now" and skips the grace; the forward and the
        # kill each run deferred as well. A signal after the ceiling in that
        # gap escapes this body, and the finally below kills the child.
        stop_now = _with_terminations_deferred(lambda: proc.send_signal(signal.SIGINT))
        second = None
        if not stop_now:
            try:
                proc.communicate(timeout=INTERRUPT_GRACE_SECONDS)
            except (subprocess.TimeoutExpired, boskos_pool.Terminated) as exc:
                second = exc
        if stop_now or second is not None:
            # The grace ran out, or a signal cut it short: either way tofu is
            # killed before the project is released, never left running
            # detached under a project handed back to the pool.
            landed = _with_terminations_deferred(lambda: (proc.kill(), proc.communicate()))
            if stop_now or landed or isinstance(second, boskos_pool.Terminated):
                # A termination is what propagates, not the ceiling: a bare
                # raise of the ceiling would carry the run into the next
                # project under Prow's kill timer.
                raise second if isinstance(second, boskos_pool.Terminated) else boskos_pool.Terminated("a termination during the interrupt")
        raise first
    finally:
        # Whatever escaped above, the child does not outlive the runner: a
        # project is released after this returns, and tofu must not still be
        # applying in it.
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        _children_discard(proc)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


# The tofu processes alive right now, across workers. Signals reach the main
# thread only, so a termination with workers is forwarded to each child from
# there; a worker's apply then exits on the interrupt and its hold releases.
_CHILDREN = set()
_CHILDREN_LOCK = threading.Lock()
_TERMINATING = threading.Event()


def _children_add(proc):
    """Registers the child; True when the termination's forward has already
    taken its snapshot, so this child missed it and the caller interrupts it.
    The flag is read in the same lock hold as the add, and the forward sets
    it and snapshots in one hold too, so exactly one side signals a child:
    a second interrupt makes tofu exit at once, mid-operation."""
    with _CHILDREN_LOCK:
        _CHILDREN.add(proc)
        return _TERMINATING.is_set()


def _begin_termination():
    """Raises the terminating flag and interrupts every child registered so
    far, flag and snapshot in one lock hold with the registration's check."""
    with _CHILDREN_LOCK:
        _TERMINATING.set()
        procs = list(_CHILDREN)
    _signal(procs)


def _children_discard(proc):
    with _CHILDREN_LOCK:
        _CHILDREN.discard(proc)


def _children_signal(kill=False):
    with _CHILDREN_LOCK:
        procs = list(_CHILDREN)
    _signal(procs, kill)


def _signal(procs, kill=False):
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.kill() if kill else proc.send_signal(signal.SIGINT)
        except OSError:
            pass


def terminating():
    """True once a termination reached the main thread while workers ran."""
    return _TERMINATING.is_set()


def _with_terminations_deferred(fn):
    """Run fn() with termination signals deferred; True if one landed."""
    landed = False
    boskos_pool._hold_signals(True)
    try:
        fn()
    finally:
        try:
            boskos_pool._hold_signals(False)
        except boskos_pool.Terminated:
            landed = True
    return landed


def _tail(text):
    text = (text or "").strip()
    return text[-OUTPUT_TAIL_CHARS:] if text else "no output"


def _step(exc):
    """The tofu verb a subprocess exception names, or None."""
    cmd = getattr(exc, "cmd", None)
    return cmd[1] if isinstance(cmd, (list, tuple)) and len(cmd) > 1 else None


class TerminatedBeforeTofu(boskos_pool.Terminated):
    """A termination landed before this step's tofu was started: no child
    ran, so the project is interrupted with nothing to unlock."""

    def __init__(self, step):
        super().__init__("a termination landed")
        self.step = step


def _tofu(args, runner, deadline, ok=(0,), env=None):
    # A worker between two steps, or just out of its acquire, when the
    # termination landed: the forward reached only the children alive then,
    # so the next child is simply not started.
    if terminating():
        raise TerminatedBeforeTofu(args[0])
    timeout = max(1, deadline - clock())
    result = runner([TOFU] + list(args), cwd=str(FLEET_DIR), env=env, timeout=timeout, capture_output=True, text=True)
    if result.returncode not in ok:
        raise ReconcileError("tofu %s exited %d: %s" % (args[0], result.returncode, _tail(result.stderr or result.stdout)))
    return result


Change = collections.namedtuple("Change", "actions address paths")


def plan_changes(show_json):
    """[Change(actions, address, paths)] from `tofu show -json`, no-ops and
    reads dropped; `paths` are the attribute paths that change, list indices
    dropped, or None when the plan gives no before and after to compare."""
    try:
        document = json.loads(show_json)
    except ValueError as exc:
        raise ReconcileError("tofu show wrote a plan that is not JSON: %s" % exc)
    if not isinstance(document, dict):
        raise ReconcileError("tofu show wrote a plan that is not a JSON object")
    changes = []
    # The raw value here too: a falsy wrong type must not read as "no changes"
    # on a plan that -detailed-exitcode said had some, and neither may an
    # absent key (the emitter omits it when empty): this is only reached for
    # a plan with changes, so a document listing none was not inspected.
    resource_changes = document.get("resource_changes")
    if not isinstance(resource_changes, list) or not resource_changes:
        raise ReconcileError("tofu show lists no resource changes for a plan that has changes; nothing was inspected, so nothing is applied")
    for change in resource_changes:
        # The raw values, not `or {}` / `or []` defaults: a falsy wrong type
        # would otherwise read as "no actions" and the entry would be applied
        # unclassified.
        block = change.get("change") if isinstance(change, dict) else None
        if not isinstance(block, dict):
            raise ReconcileError("tofu show wrote a resource change that is not a JSON object")
        actions = block.get("actions")
        # A non-empty list of strings: the plan format documents actions as
        # one of a fixed set of non-empty arrays, so an empty one is a shape
        # this parser does not know, and nothing unknown reaches apply.
        if not isinstance(actions, list) or not actions or not all(isinstance(action, str) for action in actions):
            raise ReconcileError("tofu show wrote a resource change whose actions are not a non-empty list of strings")
        actions = list(actions)
        if actions not in IGNORED_ACTIONS:
            changes.append(Change(actions, change.get("address") or "?", changed_paths(block)))
    return changes


def changed_paths(block):
    """The attribute paths whose value differs between `before` and `after`,
    plus every path `after_unknown` marks, list indices dropped. None when the
    block carries no before and after: nothing to compare, so nothing to call
    a re-stamp."""
    before, after = block.get("before"), block.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    paths = set()
    _diff_paths(before, after, (), paths)
    _unknown_paths(block.get("after_unknown"), (), paths)
    return frozenset(paths)


def _diff_paths(before, after, prefix, out):
    if isinstance(before, dict) and isinstance(after, dict):
        for key in set(before) | set(after):
            _diff_paths(before.get(key), after.get(key), prefix + (str(key),), out)
    elif isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            b = before[index] if index < len(before) else None
            a = after[index] if index < len(after) else None
            _diff_paths(b, a, prefix, out)
    elif before != after:
        out.add(".".join(prefix) or "?")


def _unknown_paths(unknown, prefix, out):
    if isinstance(unknown, dict):
        for key, value in unknown.items():
            _unknown_paths(value, prefix + (str(key),), out)
    elif isinstance(unknown, list):
        for value in unknown:
            _unknown_paths(value, prefix, out)
    elif unknown is True:
        out.add(".".join(prefix) or "?")


def is_restamp_only(changes):
    """True when every change is the exclusion re-stamp on seeded-b and
    nothing else: in-place, on that address, with known changed fields all
    inside RESTAMP_PATHS."""
    return bool(changes) and all(
        change.actions == [ACTION_UPDATE] and change.address == RESTAMP_ADDRESS and change.paths and change.paths <= RESTAMP_PATHS for change in changes
    )

def _listed(address, allowed):
    """True when the allowlist names the address, or names the resource an
    instance address belongs to: one entry without an index covers every
    instance of a count or for_each resource."""
    if address in allowed:
        return True
    resource = _INSTANCE_INDEX_RE.sub("", address)
    return resource != address and resource in allowed


def _applied(actions, address, allowed):
    if actions in APPLIED_ACTIONS:
        return True
    return (actions in REPLACE_ACTIONS or actions == [ACTION_DELETE]) and _listed(address, allowed)

def refused_changes(changes, allowed=frozenset()):
    """The changes a re-apply must not make: everything but a create, an in-place update, or a delete or replace the allowlist declares."""
    return ["%s %s" % ("+".join(change.actions), change.address) for change in changes if not _applied(change.actions, change.address, allowed)]

def describe(changes, allowed=frozenset()):
    add = sum(1 for c in changes if c.actions == [ACTION_CREATE])
    change = sum(1 for c in changes if c.actions == [ACTION_UPDATE])
    replace = sum(1 for c in changes if c.actions in REPLACE_ACTIONS and _listed(c.address, allowed))
    destroy = sum(1 for c in changes if c.actions == [ACTION_DELETE] and _listed(c.address, allowed))
    refused = sum(1 for c in changes if not _applied(c.actions, c.address, allowed))
    return "%d to add, %d to change, %d to replace, %d to destroy, %d refused" % (add, change, replace, destroy, refused)


def load_allowlist(path):
    """The allowlist file as [AllowEntry]; [] when there is none. A file that
    is not a list of {address, why[, standing]} objects is a fault: nothing is
    applied under an allowlist that was not read whole."""
    try:
        raw = pathlib.Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ReconcileError("could not read %s: %s" % (path, exc))
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise ReconcileError("%s is not JSON: %s" % (path, exc))
    if not isinstance(document, list):
        raise ReconcileError("%s must be a JSON list of {address, why} objects" % path)
    entries = []
    for item in document:
        if not isinstance(item, dict) or not isinstance(item.get(ALLOW_KEY_ADDRESS), str) or not item[ALLOW_KEY_ADDRESS] or not isinstance(item.get(ALLOW_KEY_WHY), str) or not item[ALLOW_KEY_WHY]:
            raise ReconcileError("%s: every entry needs a non-empty %r and %r: %r" % (path, ALLOW_KEY_ADDRESS, ALLOW_KEY_WHY, item))
        unknown = set(item) - ALLOW_KEYS
        if unknown:
            raise ReconcileError("%s: entry %r has keys this script does not read: %s" % (path, item[ALLOW_KEY_ADDRESS], ", ".join(sorted(unknown))))
        if not ALLOW_ADDRESS_RE.fullmatch(item[ALLOW_KEY_ADDRESS]):
            raise ReconcileError("%s: %r is not a resource address (type.name, optionally module-prefixed or indexed)" % (path, item[ALLOW_KEY_ADDRESS]))
        standing = item.get(ALLOW_KEY_STANDING, False)
        # A JSON boolean only: a string "false" is true, and a standing entry
        # is never reported unused, so the typo would make a revert's entry
        # silently permanent.
        if not isinstance(standing, bool):
            raise ReconcileError("%s: %r's %r must be true or false, not %r" % (path, item[ALLOW_KEY_ADDRESS], ALLOW_KEY_STANDING, standing))
        entries.append(AllowEntry(item[ALLOW_KEY_ADDRESS], item[ALLOW_KEY_WHY], standing))
    return entries


def _allowed(allow):
    return frozenset(entry.address for entry in allow or ())


def unused_allowlist(allow, changes):
    """The non-standing entries this plan did not need: no delete or replace on their address."""
    needed = {c.address for c in changes if c.actions == [ACTION_DELETE] or c.actions in REPLACE_ACTIONS}
    return sorted(entry.address for entry in allow or () if not entry.standing and not any(_listed(address, {entry.address}) for address in needed))

def reconcile_project(project, runner=tofu_runner, dry_run=False, timeout=PROJECT_TIMEOUT_SECONDS, allow=None, extras=None):
    """init, plan, inspect, apply, under one deadline. Returns (outcome, detail);
    with `extras`, records the allowlist entries the plan did not need."""
    deadline = clock() + timeout
    # No allowlist given means the committed one; a caller that means "nothing" passes [].
    allow = load_allowlist(ALLOWLIST_FILE) if allow is None else allow
    allowed = _allowed(allow)
    with tempfile.TemporaryDirectory(prefix="fleet-reconcile-") as tmp:
        plan_path = os.path.join(tmp, PLAN_FILE)
        env = _project_env(tmp)
        queued, waited = clock(), 0
        try:
            with _INIT_LOCK:
                waited = clock() - queued
                _tofu(
                    [
                        "init",
                        "-reconfigure",
                        # The committed lock file chooses the providers; a run
                        # that could rewrite it would adopt a release unread.
                        "-lockfile=readonly",
                        "-input=false",
                        "-no-color",
                        "-backend-config=bucket=%s" % STATE_BUCKET_TEMPLATE.format(project=project),
                        "-backend-config=prefix=%s" % STATE_PREFIX,
                    ],
                    runner,
                    deadline,
                    env=env,
                )
            planned = _tofu(
                [
                    "plan",
                    "-input=false",
                    "-no-color",
                    "-detailed-exitcode",
                    "-var=project_id=%s" % project,
                    "-out=%s" % plan_path,
                ],
                runner,
                deadline,
                ok=(PLAN_NO_CHANGES, PLAN_HAS_CHANGES),
                env=env,
            )
            if planned.returncode == PLAN_NO_CHANGES:
                if extras is not None:
                    extras["allowlist_unused"] = unused_allowlist(allow, [])
                return OUTCOME_UNCHANGED, "nothing to apply"
            shown = _tofu(["show", "-json", plan_path], runner, deadline, env=env)
            changes = plan_changes(shown.stdout)
            if extras is not None:
                extras["allowlist_unused"] = unused_allowlist(allow, changes)
            summary = describe(changes, allowed)
            refused = refused_changes(changes, allowed)
            if refused:
                return OUTCOME_REFUSED, "%s; not a create, an in-place update, or an allowlisted delete or replace: %s" % (summary, ", ".join(refused))
            restamp = is_restamp_only(changes)
            if dry_run:
                listed = ", ".join("%s %s" % ("+".join(c.actions), c.address) for c in changes)
                return OUTCOME_PLANNED, "%s: %s" % (REASON_RESTAMP if restamp else summary, listed)
            _tofu(["apply", "-input=false", "-no-color", "-auto-approve", plan_path], runner, deadline, env=env)
            if restamp:
                return OUTCOME_CONVERGED, REASON_RESTAMP
            return OUTCOME_APPLIED, summary
        except ReconcileError as exc:
            return OUTCOME_FAILED, str(exc)
        except subprocess.TimeoutExpired as exc:
            if _step(exc) == "init":
                return OUTCOME_FAILED, REASON_CEILING_INIT % (timeout, int(waited))
            return OUTCOME_FAILED, REASON_CEILING % (timeout, INTERRUPT_GRACE_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            return OUTCOME_FAILED, REASON_RUNNER % (type(exc).__name__, exc)

def load_fixture_state(source, runner=subprocess.run):
    """The published scan, from a gs:// object or a local file."""
    if source.startswith(GCS_PREFIX):
        try:
            result = runner(
                ["gcloud", "storage", "cat", source], capture_output=True, text=True, timeout=GCLOUD_TIMEOUT_SECONDS
            )
        except OSError as exc:
            raise ReconcileError("could not run gcloud to read %s: %s" % (source, exc))
        if result.returncode != 0:
            raise ReconcileError("could not read %s: %s" % (source, _tail(result.stderr)))
        raw = result.stdout
    else:
        try:
            raw = pathlib.Path(source).read_text()
        except OSError as exc:
            raise ReconcileError("could not read %s: %s" % (source, exc))
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ReconcileError("%s is not JSON: %s" % (source, exc))


def drifted_projects(document):
    return sorted(fixture_state.drift_map(document))


def pool_projects(ci_deploy_script=fixture_state.CI_DEPLOY_SCRIPT):
    """The projects the pool maps, the only names this script will ask Boskos for."""
    try:
        text = pathlib.Path(ci_deploy_script).read_text()
    except OSError as exc:
        raise ReconcileError("could not read %s: %s" % (ci_deploy_script, exc))
    match = fixture_state.MAPPING_RE.search(text)
    if not match:
        raise ReconcileError("no gitops_repo_for_project() in %s" % ci_deploy_script)
    return set(fixture_state.MAPPING_ROW_RE.findall(match.group(1)))


def pool_size(ci_deploy_script=fixture_state.CI_DEPLOY_SCRIPT):
    """How many projects the pool maps, the bound on a walk of it."""
    return len(pool_projects(ci_deploy_script))


def git_output(args):
    """stdout of `git <args>` in the repository, its trailing newline dropped;
    ReconcileError on a failure. Decoded here, not by text mode: text mode
    also folds CR and CRLF into LF, and a path git prints must arrive as the
    bytes it has (surrogateescape carries one that is not UTF-8)."""
    try:
        result = subprocess.run(["git", "-C", str(REPO_ROOT)] + list(args), capture_output=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReconcileError("git %s: %s" % (" ".join(args), exc))
    if result.returncode != 0:
        raise ReconcileError("git %s exited %d: %s" % (" ".join(args), result.returncode, _tail(result.stderr.decode(GIT_TEXT_ENCODING, GIT_TEXT_ERRORS))))
    return result.stdout.decode(GIT_TEXT_ENCODING, GIT_TEXT_ERRORS).rstrip("\n")


def _iso(epoch):
    return time.strftime(ISO_UTC_FORMAT, time.gmtime(epoch))


def is_fleet_input(path):
    """Whether a path under the fleet directory is one of the stack's inputs."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(FLEET_INPUT_SUFFIXES) or name in FLEET_INPUT_NAMES


class NoStackError(ReconcileError):
    """A rev git answered for whose bench/tf/fleet holds no stack input."""


def fleet_tree(rev):
    """A hash over the stack's inputs under FLEET_SUBDIR at `rev`: each input's
    mode, blob and path from `git ls-tree`, NUL between entries as git gives
    them (the one byte a path cannot hold, so no two trees share an input), so
    a change to any of them moves it and a change to anything else there does
    not. A rev with no inputs there
    raises NoStackError, never a hash: at the run's first check that is a
    configuration error that fails the run; later in the run it means main
    has moved away from the stack and the run stops."""
    # -z: NUL-separated entries with the path unquoted, so a non-ASCII name is
    # still matched by its suffix, and a newline inside a name stays inside it.
    listing = git_output(["ls-tree", "-r", "-z", "--full-tree", rev, "--", FLEET_SUBDIR])
    lines = sorted(entry for entry in listing.split("\0") if "\t" in entry and is_fleet_input(entry.split("\t", 1)[1]))
    if not lines:
        raise NoStackError("no stack inputs (%s, %s) under %s at %s" % ("/".join(FLEET_INPUT_SUFFIXES), "/".join(FLEET_INPUT_NAMES), FLEET_SUBDIR, rev))
    return hashlib.sha256("\0".join(lines).encode(GIT_TEXT_ENCODING, GIT_TEXT_ERRORS)).hexdigest()


class Run:
    """One run's budget and provenance, shared by every project it visits.

    `budget_seconds` is how long the whole run may take (None: unbounded, a
    hand run); `ceiling_seconds` the most one project may take, and the least
    that must be left for one to start. `main_ref` ("origin/main") makes the
    run stop, with the rest not reached, once that ref's fleet tree is no
    longer the one this checkout applies. `extras` keeps per-project times
    and the allowlist entries the plan did not need, for the report.
    """

    def __init__(self, budget_seconds=None, ceiling_seconds=PROJECT_TIMEOUT_SECONDS, main_ref=None, allow=None, workers=DEFAULT_WORKERS, commit=None, fleet_tree=None, build=None, job=None, publish=False):
        # The budget clock starts at the first project, not here: the main
        # fetch and the Boskos reset before the walk are not charged to it,
        # so a budget of exactly one ceiling still starts one project.
        self.started = None
        self.budget = budget_seconds
        self.ceiling = ceiling_seconds
        self.main_ref = main_ref
        self.allow = None if allow is None else list(allow)
        self.workers = max(1, int(workers))
        self.commit = commit
        self.fleet_tree = fleet_tree
        self.build = build
        self.job = job
        self.publish = publish
        self.extras = {}
        self.lock = threading.Lock()
        # The last failure of the main-moved check, for the report: a run
        # that could not read main did not prove it had not moved.
        self.main_check_error = None
        # How many projects the run set out to visit, for the report: a claim
        # about the whole pool (an allowlist entry nobody needs) holds only
        # when visited reaches this.
        self.mapped = None
        self.mapped_names = None
        # Set when the stop was main moving under the run: nothing is wrong,
        # the next run does the work, so a run that visited nothing for that
        # reason is not red.
        self.main_moved = False
        self._stop = None
        self._last_main_check = None

    def stop(self, reason):
        """Stop starting projects for `reason` (sticky; the first one wins)."""
        with self.lock:
            if not self._stop:
                self._stop = reason

    def room(self):
        """Seconds left in the budget; None when unbounded."""
        if self.started is None:
            self.started = clock()
        return None if self.budget is None else self.budget - (clock() - self.started)

    def wait_allowance(self, wanted):
        """How long the run may pause for busy projects and still start one after."""
        room = self.room()
        return wanted if room is None else min(wanted, room - self.ceiling)

    def stop_reason(self):
        """Why no further project may start: the budget, or main moved; None to go on. Sticky once set."""
        if self._stop:
            return self._stop
        if terminating():
            self._stop = REASON_NOT_REACHED_TERMINATED
            return self._stop
        room = self.room()
        if room is not None and room < self.ceiling:
            self._stop = REASON_NOT_REACHED_BUDGET % (max(0, int(room)), self.ceiling)
            return self._stop
        moved = self._main_moved()
        if moved:
            self._stop = moved
        return self._stop

    def require_main_readable(self):
        """The first read of `main_ref`, before anything is leased: a ref git
        cannot fetch, or one with no stack under it, is a configuration error,
        not "main has not moved"; a
        failure later in the run is a warning the report carries."""
        if not self.main_ref:
            return
        moved = self._main_moved(first=True)
        if self.main_check_error:
            raise ReconcileError("--stop-when-moved %s: %s" % (self.main_ref, self.main_check_error))
        if moved:
            self._stop = moved

    def _main_moved(self, first=False):
        if not self.main_ref:
            return None
        now = clock()
        if not first and self._last_main_check is not None and now - self._last_main_check < MAIN_CHECK_INTERVAL_SECONDS:
            return None
        self._last_main_check = now
        remote, _, branch = self.main_ref.partition("/")
        try:
            if self.fleet_tree is None:
                try:
                    self.fleet_tree = fleet_tree("HEAD")
                except NoStackError as exc:
                    # The checkout's own tree, not main's: a definite reading,
                    # fatal at the first check.
                    self.main_check_error = "this checkout: %s (%s)" % (exc, HINT_NO_STACK)
                    return None
            # No --depth: a depth-limited fetch marks a full clone shallow,
            # and a hand run with this flag uses the operator's own checkout.
            git_output(["fetch", "--quiet", remote, branch])
            try:
                current = fleet_tree("FETCH_HEAD")
            except NoStackError as exc:
                if first:
                    # A definite reading, not a failed one: no warning here;
                    # require_main_readable makes it the fatal configuration error.
                    self.main_check_error = "%s (%s)" % (exc, HINT_NO_STACK)
                    return None
                # The fetch answered and the tree holds no stack: main has
                # moved to a state this run must not apply over, a stop.
                self.main_moved = True
                return REASON_NOT_REACHED_MOVED_AWAY % (self.main_ref, self.fleet_tree)
        except ReconcileError as exc:
            # Not knowing is not the same as having moved: the run goes on
            # and tries again at the next check, and the report says so.
            self.main_check_error = str(exc)
            print("WARNING: could not read %s's fleet tree: %s" % (self.main_ref, exc), file=sys.stderr)
            return None
        if current != self.fleet_tree:
            self.main_moved = True
            return REASON_NOT_REACHED_MOVED % (self.main_ref, current, self.fleet_tree)
        return None

    def mark(self, project, **fields):
        with self.lock:
            self.extras.setdefault(project, {}).update(fields)


def publish_applied(project, outcome, run):
    """Write the project's applied.json marker; a warning string when it could not be written, else None."""
    body = {
        "schema_version": APPLIED_SCHEMA_VERSION,
        "project": project,
        "commit": run.commit,
        "fleet_tree": run.fleet_tree,
        "outcome": outcome,
        "finished_at": _iso(time.time()),
        "build": run.build,
        "job": run.job,
    }
    target = APPLIED_OBJECT_TEMPLATE.format(project=project)
    try:
        result = gcloud_runner(["gcloud", "storage", "cp", "-", target], input=json.dumps(body, indent=2) + "\n", capture_output=True, text=True, timeout=GCLOUD_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        return WARNING_MARKER % exc
    if result.returncode != 0:
        return WARNING_MARKER % _tail(result.stderr or result.stdout)
    return None


def _line(project, outcome):
    """One project's result, printed as it happens: a run that is terminated
    or loses Boskos mid-walk has still named every project it reached."""
    print("%s: %s (%s)" % (project, outcome[0], outcome[1]), flush=True)

def _record(project, outcomes, runner, dry_run, run=None):
    """reconcile_project with its outcome recorded and printed before the
    caller sees it, the project a termination landed in included."""
    run = run or Run()
    extras = {}
    run.mark(project, started_at=_iso(time.time()))
    try:
        outcome = reconcile_project(project, runner=runner, dry_run=dry_run, timeout=run.ceiling, allow=run.allow, extras=extras)
    except TerminatedBeforeTofu as exc:
        outcomes[project] = (OUTCOME_INTERRUPTED, REASON_INTERRUPTED_BEFORE % (exc, exc.step))
        run.mark(project, finished_at=_iso(time.time()))
        _line(project, outcomes[project])
        raise
    except boskos_pool.Terminated as exc:
        outcomes[project] = (OUTCOME_INTERRUPTED, REASON_INTERRUPTED % exc)
        run.mark(project, finished_at=_iso(time.time()))
        _line(project, outcomes[project])
        raise
    if terminating() and outcome[0] == OUTCOME_FAILED:
        # A worker's tofu exited on the interrupt the main thread forwarded.
        outcome = (OUTCOME_INTERRUPTED, REASON_INTERRUPTED % "signal")
    # On the record before the marker is written: a termination during the
    # write must not drop a project whose apply just happened. No allowlist
    # verdict for a plan that was not read: an empty list would read as "this
    # project needed every entry" when the entries are counted.
    outcomes[project] = outcome
    run.mark(project, finished_at=_iso(time.time()), **({"allowlist_unused": extras["allowlist_unused"]} if "allowlist_unused" in extras else {}))
    if outcome[0] in AT_TREE_OUTCOMES and not dry_run and run.publish:
        try:
            warning = publish_applied(project, outcome[0], run)
        except boskos_pool.Terminated:
            outcomes[project] = (outcome[0], "%s; %s" % (outcome[1], WARNING_MARKER % "terminated during the write"))
            _line(project, outcomes[project])
            raise
        if warning:
            outcomes[project] = (outcome[0], "%s; %s" % (outcome[1], warning))
    _line(project, outcomes[project])

def report(outcomes):
    failing = sorted(p for p, (outcome, _) in outcomes.items() if outcome in FAILING_OUTCOMES)
    count = lambda outcome: sum(1 for o, _ in outcomes.values() if o == outcome)  # noqa: E731
    print(
        "reconciled %d project(s): %d applied, %d converged, %d unchanged, %d planned, %d busy, %d refused or failed, %d interrupted, %d not reached"
        % (
            len(outcomes),
            count(OUTCOME_APPLIED),
            count(OUTCOME_CONVERGED),
            count(OUTCOME_UNCHANGED),
            count(OUTCOME_PLANNED),
            count(OUTCOME_BUSY),
            len(failing),
            count(OUTCOME_INTERRUPTED),
            count(OUTCOME_NOT_REACHED),
        )
    )
    return failing

def reconcile_named(projects, server, owner, lease=True, runner=tofu_runner, dry_run=False, known=None, outcomes=None, run=None):
    """Each named project, held through Boskos unless `lease` is off.

    A name outside the pool mapping is failed before Boskos is asked: Boskos
    answers 404 for a leased project and for one it has never heard of alike,
    so a typo would otherwise read as busy on every run. Without a lease the
    mapping is not consulted; that is the dev-project path. A project the
    run's budget cannot fit is not reached.
    """
    # The mapping is read only when Boskos will be asked: the dev-project
    # path has no use for it and must not fail on it.
    known = pool_projects() if known is None and lease else known
    outcomes = {} if outcomes is None else outcomes
    run = run or Run()
    run.mapped = len(projects)

    def not_reached(rest, reason):
        for project in rest:
            if project not in outcomes:
                outcomes[project] = (OUTCOME_NOT_REACHED, reason)
                _line(project, outcomes[project])

    for index, project in enumerate(projects):
        try:
            # Inside the try: the stop check may fetch main, and a termination
            # landing there must still leave the rest on the report.
            stop = run.stop_reason()
            if stop:
                not_reached(projects[index:], stop)
                break
            if not lease:
                _record(project, outcomes, runner, dry_run, run)
                continue
            if project not in known:
                outcomes[project] = (OUTCOME_FAILED, REASON_UNMAPPED)
                _line(project, outcomes[project])
                continue
            if _hold_named(project, server, owner, runner, dry_run, run, outcomes) is boskos_pool.NOT_ACQUIRED:
                outcomes[project] = (OUTCOME_BUSY, REASON_BUSY)
                _line(project, outcomes[project])
        except boskos_pool.Terminated:
            # The project the signal landed in has its line if it was reached;
            # every project not yet on the record is not reached, so the
            # summary adds up whichever window the signal landed in.
            not_reached(projects[index:], REASON_NOT_REACHED_TERMINATED)
            raise
        except Exception as exc:  # noqa: BLE001 -- a Boskos or network fault: on the record, then raised
            if project not in outcomes:
                outcomes[project] = (OUTCOME_FAILED, REASON_RUN_ERROR % boskos_pool.describe(exc))
                _line(project, outcomes[project])
            not_reached(projects[index + 1 :], REASON_NOT_REACHED_RUN_ERROR % boskos_pool.describe(exc))
            raise
    return outcomes


def _hold_named(project, server, owner, runner, dry_run, run, outcomes):
    """Acquire `project` by name, reconcile it held, release it; NOT_ACQUIRED when it is not free."""
    release_failures = {}

    def visit(p):
        # Recorded and printed inside the hold, so a termination that lands
        # during the release still leaves this project's apply on record.
        _record(p, outcomes, runner, dry_run, run)

    try:
        return boskos_pool.acquire_and_hold(
            server,
            owner,
            HOLD_STATE,
            lambda: boskos_pool.acquire(server, owner, HOLD_STATE, name=project),
            visit,
            release_failures,
            heartbeat=True,
        )
    finally:
        # Merged in a finally: a release that failed before a termination
        # unwound the hold is on the record the run writes on its way out,
        # and an interrupted project keeps its outcome.
        if project in release_failures:
            _merge_release_failure(outcomes, project, release_failures[project])

def reconcile_pool(server, owner, runner=tofu_runner, dry_run=False, known=None, outcomes=None, run=None):
    """Every mapped project once, by name, until all are visited or the
    budget is spent; `run.workers` projects at a time.

    Asked for by name rather than taken as Boskos hands them out, so a
    project leased when the run starts is asked for again, every
    POLL_INTERVAL_SECONDS, and reached once free. A registration outside the
    mapping is never asked for by name; anonymous acquires after the pass,
    until a project comes round twice, find one and red the run
    (_check_for_stray_registration). What is
    still busy, or not started, when the budget runs out or main's fleet tree
    moves is `not_reached`, for the next run.
    """
    known = pool_projects() if known is None else known
    outcomes = {} if outcomes is None else outcomes
    run = run or Run()
    run.mapped = len(known)
    run.mapped_names = set(known)
    pending = sorted(known)
    seen_busy = set()
    drained = []

    def drain(reason, outcome=OUTCOME_NOT_REACHED):
        # Under run.lock. A budget that ran out while a project was busy:
        # the project was busy, the budget only timed the wait, and the
        # report says the former, whichever worker drains.
        drained.append(reason)
        while pending:
            project = pending.pop(0)
            why = REASON_NOT_REACHED_BUSY if project in seen_busy and _is_budget_reason(reason) else reason
            outcomes[project] = (outcome, why)
            _line(project, outcomes[project])

    def worker():
        busy = set()
        while True:
            # The stop check may fetch main; it runs outside the lock so the
            # other workers do not hold their leases waiting on it.
            stop = run.stop_reason()
            with run.lock:
                if not pending:
                    return
                if stop:
                    drain(stop)
                    return
                project = pending.pop(0)
            try:
                acquired = _hold_named(project, server, owner, runner, dry_run, run, outcomes) is not boskos_pool.NOT_ACQUIRED
            except boskos_pool.Terminated:
                # Deferred across the acquire and raised before the visit: the
                # project was popped, held and released, and never planned.
                if project not in outcomes:
                    outcomes[project] = (OUTCOME_NOT_REACHED, REASON_NOT_REACHED_TERMINATED)
                    _line(project, outcomes[project])
                raise
            except Exception as exc:  # noqa: BLE001 -- a Boskos or network fault: on the record, and the run stops
                # The project is on the report as this run's error, and the
                # other workers stop leasing under a service that just failed;
                # the exception itself is raised once they have.
                if project not in outcomes:
                    outcomes[project] = (OUTCOME_FAILED, REASON_RUN_ERROR % boskos_pool.describe(exc))
                    _line(project, outcomes[project])
                run.stop(REASON_NOT_REACHED_RUN_ERROR % boskos_pool.describe(exc))
                raise
            if acquired:
                busy.clear()
                continue
            with run.lock:
                pending.append(project)
                busy.add(project)
                seen_busy.add(project)
                every_remaining_busy = busy >= set(pending)
            if not every_remaining_busy:
                continue
            # Only a budgeted run waits for busy projects: a hand run with no
            # budget reports them busy, as a named project is.
            wait = run.wait_allowance(POLL_INTERVAL_SECONDS) if run.budget is not None else 0
            if wait <= 0:
                with run.lock:
                    drain(REASON_NOT_REACHED_BUSY if run.budget is not None else REASON_BUSY, OUTCOME_NOT_REACHED if run.budget is not None else OUTCOME_BUSY)
                return
            pause(wait)
            busy.clear()

    try:
        if run.workers == 1:
            worker()
        else:
            _run_workers(worker, run.workers)
    except boskos_pool.Terminated:
        # The single-worker path raises straight out of the hold; the threaded
        # one may lose the worker that was draining. Either way the projects
        # never started are on the report before the termination propagates.
        with run.lock:
            drain(REASON_NOT_REACHED_TERMINATED)
        raise
    except Exception as exc:  # noqa: BLE001 -- re-raised; the report first
        with run.lock:
            drain(run.stop_reason() or REASON_NOT_REACHED_RUN_ERROR % boskos_pool.describe(exc))
        raise
    # A pass that asked for every project makes the check even when the
    # budget ran low during its last one: the check leases nothing for long
    # and runs no tofu. A drain (budget, main moved) or a termination skips it.
    if not drained and not terminating():
        _check_for_stray_registration(server, owner, runner, dry_run, run, outcomes)
    return outcomes


def _check_for_stray_registration(server, owner, runner, dry_run, run, outcomes):
    """Anonymous acquires after the by-name pass, until Boskos hands back a
    project this check has already seen. A project the mapping lacks is
    released untouched and failed, as the old walk did: a registration with
    no mapping row takes a share of every pull request's leases and nothing
    else watched reports it. Boskos hands out the free project untouched
    the longest, and a presubmit that leased the stray mid-pass makes it
    newer than the projects released first, so one acquire is not enough:
    each release here re-stamps what it got, so the free projects come
    round once each, the stray among them, before the first repeat. A
    mapped project is released untouched; the bound covers a pool that
    never repeats."""
    release_failures = {}
    seen = set()
    done = []

    def visit(project):
        if project in seen:
            done.append(True)
            return
        seen.add(project)
        if project in outcomes:
            return
        outcomes[project] = (OUTCOME_FAILED, REASON_UNMAPPED)
        _line(project, outcomes[project])

    try:
        for _ in range(len(outcomes) + STRAY_CHECK_EXTRA_ACQUIRES):
            got = boskos_pool.acquire_and_hold(server, owner, HOLD_STATE, lambda: boskos_pool.acquire(server, owner, HOLD_STATE), visit, release_failures, heartbeat=True)
            if got is boskos_pool.NOT_ACQUIRED or done:
                break
    finally:
        for project, reason in release_failures.items():
            _merge_release_failure(outcomes, project, reason)


def _run_workers(worker, count):
    """`count` threads running `worker`; the main thread waits, and on a
    termination forwards it to every live tofu, waits for the holds to
    release, and raises it on."""
    failures = []
    _TERMINATING.clear()
    # One flag per worker, set when it has returned its last project. The
    # waits below are on these, not on Thread.join: on Python 3.12 a join the
    # termination interrupts marks the thread stopped while it still runs,
    # and a drain built on join would raise before the holds were released.
    done = [threading.Event() for _ in range(count)]

    def guarded(flag):
        try:
            worker()
        except BaseException as exc:  # noqa: BLE001 -- re-raised in the main thread
            failures.append(exc)
        finally:
            flag.set()

    threads = [threading.Thread(target=guarded, args=(flag,), name="fleet-reconcile-%d" % i, daemon=True) for i, flag in enumerate(done)]
    # Signals reach the main thread alone, so a termination landing while the
    # workers are being started would escape the `except` below: no flag, no
    # drain, the applies left running. The starts run inside the `try` with
    # terminations deferred, and the unblock raises one that landed only once
    # every worker is running, where the drain handles it. A termination
    # raised out of the hold itself propagates with nothing started.
    boskos_pool._hold_signals(True)
    try:
        try:
            for thread in threads:
                thread.start()
        finally:
            boskos_pool._hold_signals(False)
        while not all(flag.is_set() for flag in done):
            for flag in done:
                flag.wait(WORKER_JOIN_STEP_SECONDS)
    except boskos_pool.Terminated:
        # The drain runs with later terminations held, whichever way the
        # first arrived: the handler and the unblock above both hold later
        # ones before they raise (boskos_pool.terminate), so a second signal
        # landing anywhere from this catch to the re-raise is recorded, not
        # raised out of the drain with the holds still to release.
        _begin_termination()
        deadline = clock() + WORKER_DRAIN_SECONDS
        for flag in done:
            flag.wait(max(0, deadline - clock()))
        _children_signal(kill=True)
        for flag in done:
            flag.wait(WORKER_JOIN_STEP_SECONDS)
        raise
    if failures:
        raise failures[0]

def _merge_release_failure(outcomes, project, reason):
    """A project the termination landed in keeps its interrupted outcome (and
    its force-unlock hint); the release failure joins its reason. Any other
    outcome becomes the failure."""
    outcome, detail = outcomes.get(project) or (None, None)
    if outcome == OUTCOME_INTERRUPTED:
        outcomes[project] = (OUTCOME_INTERRUPTED, "%s; %s" % (detail, reason))
    else:
        outcomes[project] = (OUTCOME_FAILED, reason)
    _line(project, outcomes[project])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true", help="every mapped project, asked for by name; busy ones are asked again within the budget")
    mode.add_argument("--drifted", action="store_true", help="the projects the fixture-state scan reports drifted")
    mode.add_argument("--project", action="append", help="one project (repeatable)")
    parser.add_argument("--fixture-state", default=DEFAULT_FIXTURE_STATE, help="with --drifted: the scan, gs:// or a path")
    parser.add_argument("--no-lease", action="store_true", help="with --project: do not ask Boskos (a dev project, or a lease you hold)")
    parser.add_argument("--dry-run", action="store_true", help="plan and inspect, apply nothing")
    parser.add_argument("--budget-seconds", type=int, help="how long the whole run may take; no project starts with less than the ceiling left (default: unbounded)")
    parser.add_argument("--project-ceiling-seconds", type=int, default=PROJECT_TIMEOUT_SECONDS, help="the most one project may take, init through apply (default: %(default)s)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="projects reconciled at once, each under its own lease (default: %(default)s)")
    parser.add_argument("--stop-when-moved", metavar="REMOTE/BRANCH", help="stop, with the rest not reached, once this ref's fleet tree (a hash of the stack's files under %s: %s, %s) differs from the checkout's (the jobs pass origin/main)" % (FLEET_SUBDIR, ", ".join("*" + s for s in FLEET_INPUT_SUFFIXES), ", ".join(FLEET_INPUT_NAMES)))
    parser.add_argument("--no-publish", action="store_true", help="do not write applied.json to the project's state bucket")
    parser.add_argument(
        "--report",
        default=os.path.join(os.environ[ARTIFACTS_ENV], REPORT_FILE) if os.environ.get(ARTIFACTS_ENV) else None,
        help="write the run's outcomes as JSON here (default: $%s/%s when ARTIFACTS is set)" % (ARTIFACTS_ENV, REPORT_FILE),
    )
    parser.add_argument(
        "--boskos-server",
        default=os.environ.get("BOSKOS_SERVER", boskos_pool.DEFAULT_SERVER),
        help="Boskos endpoint (default: $BOSKOS_SERVER, else the in-cluster service)",
    )
    parser.add_argument(
        "--boskos-owner",
        default=os.environ.get("BOSKOS_OWNER") or DEFAULT_OWNER,
        help="owner name the acquisitions are recorded under",
    )
    args = parser.parse_args(argv)
    if args.no_lease and not args.project:
        parser.error("--no-lease needs --project")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.workers > 1 and not args.all:
        parser.error("--workers applies to --all; --project and --drifted walk their list one at a time")
    if args.stop_when_moved is not None:
        remote, _, branch = args.stop_when_moved.partition("/")
        if not remote or not branch:
            parser.error("--stop-when-moved takes REMOTE/BRANCH, for example origin/main")
    if args.project_ceiling_seconds < 1:
        parser.error("--project-ceiling-seconds must be at least 1")
    if args.budget_seconds is not None and args.budget_seconds < args.project_ceiling_seconds:
        # A budget under one ceiling would drain every project as not reached
        # and exit green, applying nothing.
        parser.error("--budget-seconds must be at least the per-project ceiling (%d)" % args.project_ceiling_seconds)
    for sig in TERMINATION_SIGNALS:
        signal.signal(sig, boskos_pool.terminate)
    outcomes = {}
    started = time.time()
    error = []
    code = None
    run = None
    try:
        try:
            run = _start(args, error)
        except SystemExit as exc:
            # A refused allowlist or an unreadable main ref: reported above,
            # nothing leased, the report still written below.
            code = exc.code if isinstance(exc.code, int) else EXIT_FAILED
            return code
        except boskos_pool.Terminated as exc:
            # The signal landed in the main fetch, before the first lease:
            # the same exit as one inside the run.
            code = _terminated(exc, outcomes, error)
            return code
        code = _run(args, outcomes, error, run)
    except BaseException as exc:
        # Unhandled: the report still names what killed the run.
        error.append("%s: %s" % (type(exc).__name__, exc))
        raise
    finally:
        # Written whatever happened above: an exception no arm of _run
        # handles still leaves a report, with `exit` "error" and no code.
        if args.report:
            write_report(args.report, args, outcomes, code, error[0] if error else None, started, run)
    return code


def _provenance():
    """(commit, fleet tree) of the checkout; None for either git cannot answer, with a warning."""
    values = []
    for name, read in (("commit", lambda: git_output(["rev-parse", "HEAD"])), ("fleet tree", lambda: fleet_tree("HEAD"))):
        try:
            values.append(read())
        except ReconcileError as exc:
            print("WARNING: %s; the report and the markers carry no %s" % (exc, name), file=sys.stderr)
            values.append(None)
    return tuple(values)


def _start(args, error):
    """The Run for these arguments; the allowlist is read whole first, so a
    malformed one stops the run before anything is leased."""
    # The committed, reviewed list and no other: there is no flag to point a
    # run at a different file.
    try:
        allow = load_allowlist(ALLOWLIST_FILE)
    except ReconcileError as exc:
        error.append(str(exc))
        print("ERROR: %s" % exc, file=sys.stderr)
        raise SystemExit(EXIT_FAILED)
    commit, tree = _provenance()
    run = Run(
        budget_seconds=args.budget_seconds,
        ceiling_seconds=args.project_ceiling_seconds,
        main_ref=args.stop_when_moved,
        allow=allow,
        workers=args.workers,
        commit=commit,
        fleet_tree=tree,
        build=os.environ.get(BUILD_ID_ENV),
        job=os.environ.get(JOB_NAME_ENV),
        publish=not args.no_publish,
    )
    try:
        run.require_main_readable()
    except ReconcileError as exc:
        error.append(str(exc))
        print("ERROR: %s" % exc, file=sys.stderr)
        raise SystemExit(EXIT_FAILED)
    return run


def write_report(path, args, outcomes, code, error, started, run=None):
    """The run's outcomes as one JSON document, written last: what the CI
    health bot names when a run fails, and nothing a signal can cut short
    except the write itself."""
    mode = MODE_PROJECT if args.project else (MODE_DRIFTED if args.drifted else MODE_ALL)
    extras = run.extras if run else {}
    document = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": mode,
        "dry_run": bool(args.dry_run),
        "commit": run.commit if run else None,
        "fleet_tree": run.fleet_tree if run else None,
        "build": run.build if run else None,
        "job": run.job if run else None,
        "workers": run.workers if run else None,
        "budget_seconds": run.budget if run else None,
        "ceiling_seconds": run.ceiling if run else None,
        "main_ref": run.main_ref if run else None,
        "main_check_error": run.main_check_error if run else None,
        "started_at": _iso(started),
        "finished_at": _iso(time.time()),
        "exit": EXIT_NAMES.get(code, EXIT_NAME_ERROR),
        "exit_code": code,
        "error": error,
        "mapped": run.mapped if run else None,
        "visited": visited_count(outcomes, run),
        "outcomes": {
            project: dict({"outcome": outcome, "detail": detail}, **extras.get(project, {}))
            for project, (outcome, detail) in sorted(outcomes.items())
        },
        "summary": {outcome: sum(1 for o, _ in outcomes.values() if o == outcome) for outcome in OUTCOMES},
    }
    try:
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(path).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print("WARNING: could not write the report to %s (%s)" % (path, exc), file=sys.stderr)


def _terminated(exc, outcomes, error):
    """The terminated exit: every project reached has its line above, the one
    the signal landed in included; the summary says how far the run got."""
    report(outcomes)
    interrupted = sorted(p for p, (o, _) in outcomes.items() if o == OUTCOME_INTERRUPTED)
    # The drain puts every pending project on the record as not reached
    # before the termination gets here; those were not got through.
    reached = sum(1 for o, _ in outcomes.values() if o != OUTCOME_NOT_REACHED)
    message = "terminated (%s) after %d project(s)%s; held projects were released unless named above" % (
        exc, reached, "; interrupted in %s" % ", ".join(interrupted) if interrupted else ""
    )
    error.append(message)
    print("ERROR: %s" % message, file=sys.stderr)
    return boskos_pool.TERMINATED_EXIT_CODE


def _run(args, outcomes, error, run):
    """The reconcile itself; returns the exit code and records the run's
    error, if any, in `error` for the report."""
    try:
        if args.project:
            if not args.no_lease:
                boskos_pool.reset_stranded(args.boskos_server, HOLD_STATE, STRANDED_AFTER, "reconcile")
            reconcile_named(
                args.project,
                args.boskos_server,
                args.boskos_owner,
                lease=not args.no_lease,
                runner=tofu_runner,
                dry_run=args.dry_run,
                outcomes=outcomes,
                run=run,
            )
        else:
            boskos_pool.reset_stranded(args.boskos_server, HOLD_STATE, STRANDED_AFTER, "reconcile")
            if args.drifted:
                projects = drifted_projects(load_fixture_state(args.fixture_state))
                print("%d project(s) drifted in %s" % (len(projects), args.fixture_state))
                reconcile_named(
                    projects, args.boskos_server, args.boskos_owner, runner=tofu_runner, dry_run=args.dry_run, outcomes=outcomes, run=run
                )
            else:
                known = pool_projects()
                reconcile_pool(args.boskos_server, args.boskos_owner, runner=tofu_runner, dry_run=args.dry_run, known=known, outcomes=outcomes, run=run)
        failing = report(outcomes)
        if failing:
            error.append("%d project(s) not reconciled: %s" % (len(failing), ", ".join(failing)))
            print("ERROR: %s" % error[-1], file=sys.stderr)
            return EXIT_FAILED
        visited = visited_count(outcomes, run)
        if args.all and outcomes and visited == 0 and not run.main_moved:
            # Busy for the whole budget, unregistered under the mapped name,
            # or a budget the poll ate: whichever, nothing applied today, and
            # a green build would hide it.
            error.append(REASON_VISITED_NONE % len(outcomes))
            print("ERROR: %s" % error[-1], file=sys.stderr)
            return EXIT_FAILED
        return EXIT_OK
    except boskos_pool.Terminated as exc:
        return _terminated(exc, outcomes, error)
    except (ReconcileError, boskos_pool.BoskosError) as exc:
        report(outcomes)
        error.append(str(exc))
        print("ERROR: %s" % exc, file=sys.stderr)
        return EXIT_FAILED
    except subprocess.SubprocessError as exc:
        report(outcomes)
        error.append("could not run a command (%s: %s)" % (type(exc).__name__, exc))
        print("ERROR: %s" % error[-1], file=sys.stderr)
        return EXIT_FAILED
    except boskos_pool.REACH_ERRORS as exc:
        report(outcomes)
        error.append("could not reach a service (%s: %s)" % (type(exc).__name__, exc))
        print("ERROR: %s" % error[-1], file=sys.stderr)
        return EXIT_FAILED

if __name__ == "__main__":
    sys.exit(main())
