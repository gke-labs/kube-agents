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
when main's fleet tree is no longer the one it checked out, so two runs at
different commits never apply over each other. `--report` writes
fleet-reconcile.json (mode, commit, tree, per-project outcomes with times, a
summary), under $ARTIFACTS when Prow sets it, for the CI health bot
(scripts/eval_dashboard/periodics.py); each applied project also gets
`applied.json` in its state bucket, the record of which commit it is at.
"""

import argparse
import collections
import json
import os
import pathlib
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
MAIN_CHECK_INTERVAL_SECONDS = 60
DEFAULT_WORKERS = 1
# How long the threads get to return their projects after a termination
# interrupted their applies, before the children are killed outright.
WORKER_DRAIN_SECONDS = INTERRUPT_GRACE_SECONDS + 30
WORKER_JOIN_STEP_SECONDS = 0.2
FLEET_SUBDIR = "bench/tf/fleet"
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
REASON_INTERRUPTED = "terminated (%s) while tofu ran; an apply cut past its grace leaves the state locked: tofu force-unlock"
REASON_CEILING = "did not finish within %ds; tofu was interrupted, and killed if it did not stop within %ds, which leaves the state locked: tofu force-unlock"
REASON_NOT_REACHED_BUDGET = "not started: %ds left in the run's budget, under the %ds per-project ceiling; the next run takes it"
REASON_NOT_REACHED_MOVED = "not started: bench/tf/fleet on %s is now %s and this run applies %s; the next run takes it"
REASON_NOT_REACHED_BUSY = "not free in Boskos before the run's budget ran out; the next run takes it"
REASON_NOT_REACHED_TERMINATED = "not started: the run was terminated; the next run takes it"
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


def tofu_runner(argv, cwd=None, timeout=None, **_):
    """subprocess.run for tofu, with a graceful stop.

    A killed tofu leaves the state locked and the next run failing on the lock,
    so on a termination signal or the ceiling it gets SIGINT and a grace period
    first. Its own session, so a terminal's Ctrl-C reaches this process alone
    and tofu sees one interrupt, the forwarded one: a second interrupt makes
    tofu exit at once, mid-operation.
    """
    env = dict(os.environ, **TOFU_ENV)
    proc = None
    try:
        # A termination while the child is being started would leave it
        # running with no handle to interrupt or kill; deferred until Popen
        # has returned, it lands below with `proc` in hand.
        boskos_pool._hold_signals(True)
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
            )
            _children_add(proc)
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
    with _CHILDREN_LOCK:
        _CHILDREN.add(proc)


def _children_discard(proc):
    with _CHILDREN_LOCK:
        _CHILDREN.discard(proc)


def _children_signal(kill=False):
    with _CHILDREN_LOCK:
        procs = list(_CHILDREN)
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


def _tofu(args, runner, deadline, ok=(0,)):
    timeout = max(1, deadline - clock())
    result = runner([TOFU] + list(args), cwd=str(FLEET_DIR), timeout=timeout, capture_output=True, text=True)
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

def _applied(actions, address, allowed):
    if actions in APPLIED_ACTIONS:
        return True
    return (actions in REPLACE_ACTIONS or actions == [ACTION_DELETE]) and address in allowed

def refused_changes(changes, allowed=frozenset()):
    """The changes a re-apply must not make: everything but a create, an in-place update, or a delete or replace the allowlist declares."""
    return ["%s %s" % ("+".join(change.actions), change.address) for change in changes if not _applied(change.actions, change.address, allowed)]

def describe(changes, allowed=frozenset()):
    add = sum(1 for c in changes if c.actions == [ACTION_CREATE])
    change = sum(1 for c in changes if c.actions == [ACTION_UPDATE])
    replace = sum(1 for c in changes if c.actions in REPLACE_ACTIONS and c.address in allowed)
    destroy = sum(1 for c in changes if c.actions == [ACTION_DELETE] and c.address in allowed)
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
        entries.append(AllowEntry(item[ALLOW_KEY_ADDRESS], item[ALLOW_KEY_WHY], bool(item.get(ALLOW_KEY_STANDING, False))))
    return entries


def _allowed(allow):
    return frozenset(entry.address for entry in allow or ())


def unused_allowlist(allow, changes):
    """The non-standing entries this plan did not need: no delete or replace on their address."""
    needed = {c.address for c in changes if c.actions == [ACTION_DELETE] or c.actions in REPLACE_ACTIONS}
    return sorted(entry.address for entry in allow or () if not entry.standing and entry.address not in needed)

def reconcile_project(project, runner=tofu_runner, dry_run=False, timeout=PROJECT_TIMEOUT_SECONDS, allow=None, extras=None):
    """init, plan, inspect, apply, under one deadline. Returns (outcome, detail);
    with `extras`, records the allowlist entries the plan did not need."""
    deadline = clock() + timeout
    # No allowlist given means the committed one; a caller that means "nothing" passes [].
    allow = load_allowlist(ALLOWLIST_FILE) if allow is None else allow
    allowed = _allowed(allow)
    with tempfile.TemporaryDirectory(prefix="fleet-reconcile-") as tmp:
        plan_path = os.path.join(tmp, PLAN_FILE)
        try:
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
            )
            if planned.returncode == PLAN_NO_CHANGES:
                if extras is not None:
                    extras["allowlist_unused"] = unused_allowlist(allow, [])
                return OUTCOME_UNCHANGED, "nothing to apply"
            shown = _tofu(["show", "-json", plan_path], runner, deadline)
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
            _tofu(["apply", "-input=false", "-no-color", "-auto-approve", plan_path], runner, deadline)
            if restamp:
                return OUTCOME_CONVERGED, REASON_RESTAMP
            return OUTCOME_APPLIED, summary
        except ReconcileError as exc:
            return OUTCOME_FAILED, str(exc)
        except subprocess.TimeoutExpired:
            return OUTCOME_FAILED, REASON_CEILING % (timeout, INTERRUPT_GRACE_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            return OUTCOME_FAILED, "could not run tofu (%s: %s)" % (type(exc).__name__, exc)

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
    """stdout of `git <args>` in the repository, stripped; ReconcileError on a failure."""
    try:
        result = subprocess.run(["git", "-C", str(REPO_ROOT)] + list(args), capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReconcileError("git %s: %s" % (" ".join(args), exc))
    if result.returncode != 0:
        raise ReconcileError("git %s exited %d: %s" % (" ".join(args), result.returncode, _tail(result.stderr)))
    return result.stdout.strip()


def _iso(epoch):
    return time.strftime(ISO_UTC_FORMAT, time.gmtime(epoch))


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
        self.started = clock()
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
        self._stop = None
        self._last_main_check = None

    def room(self):
        """Seconds left in the budget; None when unbounded."""
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

    def _main_moved(self):
        if not self.main_ref:
            return None
        now = clock()
        if self._last_main_check is not None and now - self._last_main_check < MAIN_CHECK_INTERVAL_SECONDS:
            return None
        self._last_main_check = now
        remote, _, branch = self.main_ref.partition("/")
        try:
            if self.fleet_tree is None:
                self.fleet_tree = git_output(["rev-parse", "HEAD:%s" % FLEET_SUBDIR])
            git_output(["fetch", "--quiet", "--depth=1", remote, branch])
            current = git_output(["rev-parse", "FETCH_HEAD:%s" % FLEET_SUBDIR])
        except ReconcileError as exc:
            # Not knowing is not the same as having moved: the run goes on
            # and tries again at the next check, and the report says so.
            self.main_check_error = str(exc)
            print("WARNING: could not read %s's fleet tree: %s" % (self.main_ref, exc), file=sys.stderr)
            return None
        if current != self.fleet_tree:
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
    except boskos_pool.Terminated as exc:
        outcomes[project] = (OUTCOME_INTERRUPTED, REASON_INTERRUPTED % exc)
        run.mark(project, finished_at=_iso(time.time()))
        _line(project, outcomes[project])
        raise
    if terminating() and outcome[0] == OUTCOME_FAILED:
        # A worker's tofu exited on the interrupt the main thread forwarded.
        outcome = (OUTCOME_INTERRUPTED, REASON_INTERRUPTED % "signal")
    if outcome[0] in AT_TREE_OUTCOMES and not dry_run and run.publish:
        warning = publish_applied(project, outcome[0], run)
        if warning:
            outcome = (outcome[0], "%s; %s" % (outcome[1], warning))
    outcomes[project] = outcome
    # No allowlist verdict for a plan that was not read: an empty list would
    # read as "this project needed every entry" when the entries are counted.
    run.mark(project, finished_at=_iso(time.time()), **({"allowlist_unused": extras["allowlist_unused"]} if "allowlist_unused" in extras else {}))
    _line(project, outcome)

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
    for index, project in enumerate(projects):
        stop = run.stop_reason()
        if stop:
            for rest in projects[index:]:
                outcomes[rest] = (OUTCOME_NOT_REACHED, stop)
                _line(rest, outcomes[rest])
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
    mapping is never asked for (the pull sweep reports those). What is still
    busy, or not started, when the budget runs out or main's fleet tree
    moves is `not_reached`, for the next run.
    """
    known = pool_projects() if known is None else known
    outcomes = {} if outcomes is None else outcomes
    run = run or Run()
    pending = sorted(known)

    def drain(reason, outcome=OUTCOME_NOT_REACHED):
        # Under run.lock.
        while pending:
            project = pending.pop(0)
            outcomes[project] = (outcome, reason)
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
            if _hold_named(project, server, owner, runner, dry_run, run, outcomes) is not boskos_pool.NOT_ACQUIRED:
                busy.clear()
                continue
            with run.lock:
                pending.append(project)
                busy.add(project)
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

    if run.workers == 1:
        worker()
        return outcomes
    _run_workers(worker, run.workers)
    return outcomes


