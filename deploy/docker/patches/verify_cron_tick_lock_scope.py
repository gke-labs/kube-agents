#!/usr/bin/env python3
"""Build gate for the cron tick-lock-scope patch.

Run by deploy/docker/Dockerfile from /opt/hermes after
apply_cron_tick_lock_scope.py. The applier only proves twelve anchors matched;
it proves nothing about WHERE the release landed, and a release placed one
statement too early would silently break at-most-once.

Six checks, each a way the patch could match its anchors and still be wrong or
harmful:

1. RELEASE PLACEMENT. Parse the patched ``cron/scheduler.py::tick`` and assert
   the ``_tick_lock.release()`` statement sits inside the ``try``, strictly
   AFTER the ``advance_next_runs(...)`` call and after the last
   ``_submit_with_guard`` call, and strictly BEFORE the ``if sync:`` node.
   Releasing before the advance or before dispatch would reintroduce
   double-firing. The ``finally`` is also asserted to be the idempotent
   backstop and nothing else -- if it still touches ``lock_fd`` directly, the
   old wide-scope unlock survived alongside the new one.

2. THE CLAIM IS CARRIED, NOT LOOKED UP. Assert the dispatch guard binds
   ``_job_lock`` from ``_job_locks.claim(...)``, hands that object to
   ``_run_and_release`` as a default argument, releases through it, and that
   ``_job_locks.release(`` appears nowhere. Releasing by job id meant
   recomputing the lock path on a worker thread whose context can no longer
   resolve the profile -- see the "caller owns its claim" section of
   ``cron_tick_lock_scope.py``. The binding is also what keeps the lock object
   alive for the run's duration.

3. EVERY DISPATCH PATH CLAIMS THE FLOCK. Assert
   ``tools/cronjob_tools.py::_run_claimed_job`` claims the per-job flock before
   it calls ``run_one_job``, releases it in a ``finally`` -- the only path out
   that a BaseException cannot skip -- and that nothing else in the module
   calls ``run_one_job`` at all, which is what makes one guard cover all four
   of v2026.8.13's dispatch paths.

   This is where the flock lives *because* of that last assertion. Before the
   split it sat in ``_execute_job_now`` and ran strictly before the store CAS,
   so a refused claim cost nothing; now the CAS is one frame up and
   ``next_run_at`` is already advanced when the flock is refused. The trade is
   deliberate -- guarding the run body covers every caller, guarding one caller
   covers one -- and it is the trade upstream already makes for its own
   ``try_register_running_job``, which sits directly above the claim, after the
   same CAS. The checks pin both halves: the CAS stays with the caller and the
   caller takes no second flock.

4. THE SPAWNED TICK SWEEPS FIRST. Assert ``hermes_cli/cron.py::cron_tick``
   calls ``recover_interrupted_executions`` before ``tick``, and inside a
   ``try`` -- a bookkeeping sweep that raises must never cost the profile its
   tick.

5. HEAD-OF-LINE IS GONE, AND THE GUARD IS ARMED. Against a throwaway
   HERMES_HOME with one ``no_agent`` job whose script sleeps, run a real
   ``tick(sync=True)`` in a child process; once the ledger shows the job
   running, assert ``.tick.lock`` can be flock'd from this process (on the
   unpatched image it cannot -- that is the defect, stated as an assertion)
   and that ``.job-<id>.lock`` cannot. Then, with the child STILL ALIVE after
   its tick returns, assert the per-job lock is free: that proves the patched
   code released the claim rather than the kernel reaping it at exit, which is
   the half a dying process would hide.

6. A STUCK LEDGER ROW IS REAPED. Leave a ``running`` row behind from a child
   that exits without finishing it -- the live platform profile had six of
   these, the oldest a day old -- then run the patched ``cron_tick`` in another
   child and assert the row became ``unknown``.

Checks 5 and 6 are real cross-process exercises, not AST assertions: separate
interpreters hold the locks and own the ledger rows, and this process is the
second claimant. The kernel-releases-on-death property is pinned by the host
suite instead (``test_cron_tick_lock_scope.py``), because it needs a SIGKILL
that would make this file's child bookkeeping much harder to read.

Usage::

    cd /opt/hermes && python3 verify_cron_tick_lock_scope.py
"""

from __future__ import annotations

import ast
import fcntl
import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


