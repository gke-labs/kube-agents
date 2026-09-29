#!/usr/bin/env python3
"""Re-apply the seeded fleet stack in pool projects, from outside any run.

bench/tf/fleet plants the defects the presubmit's fixture scenarios assert on,
and the fleet drifts: GKE auto-upgrade heals seeded-b's version lag once its
maintenance exclusion lapses, a node repair leaves a fixture Pending, a cleanup
deletes the orphan disk. The stack's own README says the reconcile is a
re-apply on a schedule; this is that schedule's script.

Runs from two Prow periodics that execute only `main`, never a pull request's
code, as an identity of its own (docs/ci-pool-projects.md, section 6.2):
hourly against the projects the CI health bot's fixture-state scan reports
drifted, and weekly against every project, which is what rolls seeded-b's
exclusion forward. A presubmit runs the pull request's own scripts, so a
credential that can rewrite the fleet is never mounted there.

Each project is acquired from Boskos out of `free` into `reconciling` for the
minutes the apply takes and released back, so a project a run holds is never
applied under it and a run arriving mid-apply waits at its own acquire. The
plan is inspected before it is applied: only creates and in-place updates
are applied, and a plan with anything else (a destroy, a replace, a forget)
is refused and named, because nothing this stack declares should ever need
that on a re-apply, and a plan that does is a code change or an incident a
person should look at first.
"""

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
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
# The actions `tofu show -json` reports per resource. Only a create and an
# in-place update are applied; a delete, a replace (delete paired with a
# create, either order), a forget, or any action a later tofu adds is refused.
ACTION_NOOP = "no-op"
ACTION_READ = "read"
ACTION_CREATE = "create"
ACTION_UPDATE = "update"
IGNORED_ACTIONS = ([ACTION_NOOP], [ACTION_READ])
APPLIED_ACTIONS = ([ACTION_CREATE], [ACTION_UPDATE])

# Boskos: this script's hold state and owner. A project is held for one apply.
HOLD_STATE = "reconciling"
DEFAULT_OWNER = "ci-kube-agents-fleet-reconcile"
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
# periodic's grace_period must exceed this.
INTERRUPT_GRACE_SECONDS = 120
TERMINATION_SIGNALS = boskos_pool.TERMINATION_SIGNALS

# Where the CI health bot publishes its scan (docs/ci-health.md, "The
# seeded-fleet scan"); `--drifted` applies the projects it lists.
DEFAULT_FIXTURE_STATE = "gs://kube-agents-dashboards/evals/fixture-state.json"
GCS_PREFIX = "gs://"
GCLOUD_TIMEOUT_SECONDS = 60

OUTCOME_APPLIED = "applied"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"
OUTCOME_BUSY = "busy"
OUTCOME_PLANNED = "planned"
# The project a termination landed in: its line names it, because the
# recovery for an apply killed past its grace is against that project's state.
OUTCOME_INTERRUPTED = "interrupted"
# The outcomes that red the job: a project whose fleet is not what the stack
# declares and stays that way. Busy is not one; the next run gets it.
FAILING_OUTCOMES = frozenset({OUTCOME_REFUSED, OUTCOME_FAILED})
OUTPUT_TAIL_CHARS = 600
REASON_UNMAPPED = "not a mapped pool project (gitops_repo_for_project in hack/ci-deploy.sh)"
# Boskos's 404 does not say which; a mapped project lands here between its
# mapping row and its Boskos registration, and reads busy until registered.
REASON_BUSY = "not free in Boskos, or not registered there yet"
REASON_INTERRUPTED = "terminated (%s) while tofu ran; an apply cut past its grace leaves the state locked: tofu force-unlock"
REASON_CEILING = "did not finish within %ds; tofu was interrupted, and killed if it did not stop within %ds, which leaves the state locked: tofu force-unlock"

EXIT_OK = 0
EXIT_FAILED = 1


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
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


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
    timeout = max(1, deadline - time.monotonic())
    result = runner([TOFU] + list(args), cwd=str(FLEET_DIR), timeout=timeout, capture_output=True, text=True)
    if result.returncode not in ok:
        raise ReconcileError("tofu %s exited %d: %s" % (args[0], result.returncode, _tail(result.stderr or result.stdout)))
    return result


