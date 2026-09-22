#!/usr/bin/env python3
"""Wire tools/cron_tick_lock_scope.py into the Hermes source tree.

Twelve anchored edits across two files -- eight in ``cron/scheduler.py``, four
in ``tools/cronjob_tools.py``. See the module docstring in
``deploy/docker/patches/cron_tick_lock_scope.py`` for what each group is for.
Usage::

    python3 apply_cron_tick_lock_scope.py [HERMES_ROOT]   # default /opt/hermes

Must run AFTER apply_cron_run_scope.py: every anchor below was verified against
the post-cron_run_scope source in the running image, count == 1 for each.

Not idempotent, deliberately. A second run raises SystemExit because every
anchor has been consumed by the first -- that is the intended signal that the
build is applying the same surgery twice.

Anchors are derived against v2026.9.14, which lifted the dispatch guard out of
``tick`` into a module-level ``_submit_with_guard`` and the lock handling into
``_acquire_tick_lock`` / ``_release_tick_lock``. Two of the fourteen edits this
applier used to make are retired there, because upstream now does what they
did; the sections that held them say which and why.
"""

import sys
from pathlib import Path

import patchlib

# --- 1. import + the per-job lock registry ----------------------------------
# `msvcrt` is only bound in the ImportError branch of the fcntl import, so on
# Unix the NAME does not exist. globals().get() rather than a bare reference.
IMPORT_ANCHOR = "def _get_lock_paths() -> tuple[Path, Path]:"
IMPORT_PATCHED = (
    "# kube-agents patch: see tools/cron_tick_lock_scope.py\n"
    "from tools.cron_tick_lock_scope import AdvisoryLock, JobLocks\n"
    "\n"
    "# Cross-process mirror of _running_job_ids. flock, not a ledger row: the\n"
    "# kernel releases the claim when a tick process dies, so it cannot wedge.\n"
    "_job_locks = JobLocks(\n"
    "    lambda: _get_lock_paths()[0], fcntl, globals().get(\"msvcrt\")\n"
    ")\n"
    "\n"
    "\n"
    "def _get_lock_paths() -> tuple[Path, Path]:"
)

# --- 2. own the tick lock's handle ------------------------------------------
# v2026.9.14 moved the acquire into _acquire_tick_lock(), which returns the
# locked handle or None on contention (and re-raises fd exhaustion, #87644).
# Reaching the `try:` still means the same thing it always did -- this process
# holds the lock -- so the handle is wrapped right there.
ACQUIRE_ANCHOR = (
    "    lock_fd = _acquire_tick_lock(lock_file)\n"
    "    if lock_fd is None:\n"
    "        return 0\n"
    "\n"
    "    try:\n"
)
ACQUIRE_PATCHED = (
    "    lock_fd = _acquire_tick_lock(lock_file)\n"
    "    if lock_fd is None:\n"
    "        return 0\n"
    "\n"
    "    # kube-agents patch: the tick lock guards the scheduling decision, not\n"
    "    # job execution. See tools/cron_tick_lock_scope.py.\n"
    '    _tick_lock = AdvisoryLock(lock_fd, fcntl, globals().get("msvcrt"))\n'
    "\n"
    "    try:\n"
)

# --- 3. release once dispatch is done, before the sync wait -----------------
RELEASE_ANCHOR = (
    "        if sync:\n"
    "            for f in concurrent.futures.as_completed(_all_futures):\n"
)
RELEASE_PATCHED = (
    "        # kube-agents patch: every due job's next_run_at has been advanced\n"
    "        # and every job has been submitted by this point, so at-most-once is\n"
    "        # already secured. Releasing here instead of in the finally stops one\n"
    "        # slow job blocking the whole profile for its entire runtime.\n"
    "        # See tools/cron_tick_lock_scope.py.\n"
    "        _tick_lock.release()\n"
    "\n"
    "        if sync:\n"
    "            for f in concurrent.futures.as_completed(_all_futures):\n"
)

# --- 4. the finally becomes the idempotent backstop -------------------------
# The whole release, not a second one beside upstream's: _release_tick_lock
# flocks the handle before closing it, and AdvisoryLock.release() has already
# closed it on the dispatch path above, so calling both would raise ValueError
# out of the finally on every tick that dispatched anything.
FINALLY_ANCHOR = (
    "    finally:\n"
    "        _release_tick_lock(lock_fd)\n"
)
FINALLY_PATCHED = (
    "    finally:\n"
    "        # kube-agents patch: idempotent — a no-op when the dispatch path\n"
    "        # already released, and still the only release on the early-return\n"
    "        # and exception paths. See tools/cron_tick_lock_scope.py.\n"
    "        _tick_lock.release()\n"
)