def function_named(path: Path, name: str):
    """The named function node in ``path``, or None."""
    tree = ast.parse(path.read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    return tree, fn


def call_linenos(node, pred) -> list[int]:
    return [n.lineno for n in ast.walk(node) if isinstance(n, ast.Call) and pred(n)]


def named_call(name: str):
    """Matches ``name(...)`` however it is spelled -- bare or as an attribute."""
    return lambda n: (getattr(n.func, "id", None) == name
                      or getattr(n.func, "attr", None) == name)


# --- 1 + 2. cron/scheduler.py -----------------------------------------------
def check_release_placement() -> None:
    print("release placement (cron/scheduler.py::tick):")
    scheduler = HERMES / "cron" / "scheduler.py"
    tree, fn = function_named(scheduler, "tick")
    check("tick() is still a module-level function", fn is not None)
    if fn is None:
        return

    # tick() has TWO top-level Try nodes: the acquire's try/except, and the
    # try/finally that wraps the whole body. Only the second one is ours.
    tries = [n for n in fn.body if isinstance(n, ast.Try) and n.finalbody]
    check("tick() still has exactly one try/finally", len(tries) == 1,
          f"found {len(tries)}")
    if len(tries) != 1:
        return
    outer = tries[0]
    body = outer.body

    releases = [n.lineno for n in body
                if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "attr", None) == "release"
                and getattr(getattr(n.value.func, "value", None), "id", None) == "_tick_lock"]
    advances = call_linenos(outer, named_call("advance_next_runs"))
    submits = call_linenos(outer, named_call("_submit_with_guard"))
    syncs = [n.lineno for n in body
             if isinstance(n, ast.If) and getattr(n.test, "id", None) == "sync"]

    check("exactly one _tick_lock.release() in the try body", len(releases) == 1,
          f"found {len(releases)}")
    check("advance_next_runs still called under the lock", len(advances) == 1,
          f"found {len(advances)}")
    check("_submit_with_guard still called under the lock", len(submits) >= 1,
          f"found {len(submits)}")
    check("if sync: still in the try body", len(syncs) == 1, f"found {len(syncs)}")
    if releases and advances and submits and syncs:
        check("release is after advance_next_runs", releases[0] > advances[0],
              f"release at {releases[0]}, advance at {advances[0]}")
        check("release is after every dispatch", releases[0] > max(submits),
              f"release at {releases[0]}, last submit at {max(submits)}")
        check("release is before the sync wait", releases[0] < syncs[0],
              f"release at {releases[0]}, if sync: at {syncs[0]}")

    finally_src = "\n".join(ast.unparse(s) for s in outer.finalbody)
    check("finally still releases as a backstop",
          "_tick_lock.release()" in finally_src, f"finally body: {finally_src!r}")
    check("the old wide-scope unlock is gone from the finally",
          "lock_fd" not in finally_src,
          "the finally still unlocks lock_fd directly, so the narrowing did "
          f"not take: {finally_src!r}")

    # The registry the guard depends on has to exist at module scope, and it
    # must not reference a bare `msvcrt` -- that name is only bound inside the
    # ImportError branch of the fcntl import, so on Unix it does not exist.
    module_src = "\n".join(
        ast.unparse(n) for n in tree.body if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "_job_locks" for t in n.targets)
    )
    check("_job_locks is created at module scope", "JobLocks(" in module_src,
          f"found {module_src!r}")
    check("_job_locks does not reference a bare msvcrt",
          "globals().get('msvcrt')" in module_src
          or 'globals().get("msvcrt")' in module_src,
          f"found {module_src!r}")