def plan_changes(show_json):
    """[(actions, address)] from `tofu show -json`, no-ops and reads dropped."""
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
            changes.append((actions, change.get("address") or "?"))
    return changes


def refused_changes(changes):
    """The changes a re-apply must not make: everything but a create or an in-place update."""
    return ["%s %s" % ("+".join(actions), address) for actions, address in changes if actions not in APPLIED_ACTIONS]


def describe(changes):
    add = sum(1 for actions, _ in changes if actions == [ACTION_CREATE])
    change = sum(1 for actions, _ in changes if actions == [ACTION_UPDATE])
    refused = sum(1 for actions, _ in changes if actions not in APPLIED_ACTIONS)
    return "%d to add, %d to change, %d refused" % (add, change, refused)


def reconcile_project(project, runner=tofu_runner, dry_run=False, timeout=PROJECT_TIMEOUT_SECONDS):
    """init, plan, inspect, apply, under one deadline. Returns (outcome, detail)."""
    deadline = time.monotonic() + timeout
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
                return OUTCOME_UNCHANGED, "nothing to apply"
            shown = _tofu(["show", "-json", plan_path], runner, deadline)
            changes = plan_changes(shown.stdout)
            summary = describe(changes)
            refused = refused_changes(changes)
            if refused:
                return OUTCOME_REFUSED, "%s; not a create or an in-place update: %s" % (summary, ", ".join(refused))
            if dry_run:
                return OUTCOME_PLANNED, "%s: %s" % (summary, ", ".join("%s %s" % ("+".join(a), addr) for a, addr in changes))
            _tofu(["apply", "-input=false", "-no-color", "-auto-approve", plan_path], runner, deadline)
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


def _line(project, outcome):
    """One project's result, printed as it happens: a run that is terminated
    or loses Boskos mid-walk has still named every project it reached."""
    print("%s: %s (%s)" % (project, outcome[0], outcome[1]), flush=True)


def _record(project, outcomes, runner, dry_run):
    """reconcile_project with its outcome recorded and printed before the
    caller sees it, the project a termination landed in included."""
    try:
        outcomes[project] = reconcile_project(project, runner=runner, dry_run=dry_run)
    except boskos_pool.Terminated as exc:
        outcomes[project] = (OUTCOME_INTERRUPTED, REASON_INTERRUPTED % exc)
        _line(project, outcomes[project])
        raise
    _line(project, outcomes[project])


def report(outcomes):
    failing = sorted(p for p, (outcome, _) in outcomes.items() if outcome in FAILING_OUTCOMES)
    print(
        "reconciled %d project(s): %d applied, %d unchanged, %d planned, %d busy, %d refused or failed, %d interrupted"
        % (
            len(outcomes),
            sum(1 for o, _ in outcomes.values() if o == OUTCOME_APPLIED),
            sum(1 for o, _ in outcomes.values() if o == OUTCOME_UNCHANGED),
            sum(1 for o, _ in outcomes.values() if o == OUTCOME_PLANNED),
            sum(1 for o, _ in outcomes.values() if o == OUTCOME_BUSY),
            len(failing),
            sum(1 for o, _ in outcomes.values() if o == OUTCOME_INTERRUPTED),
        )
    )
    return failing


def reconcile_named(projects, server, owner, lease=True, runner=tofu_runner, dry_run=False, known=None, outcomes=None):
    """Each named project, held through Boskos unless `lease` is off.

    A name outside the pool mapping is failed before Boskos is asked: Boskos
    answers 404 for a leased project and for one it has never heard of alike,
    so a typo would otherwise read as busy on every run. Without a lease the
    mapping is not consulted; that is the dev-project path.
    """
    # The mapping is read only when Boskos will be asked: the dev-project
    # path has no use for it and must not fail on it.
    known = pool_projects() if known is None and lease else known
    outcomes = {} if outcomes is None else outcomes
    for project in projects:
        if not lease:
            _record(project, outcomes, runner, dry_run)
            continue
        if project not in known:
            outcomes[project] = (OUTCOME_FAILED, REASON_UNMAPPED)
            _line(project, outcomes[project])
            continue
        release_failures = {}

        def visit(p):
            # Recorded and printed inside the hold, so a termination that lands
            # during the release still leaves this project's apply on record.
            _record(p, outcomes, runner, dry_run)

        outcome = boskos_pool.acquire_and_hold(
            server,
            owner,
            HOLD_STATE,
            lambda project=project: boskos_pool.acquire(server, owner, HOLD_STATE, name=project),
            visit,
            release_failures,
            heartbeat=True,
        )
        if outcome is boskos_pool.NOT_ACQUIRED:
            outcomes[project] = (OUTCOME_BUSY, REASON_BUSY)
            _line(project, outcomes[project])
        elif project in release_failures:
            outcomes[project] = (OUTCOME_FAILED, release_failures[project])
            _line(project, outcomes[project])
    return outcomes