# --- 5. claim the per-job lock beside the process-local guard ---------------
# The claim object travels into the worker as a default argument rather than
# being looked up again by job id on the way out: the worker thread cannot
# resolve the lock path the claim was taken under. See the "caller owns its
# claim" section of tools/cron_tick_lock_scope.py.
#
# v2026.8.13 lifted the inline ``with _running_lock:`` guard this used to sit
# beside into try_register_running_job()/release_running_job(), so that a
# manual dispatch could take the same in-flight claim; v2026.9.14 lifted the
# whole guard out of tick into the module-level _submit_with_guard(). Nothing
# about the reasoning changed: that set is still process-local, and the flock
# below is still the only thing a second `hermes cron tick` process can see.
GUARD_ANCHOR = (
    "    if not try_register_running_job(job_id):\n"
    "        logger.info(\"Job '%s' already running — skipping\", job_label)\n"
    "        return None\n"
    "    # Record the attempt before dispatch; recovery marks abandoned rows unknown (no retry).\n"
)
GUARD_PATCHED = (
    "    if not try_register_running_job(job_id):\n"
    "        logger.info(\"Job '%s' already running — skipping\", job_label)\n"
    "        return None\n"
    "    # kube-agents patch: the set try_register_running_job guards is\n"
    "    # module-level, so it is empty in every freshly spawned\n"
    "    # `hermes cron tick`. With the tick lock now released at dispatch, a\n"
    "    # second process can reach here for the same job if that job\n"
    "    # outlives its own period. Mirror the claim with a per-job flock the\n"
    "    # kernel releases on process death.\n"
    "    # See tools/cron_tick_lock_scope.py.\n"
    "    _job_lock = _job_locks.claim(job_id)\n"
    "    if _job_lock is None:\n"
    "        logger.info(\n"
    "            \"Job '%s' already running in another process — skipping\",\n"
    "            job_label,\n"
    "        )\n"
    "        release_running_job(job_id)\n"
    "        return None\n"
    "    # Record the attempt before dispatch; recovery marks abandoned rows unknown (no retry).\n"
)

# --- 5b. release it when execution creation fails ---------------------------
# v2026.8.19 wrapped create_execution()/dispatched_job/_ctx in a try/except that
# releases the in-flight claim and returns None when execution creation fails.
# That is a third way out of this block between the claim above and the worker
# that releases it, so the flock has to go with the claim or a failed
# create_execution wedges the job until the process dies.
#
# `logger.exception(` is what tells this handler apart from the submit-failure
# one below: both open with the same two calls.
EXECUTION_ERR_ANCHOR = (
    "        release_running_job(job_id)\n"
    "        _clear_run_claim_best_effort()\n"
    "        logger.exception(\n"
)
EXECUTION_ERR_PATCHED = (
    "        release_running_job(job_id)\n"
    "        _job_lock.release()\n"
    "        _clear_run_claim_best_effort()\n"
    "        logger.exception(\n"
)

# --- 5c. the worker owns the claim for the run's duration -------------------
RUN_AND_RELEASE_ANCHOR = (
    "    def _run_and_release(j=dispatched_job, ctx=_ctx):\n"
    "        try:\n"
    "            return ctx.run(process_job, j)\n"
    "        finally:\n"
    '            release_running_job(j["id"])\n'
)
RUN_AND_RELEASE_PATCHED = (
    "    def _run_and_release(j=dispatched_job, ctx=_ctx, lock=_job_lock):\n"
    "        try:\n"
    "            return ctx.run(process_job, j)\n"
    "        finally:\n"
    '            release_running_job(j["id"])\n'
    "            lock.release()\n"
)

# --- 6. release it on the dispatch-failure path too -------------------------
SUBMIT_ERR_ANCHOR = (
    "    except Exception as submit_err:\n"
    "        release_running_job(job_id)\n"
)
SUBMIT_ERR_PATCHED = (
    "    except Exception as submit_err:\n"
    "        release_running_job(job_id)\n"
    "        _job_lock.release()\n"
)