def check_claim_is_carried() -> None:
    print("claim ownership (cron/scheduler.py dispatch guard):")
    scheduler = HERMES / "cron" / "scheduler.py"
    source = scheduler.read_text()
    _tree, fn = function_named(scheduler, "tick")
    if fn is None:
        check("tick() is still a module-level function", False)
        return
    src = ast.unparse(fn)

    check("the guard binds the claim it took",
          "_job_lock = _job_locks.claim(job_id)" in src,
          "the claim is not held in a local, so nothing keeps the lock object "
          "alive and the flock is dropped the moment it is garbage collected")
    check("a lost claim skips the job", "if _job_lock is None:" in src)

    # Scoped to the nested worker rather than searched for across tick(): the
    # tick lock's own `_tick_lock.release()` would satisfy a loose substring
    # match and the check would pass on an unpatched image.
    worker = next((n for n in ast.walk(fn) if isinstance(n, ast.FunctionDef)
                   and n.name == "_run_and_release"), None)
    check("_run_and_release still wraps the dispatch", worker is not None)
    if worker is not None:
        params = [a.arg for a in worker.args.args]
        defaults = [ast.unparse(d) for d in worker.args.defaults]
        check("the claim travels into the worker as a default argument",
              "lock" in params and "_job_lock" in defaults,
              f"params={params}, defaults={defaults} — the worker cannot "
              "resolve the store the claim was taken under, so it has to be "
              "handed the object")
        finals = "\n".join(ast.unparse(s) for t in worker.body
                           if isinstance(t, ast.Try) for s in t.finalbody)
        check("the worker releases through that object in its finally",
              "lock.release()" in finals,
              f"finally body: {finals!r}")

    check("nothing releases a claim by job id", "_job_locks.release(" not in source,
          "release-by-id recomputes the lock path from get_hermes_home(), whose "
          "override is a ContextVar the worker thread does not have — the pop "
          "misses and the job is suppressed for the gateway's whole life")


# --- 3. tools/cronjob_tools.py ----------------------------------------------
def check_dispatch_claims_the_flock() -> None:
    print("dispatch gate (tools/cronjob_tools.py::_run_claimed_job):")
    path = HERMES / "tools" / "cronjob_tools.py"
    tree, fn = function_named(path, "_run_claimed_job")
    check("_run_claimed_job still exists", fn is not None,
          "v2026.8.13 split the fire path into a claim half and a run half; "
          "the flock belongs to the run half because that is the one every "
          "dispatch path executes")
    if fn is None or tree is None:
        return

    def job_lock_claims(node) -> list[int]:
        return [n.lineno for n in ast.walk(node) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", None) == "claim"
                and getattr(getattr(n.func, "value", None), "id", None)
                == "_job_locks"]

    claims = job_lock_claims(fn)
    runs = call_linenos(fn, named_call("run_one_job"))
    check("it claims the per-job flock", len(claims) == 1, f"found {len(claims)}")
    check("it still runs the job", len(runs) == 1, f"found {len(runs)}")
    if claims and runs:
        check("the flock is claimed BEFORE the run", claims[0] < runs[0],
              f"claim at {claims[0]}, run_one_job at {runs[0]}")

    # Every dispatch path inherits the flock because every dispatch path runs
    # the job here. v2026.8.13 reaches this body from four call sites (the
    # synchronous tool, the background worker, the pool-rejection fallback and
    # the inline fallback); a fifth that called run_one_job directly would run
    # unguarded, and this is what would catch it.
    module_runs = call_linenos(tree, named_call("run_one_job"))
    check("nothing else in the module runs a job", len(module_runs) == 1,
          f"found {len(module_runs)} run_one_job call(s) — a dispatch path "
          "that does not go through _run_claimed_job takes no per-job flock")

    # The CAS now happens in the caller, one frame up, so a lost flock is
    # discovered after next_run_at has already advanced. That is a real cost
    # and it is deliberate: guarding _run_claimed_job covers all four dispatch
    # paths, guarding _execute_job_now covers one, and it is the same trade
    # upstream already makes for its own try_register_running_job — which sits
    # directly above the claim, after the same CAS. Asserted so the claim
    # cannot drift back into a single caller unnoticed.
    _tree2, outer = function_named(path, "_execute_job_now")
    check("_execute_job_now still exists", outer is not None)
    if outer is not None:
        check("the CAS stays with the caller",
              len(call_linenos(outer, named_call("claim_job_for_fire"))) == 1,
              "found none — the store CAS is what makes this a *claimed* job")
        check("and the caller takes no flock of its own",
              not job_lock_claims(outer),
              "two claims of one job on one thread is a self-deadlock, not "
              "a stronger guard")

    tries = [n for n in fn.body if isinstance(n, ast.Try) and n.finalbody]
    check("_run_claimed_job has exactly one try/finally", len(tries) == 1,
          f"found {len(tries)}")
    if len(tries) == 1:
        finally_src = "\n".join(ast.unparse(s) for s in tries[0].finalbody)
        check("the claim is released in the finally",
              "_run_lock.release()" in finally_src,
              "a return or a BaseException would leak the claim, and a leaked "
              f"claim silently suppresses the job: {finally_src!r}")