def _run_workers(worker, count):
    """`count` threads running `worker`; the main thread waits, and on a
    termination forwards it to every live tofu, waits for the holds to
    release, and raises it on."""
    failures = []
    _TERMINATING.clear()

    def guarded():
        try:
            worker()
        except BaseException as exc:  # noqa: BLE001 -- re-raised in the main thread
            failures.append(exc)

    threads = [threading.Thread(target=guarded, name="fleet-reconcile-%d" % i, daemon=True) for i in range(count)]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(WORKER_JOIN_STEP_SECONDS)
    except boskos_pool.Terminated:
        _TERMINATING.set()
        _children_signal()
        deadline = clock() + WORKER_DRAIN_SECONDS
        for thread in threads:
            thread.join(max(0, deadline - clock()))
        _children_signal(kill=True)
        for thread in threads:
            thread.join(WORKER_JOIN_STEP_SECONDS)
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
    parser.add_argument("--stop-when-moved", metavar="REMOTE/BRANCH", help="stop, with the rest not reached, once this ref's bench/tf/fleet tree differs from the checkout's (the jobs pass origin/main)")
    parser.add_argument("--allowlist", default=str(ALLOWLIST_FILE), help="the deletes and replaces a re-apply may make (default: %(default)s)")
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
    for sig in TERMINATION_SIGNALS:
        signal.signal(sig, boskos_pool.terminate)
    outcomes = {}
    started = time.time()
    error = []
    code = None
    run = None
    try:
        run = _start(args, error)
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
    for spec in ("HEAD", "HEAD:%s" % FLEET_SUBDIR):
        try:
            values.append(git_output(["rev-parse", spec]))
        except ReconcileError as exc:
            print("WARNING: %s; the report and the markers carry no %s" % (exc, "commit" if spec == "HEAD" else "fleet tree"), file=sys.stderr)
            values.append(None)
    return tuple(values)