# --- 7. _execute_job_now's docstring said the CAS was the guard -------------
# v2026.9.14 compacted the docstring but kept the claim it makes about what the
# claim does ("so a concurrent tick cannot double-fire"), so the correction is
# still owed. It is load-bearing prose: it is why nobody looked for the overlap.
# Its other half, "next_run_at advances", is right and stays: the manual claim
# (cron/jobs.py, claim_job_for_fire(manual=True)) stamps the fire claim and
# rewrites a recurring job's next_run_at to compute_next_run(schedule, now);
# what ``manual`` withholds is the occurrence stamp, nothing else.
DISPATCH_DOC_ANCHOR = (
    '    """Run a job now, outside the scheduler tick: claim via ``claim_job_for_fire`` (the ticker\'s\n'
    "    CAS, so a concurrent tick cannot double-fire and next_run_at advances), then fire through\n"
    '    the shared ``run_one_job`` body. Returns {"claimed", "success", "error"}."""\n'
)
DISPATCH_DOC_PATCHED = (
    '    """Run a job now, outside the scheduler tick: claim via ``claim_job_for_fire`` (the ticker\'s\n'
    "    CAS: it stamps the fire claim, so a second claim inside the claim TTL loses, and re-anchors\n"
    "    a recurring job's next_run_at from now -- the same slot for a cron expression, now plus one\n"
    "    period for an interval -- but stamps no occurrence identity, because a manual run is not\n"
    "    the pending slot), then fire through the shared ``run_one_job`` body. What the CAS never\n"
    "    did is block a *concurrent* tick; ``_run_claimed_job`` takes a per-job flock for that --\n"
    "    see the kube-agents patch note there, and ``tools/cron_tick_lock_scope.py``.\n"
    '    Returns {"claimed", "success", "error"}."""\n'
)

# --- 8. a dispatched run claims the same lock the ticker does ---------------
# In _run_claimed_job rather than _execute_job_now, which is where this used to
# live. v2026.8.13 split the claim from the run so a background dispatch could
# take the CAS synchronously and hand the run to a daemon worker, and there are
# now four call sites for the run half — the flock has to be where the run is or
# three of them go unguarded.
#
# That does put it after claim_job_for_fire has run. Since v2026.9.11 every
# claim on this path is a ``manual`` one (cron/jobs.py, claim_job_for_fire):
# it stamps the fire claim and re-anchors a recurring job's next_run_at from
# now -- the same slot for a cron expression, now plus one period for an
# interval -- but stamps no occurrence identity, so no execution row can make
# the scheduler treat the pending slot as already done. A refusal here
# therefore costs the requested run itself, not a scheduled occurrence; that
# is the same trade upstream makes for its own try_register_running_job()
# guard two lines above, and a far smaller harm than two overlapping runs
# sharing one output file, which is what this patch exists to stop.
DISPATCH_CLAIM_ANCHOR = (
    "        _registered = True\n"
)
DISPATCH_CLAIM_PATCHED = (
    "        _registered = True\n"
    "        # kube-agents patch: a dispatched run overlapped a scheduled one, and\n"
    "        # two runs of one fleet audit share the job's output file and scratch\n"
    "        # state. The register above cannot see a run in another process — the\n"
    "        # platform profile ticks by spawning `hermes cron tick` — so take the\n"
    "        # same per-job flock that tick takes, for the whole run.\n"
    "        # See tools/cron_tick_lock_scope.py.\n"
    "        _run_lock = _job_locks.claim(job_id)\n"
    "        if _run_lock is None:\n"
    "            _registered = False\n"
    "            release_running_job(job_id)\n"
    "            return {\n"
    '                "claimed": True,\n'
    '                "success": False,\n'
    '                "error": (\n'
    '                    "Job is already running in another process — a scheduled "\n'
    '                    "tick or another dispatch holds it, and a second copy "\n'
    "                    \"would share this run's output file and scratch state. \"\n"
    '                    "Not started. The run in flight records its own result; "\n'
    '                    "check it with `hermes cron runs <job_id>` before "\n'
    '                    "dispatching again."\n'
    "                ),\n"
    "            }\n"
)

# The import the claim above needs, and the local that keeps the release below
# safe on the paths that never reach the claim.
DISPATCH_IMPORT_ANCHOR = (
    "    _registered = False\n"
    "    fire_owner = None\n"
    "    try:\n"
    "        from cron.scheduler import release_running_job, run_one_job, try_register_running_job\n"
)
DISPATCH_IMPORT_PATCHED = (
    "    _registered = False\n"
    "    fire_owner = None\n"
    "    # kube-agents patch: see below, and tools/cron_tick_lock_scope.py.\n"
    "    _run_lock = None\n"
    "    try:\n"
    "        from cron.scheduler import (\n"
    "            _job_locks,\n"
    "            release_running_job,\n"
    "            run_one_job,\n"
    "            try_register_running_job,\n"
    "        )\n"
)