# --- 4. hermes_cli/cron.py ---------------------------------------------------
def check_spawned_tick_sweeps_first() -> None:
    print("recovery sweep (hermes_cli/cron.py::cron_tick):")
    path = HERMES / "hermes_cli" / "cron.py"
    _tree, fn = function_named(path, "cron_tick")
    check("cron_tick still exists", fn is not None)
    if fn is None:
        return

    sweeps = call_linenos(fn, named_call("recover_interrupted_executions"))
    ticks = call_linenos(fn, named_call("tick"))
    check("it sweeps interrupted executions", len(sweeps) == 1,
          f"found {len(sweeps)} — a spawned tick is this profile's entire "
          "scheduler lifecycle, so nothing else will ever reap a stuck row")
    check("it still ticks", len(ticks) == 1, f"found {len(ticks)}")
    if sweeps and ticks:
        check("the sweep runs before the tick", sweeps[0] < ticks[0],
              f"sweep at {sweeps[0]}, tick at {ticks[0]}")

    guarded = any(sweeps and t.lineno <= sweeps[0] <= (t.end_lineno or t.lineno)
                  for t in ast.walk(fn) if isinstance(t, ast.Try))
    check("the sweep is wrapped in a try", guarded,
          "bookkeeping that raises would cost the profile its tick")


# --- 5 + 6. behavioural ------------------------------------------------------
SLOW_SCRIPT = "import time\ntime.sleep(12)\nprint('slow job done')\n"

# The child stays alive after the tick returns on purpose: it lets the parent
# tell "the patch released the per-job claim" from "the process died and the
# kernel released it for us". Only the first is evidence the code is correct.
CHILD = (
    "import sys, time\n"
    "sys.path.insert(0, {root!r})\n"
    "from cron.scheduler import tick\n"
    "tick(verbose=False)\n"
    "print('TICK RETURNED', flush=True)\n"
    "time.sleep(60)\n"
)

# Exits without a terminal ledger state, exactly as the kanban worker processes
# that left six immortal `running` rows on the live platform profile did.
GHOST = (
    "import os, sys\n"
    "sys.path.insert(0, {root!r})\n"
    "from cron.executions import create_execution, mark_execution_running\n"
    "row = create_execution('ghost-job', source='direct')\n"
    "mark_execution_running(row['id'])\n"
    "print(row['id'], flush=True)\n"
    "os._exit(0)\n"
)

SWEEPER = (
    "import sys\n"
    "sys.path.insert(0, {root!r})\n"
    "from hermes_cli.cron import cron_tick\n"
    "cron_tick()\n"
)


def flockable(path: Path) -> bool:
    """Whether an exclusive non-blocking flock on ``path`` succeeds right now."""
    try:
        fh = open(path, "w", encoding="utf-8")
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    fcntl.flock(fh, fcntl.LOCK_UN)
    fh.close()
    return True


def flockable_within(path: Path, seconds: float = 3.0) -> bool:
    """``flockable``, retried — for the assertion that must eventually be true.

    ``create_execution`` runs inside ``_submit_with_guard``, several statements
    BEFORE ``_tick_lock.release()``. So the in-flight ledger row that wakes the
    poller below legitimately exists for a short window during which the tick
    lock is still held. A single shot that landed in that window would fail the
    docker build with "one slow job still starves the whole profile" — a scary
    and false diagnosis of a correctly patched image. Only this direction
    needs the retry; "the per-job lock IS held" must be true the instant the
    row appears, so that one stays a single shot.
    """
    deadline = time.monotonic() + seconds
    while True:
        if flockable(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def write_job_store(home: Path, jobs: list) -> None:
    (home / "cron").mkdir(parents=True, exist_ok=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))