def _start(args, error):
    """The Run for these arguments; the allowlist is read whole first, so a
    malformed one stops the run before anything is leased."""
    try:
        allow = load_allowlist(args.allowlist)
    except ReconcileError as exc:
        error.append(str(exc))
        print("ERROR: %s" % exc, file=sys.stderr)
        raise SystemExit(EXIT_FAILED)
    commit, fleet_tree = _provenance()
    return Run(
        budget_seconds=args.budget_seconds,
        ceiling_seconds=args.project_ceiling_seconds,
        main_ref=args.stop_when_moved,
        allow=allow,
        workers=args.workers,
        commit=commit,
        fleet_tree=fleet_tree,
        build=os.environ.get(BUILD_ID_ENV),
        job=os.environ.get(JOB_NAME_ENV),
        publish=not args.no_publish,
    )


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
        "visited": sum(1 for o, _ in outcomes.values() if o in VISITED_OUTCOMES),
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
        return EXIT_OK
    except boskos_pool.Terminated as exc:
        # Every project reached has its line above, the one the signal landed
        # in included; the summary says how far the run got.
        report(outcomes)
        interrupted = sorted(p for p, (o, _) in outcomes.items() if o == OUTCOME_INTERRUPTED)
        message = "terminated (%s) after %d project(s)%s; held projects were released unless named above" % (
            exc, len(outcomes), "; interrupted in %s" % ", ".join(interrupted) if interrupted else ""
        )
        error.append(message)
        print("ERROR: %s" % message, file=sys.stderr)
        return boskos_pool.TERMINATED_EXIT_CODE
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