def reconcile_pool(server, owner, size, runner=tofu_runner, dry_run=False, known=None, outcomes=None):
    """Every project Boskos hands out as free, once each. A hand-out outside
    the pool mapping is released untouched and reported, as the sweep does:
    a project registered in Boskos before its mapping row, or left registered
    after it, is not one to apply the fleet in."""
    known = pool_projects() if known is None else known
    outcomes = {} if outcomes is None else outcomes

    def visit(project):
        if project not in known:
            outcomes[project] = (OUTCOME_FAILED, REASON_UNMAPPED)
            _line(project, outcomes[project])
        else:
            _record(project, outcomes, runner, dry_run)

    _, release_failures = boskos_pool.walk(server, owner, HOLD_STATE, size, visit, heartbeat=True)
    for project, reason in release_failures.items():
        outcomes[project] = (OUTCOME_FAILED, reason)
        _line(project, outcomes[project])
    return outcomes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true", help="every project Boskos reports free")
    mode.add_argument("--drifted", action="store_true", help="the projects the fixture-state scan reports drifted")
    mode.add_argument("--project", action="append", help="one project (repeatable)")
    parser.add_argument("--fixture-state", default=DEFAULT_FIXTURE_STATE, help="with --drifted: the scan, gs:// or a path")
    parser.add_argument("--no-lease", action="store_true", help="with --project: do not ask Boskos (a dev project, or a lease you hold)")
    parser.add_argument("--dry-run", action="store_true", help="plan and inspect, apply nothing")
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
    for sig in TERMINATION_SIGNALS:
        signal.signal(sig, boskos_pool.terminate)
    outcomes = {}
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
            )
        else:
            boskos_pool.reset_stranded(args.boskos_server, HOLD_STATE, STRANDED_AFTER, "reconcile")
            if args.drifted:
                projects = drifted_projects(load_fixture_state(args.fixture_state))
                print("%d project(s) drifted in %s" % (len(projects), args.fixture_state))
                reconcile_named(
                    projects, args.boskos_server, args.boskos_owner, runner=tofu_runner, dry_run=args.dry_run, outcomes=outcomes
                )
            else:
                known = pool_projects()
                reconcile_pool(
                    args.boskos_server, args.boskos_owner, len(known), runner=tofu_runner, dry_run=args.dry_run, known=known, outcomes=outcomes
                )
        failing = report(outcomes)
        if failing:
            print("ERROR: %d project(s) not reconciled: %s" % (len(failing), ", ".join(failing)), file=sys.stderr)
            return EXIT_FAILED
        return EXIT_OK
    except boskos_pool.Terminated as exc:
        # Every project reached has its line above, the one the signal landed
        # in included; the summary says how far the run got.
        report(outcomes)
        interrupted = sorted(p for p, (o, _) in outcomes.items() if o == OUTCOME_INTERRUPTED)
        print(
            "ERROR: terminated (%s) after %d project(s)%s; held projects were released"
            % (exc, len(outcomes), "; interrupted in %s" % ", ".join(interrupted) if interrupted else ""),
            file=sys.stderr,
        )
        return boskos_pool.TERMINATED_EXIT_CODE
    except (ReconcileError, boskos_pool.BoskosError) as exc:
        report(outcomes)
        print("ERROR: %s" % exc, file=sys.stderr)
        return EXIT_FAILED
    except subprocess.SubprocessError as exc:
        report(outcomes)
        print("ERROR: could not run a command (%s: %s)" % (type(exc).__name__, exc), file=sys.stderr)
        return EXIT_FAILED
    except boskos_pool.REACH_ERRORS as exc:
        report(outcomes)
        print("ERROR: could not reach a service (%s: %s)" % (type(exc).__name__, exc), file=sys.stderr)
        return EXIT_FAILED

if __name__ == "__main__":
    sys.exit(main())