def wait_for_line(log: Path, needle: str, child, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if log.is_file() and needle in log.read_text(errors="replace"):
            return True
        if child.poll() is not None:
            return log.is_file() and needle in log.read_text(errors="replace")
        time.sleep(0.2)
    return False


def check_behaviour() -> None:
    print("lock behaviour (a real second process holds the locks):")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        (home / "scripts").mkdir(parents=True)
        (home / "scripts" / "slow_job.py").write_text(SLOW_SCRIPT)
        write_job_store(home, [{
            "id": "slow-job", "name": "Slow Job",
            "schedule": {"kind": "interval", "minutes": 1, "display": "every 1m"},
            "prompt": "", "no_agent": True, "script": "slow_job.py", "skills": [],
            "enabled": True, "deliver": "local",
            "next_run_at": "2000-01-01T00:00:00+00:00", "state": "scheduled",
        }])

        env = dict(os.environ, HERMES_HOME=str(home))
        # The child's output is kept, not discarded: if the throwaway
        # HERMES_HOME fails to dispatch, this log is the only account of why,
        # and an opaque build failure here is expensive to diagnose.
        log = home / "child.log"
        child_log = open(log, "w", encoding="utf-8")
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD.format(root=str(HERMES))], env=env,
            stdout=child_log, stderr=subprocess.STDOUT)
        try:
            ledger = home / "cron" / "executions.db"
            deadline = time.monotonic() + 20
            running = False
            while time.monotonic() < deadline and not running:
                if ledger.is_file():
                    try:
                        con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
                        running = bool(list(con.execute(
                            "SELECT 1 FROM executions WHERE job_id='slow-job' "
                            "AND finished_at IS NULL")))
                        con.close()
                    except sqlite3.Error:
                        pass
                if not running:
                    if child.poll() is not None:
                        break  # the tick is over; no in-flight window to test
                    time.sleep(0.2)
            check("slow job reached the ledger as in-flight", running,
                  "the throwaway HERMES_HOME never dispatched — the harness "
                  "is broken, not the patch")
            if not running:
                child_log.close()
                print("--- child output ---")
                print(log.read_text()[-4000:] or "(empty)")
                print("--- end child output ---")
                return

            check("tick lock is free while a job runs (the defect)",
                  flockable_within(home / "cron" / ".tick.lock"),
                  "tick() is still holding .tick.lock across the as_completed "
                  "wait, so one slow job still starves the whole profile")
            check("per-job lock is held while the job runs (the guard)",
                  not flockable(home / "cron" / ".job-slow-job.lock"),
                  "nothing replaced the cross-process in-flight guard the wide "
                  "lock used to provide")

            returned = wait_for_line(log, "TICK RETURNED", child, 60)
            check("the child's tick returned", returned,
                  "the slow job never finished, so the release cannot be "
                  f"observed: {log.read_text()[-2000:]!r}")
            check("the claim is released by the code, not by the child dying",
                  returned and child.poll() is None
                  and flockable_within(home / "cron" / ".job-slow-job.lock"),
                  "the per-job claim outlived the run inside a process that is "
                  "still alive — _run_and_release did not release it, which is "
                  "the leak that suppresses the job on every later tick")
        finally:
            child.kill()
            child.wait(timeout=30)
            child_log.close()


def check_recovery_sweep() -> None:
    print("recovery sweep (a stuck row from a process that is gone):")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        write_job_store(home, [])
        env = dict(os.environ, HERMES_HOME=str(home))

        ghost = subprocess.run(
            [sys.executable, "-c", GHOST.format(root=str(HERMES))], env=env,
            capture_output=True, text=True, timeout=60)
        row_id = ghost.stdout.strip().splitlines()[-1] if ghost.stdout.strip() else ""
        check("a running row was left behind", bool(row_id),
              f"stdout={ghost.stdout!r} stderr={ghost.stderr[-2000:]!r}")
        if not row_id:
            return

        ledger = home / "cron" / "executions.db"

        def status() -> str:
            con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
            try:
                found = list(con.execute(
                    "SELECT status FROM executions WHERE id=?", (row_id,)))
            finally:
                con.close()
            return found[0][0] if found else ""

        check("the row starts out running", status() == "running",
              f"status={status()!r}")

        swept = subprocess.run(
            [sys.executable, "-c", SWEEPER.format(root=str(HERMES))], env=env,
            capture_output=True, text=True, timeout=120)
        check("the row was reaped as unknown", status() == "unknown",
              f"status={status()!r} — a spawned tick still never sweeps, so a "
              "run whose process died stays in flight forever and its failure "
              f"is invisible. sweeper stdout={swept.stdout[-2000:]!r} "
              f"stderr={swept.stderr[-2000:]!r}")


if __name__ == "__main__":
    print("verify_cron_tick_lock_scope")
    check_release_placement()
    check_claim_is_carried()
    check_dispatch_claims_the_flock()
    check_spawned_tick_sweeps_first()
    check_behaviour()
    check_recovery_sweep()
    print()
    if FAILURES:
        print(f"verify_cron_tick_lock_scope: {len(FAILURES)} FAILED")
        for f in FAILURES:
            print(f"  - {f}")
        raise SystemExit(1)
    print("verify_cron_tick_lock_scope: all checks passed")