# --- 9. release it on every path out of _run_claimed_job --------------------
# The tail of the handler is the anchor: the ``mark_job_run`` call with
# ``expected_fire_owner`` is the one _claim_for_manual_run's own handler does
# not make, and it is what tells the two apart. ``finally`` goes after the
# ``except``, which is why this appends to the end of the handler rather than
# opening a clause before it.
DISPATCH_RELEASE_ANCHOR = (
    "        with contextlib.suppress(Exception):\n"
    "            mark_job_run(job_id, False, str(e), expected_fire_owner=fire_owner)\n"
    '        return {"claimed": True, "success": False, "error": str(e)}\n'
)
DISPATCH_RELEASE_PATCHED = DISPATCH_RELEASE_ANCHOR + (
    "    finally:\n"
    "        # kube-agents patch: every path out, including the BaseException the\n"
    "        # except above does not catch. The claim was taken on this thread and\n"
    "        # is released on it, and AdvisoryLock.release() is idempotent.\n"
    "        # See tools/cron_tick_lock_scope.py.\n"
    "        if _run_lock is not None:\n"
    "            _run_lock.release()\n"
)

# --- 11. (retired at v2026.9.14) the comment that said the CAS was enough ----
# The `cronjob(action='run')` comment this edit corrected -- "the claim ...
# blocks a concurrent tick from double-firing" -- was dropped when upstream
# split the tool into per-action helpers; _action_run now says only that a
# manual run must actually run. The one place the false claim survives is
# _execute_job_now's docstring, which edit 7 corrects.

# --- 12. (retired at v2026.9.14) a spawned tick sweeps first ----------------
# This applier used to make hermes_cli/cron.py::cron_tick call
# recover_interrupted_executions() before tick(), because the sweep ran only
# from the two gateway-ticker lifecycles and the platform profile -- ticked by
# spawning this CLI -- had never once reaped an abandoned attempt. Upstream
# closed that in the run-up to v2026.9.14 (#86721): tick() now calls
# _maybe_reap_dead_owners(), which runs the same sweep behind a 300s throttle
# held in a module global -- unset in every freshly spawned process, so a
# spawned tick sweeps every time -- and _try_dispatch_background_run reaps
# before every manual dispatch as well. verify_cron_tick_lock_scope.py still
# asserts both the wiring and the behaviour, so a future upstream change that
# drops the sweep fails the build rather than quietly re-opening the gap.

PATCHES = (
    (
        "cron/scheduler.py",
        (
            (IMPORT_ANCHOR, IMPORT_PATCHED, 1),
            (ACQUIRE_ANCHOR, ACQUIRE_PATCHED, 1),
            (RELEASE_ANCHOR, RELEASE_PATCHED, 1),
            (FINALLY_ANCHOR, FINALLY_PATCHED, 1),
            (GUARD_ANCHOR, GUARD_PATCHED, 1),
            (EXECUTION_ERR_ANCHOR, EXECUTION_ERR_PATCHED, 1),
            (RUN_AND_RELEASE_ANCHOR, RUN_AND_RELEASE_PATCHED, 1),
            (SUBMIT_ERR_ANCHOR, SUBMIT_ERR_PATCHED, 1),
        ),
    ),
    (
        "tools/cronjob_tools.py",
        (
            (DISPATCH_DOC_ANCHOR, DISPATCH_DOC_PATCHED, 1),
            (DISPATCH_IMPORT_ANCHOR, DISPATCH_IMPORT_PATCHED, 1),
            (DISPATCH_CLAIM_ANCHOR, DISPATCH_CLAIM_PATCHED, 1),
            (DISPATCH_RELEASE_ANCHOR, DISPATCH_RELEASE_PATCHED, 1),
        ),
    ),
)


def apply(root: Path) -> None:
    for relative, edits in PATCHES:
        patch = patchlib.Patch(root, relative, prefix="cron_tick_lock_scope")
        for anchor, replacement, expected in edits:
            patch.substitute(anchor, replacement, expected=expected)
        patch.commit(f"{len(edits)} anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
