#!/usr/bin/env python3
"""Wire tools/cron_tick_lock_scope.py into the Hermes source tree.

Twelve anchored edits across three files -- six in ``cron/scheduler.py``, five
in ``tools/cronjob_tools.py``, one in ``hermes_cli/cron.py``. See the module
docstring in ``deploy/docker/patches/cron_tick_lock_scope.py`` for what each
group is for. Usage::

    python3 apply_cron_tick_lock_scope.py [HERMES_ROOT]   # default /opt/hermes

Must run AFTER apply_cron_run_scope.py: every anchor below was verified against
the post-cron_run_scope source in the running image, count == 1 for each.

Not idempotent, deliberately. A second run raises SystemExit because every
anchor has been consumed by the first -- that is the intended signal that the
build is applying the same surgery twice.
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
ACQUIRE_ANCHOR = (
    "    except (OSError, IOError):\n"
    '        logger.debug("Tick skipped — another instance holds the lock")\n'
    "        if lock_fd is not None:\n"
    "            lock_fd.close()\n"
    "        return 0\n"
    "\n"
    "    try:\n"
)
ACQUIRE_PATCHED = (
    "    except (OSError, IOError):\n"
    '        logger.debug("Tick skipped — another instance holds the lock")\n'
    "        if lock_fd is not None:\n"
    "            lock_fd.close()\n"
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
    "            # Sync mode (tests / manual ticks): wait for all dispatched jobs,\n"
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
    "            # Sync mode (tests / manual ticks): wait for all dispatched jobs,\n"
)

# --- 4. the finally becomes the idempotent backstop -------------------------
FINALLY_ANCHOR = (
    "    finally:\n"
    "        if fcntl:\n"
    "            try:\n"
    "                fcntl.flock(lock_fd, fcntl.LOCK_UN)\n"
    "            except (OSError, IOError):\n"
    "                pass\n"
    "        elif msvcrt:\n"
    "            try:\n"
    "                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)\n"
    "            except (OSError, IOError):\n"
    "                pass\n"
    "        lock_fd.close()\n"
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
# manual dispatch could take the same in-flight claim. Nothing about the
# reasoning changed: that set is still process-local, and the flock below is
# still the only thing a second `hermes cron tick` process can see.
GUARD_ANCHOR = (
    "            if not try_register_running_job(job_id):\n"
    "                logger.info(\"Job '%s' already running — skipping\", job.get(\"name\", job_id))\n"
    "                return None\n"
    "            # Record the attempt before executor dispatch. Recovery classifies\n"
    "            # abandoned records as unknown; it never automatically retries them.\n"
    '            execution = create_execution(job_id, source="builtin")\n'
    '            dispatched_job = dict(job, execution_id=execution["id"])\n'
    "            _ctx = contextvars.copy_context()\n"
    "\n"
    "            def _run_and_release(j=dispatched_job, ctx=_ctx):\n"
    "                try:\n"
    "                    return ctx.run(_process_job, j)\n"
    "                finally:\n"
    '                    release_running_job(j["id"])\n'
)
GUARD_PATCHED = (
    "            if not try_register_running_job(job_id):\n"
    "                logger.info(\"Job '%s' already running — skipping\", job.get(\"name\", job_id))\n"
    "                return None\n"
    "            # kube-agents patch: the set try_register_running_job guards is\n"
    "            # module-level, so it is empty in every freshly spawned\n"
    "            # `hermes cron tick`. With the tick lock now released at dispatch, a\n"
    "            # second process can reach here for the same job if that job\n"
    "            # outlives its own period. Mirror the claim with a per-job flock the\n"
    "            # kernel releases on process death.\n"
    "            # See tools/cron_tick_lock_scope.py.\n"
    "            _job_lock = _job_locks.claim(job_id)\n"
    "            if _job_lock is None:\n"
    "                logger.info(\n"
    "                    \"Job '%s' already running in another process — skipping\",\n"
    '                    job.get("name", job_id),\n'
    "                )\n"
    "                release_running_job(job_id)\n"
    "                return None\n"
    "            # Record the attempt before executor dispatch. Recovery classifies\n"
    "            # abandoned records as unknown; it never automatically retries them.\n"
    '            execution = create_execution(job_id, source="builtin")\n'
    '            dispatched_job = dict(job, execution_id=execution["id"])\n'
    "            _ctx = contextvars.copy_context()\n"
    "\n"
    "            def _run_and_release(j=dispatched_job, ctx=_ctx, lock=_job_lock):\n"
    "                try:\n"
    "                    return ctx.run(_process_job, j)\n"
    "                finally:\n"
    '                    release_running_job(j["id"])\n'
    "                    lock.release()\n"
)

# --- 6. release it on the dispatch-failure path too -------------------------
SUBMIT_ERR_ANCHOR = (
    "            except Exception as submit_err:\n"
    "                release_running_job(job_id)\n"
)
SUBMIT_ERR_PATCHED = (
    "            except Exception as submit_err:\n"
    "                release_running_job(job_id)\n"
    "                _job_lock.release()\n"
)

# --- 7. _execute_job_now's docstring said the CAS was the guard -------------
DISPATCH_DOC_ANCHOR = (
    "    Atomically claims the job first via ``claim_job_for_fire`` — the same\n"
    "    at-most-once CAS the scheduler/external-provider fire path uses — so a\n"
    "    concurrently-running gateway ticker cannot also fire it (the claim both\n"
    "    blocks a duplicate fire and advances ``next_run_at`` for recurring jobs).\n"
    "    If the claim is lost (another fire is in flight), this is a no-op.\n"
)
DISPATCH_DOC_PATCHED = (
    "    Atomically claims the job first via ``claim_job_for_fire`` — the same\n"
    "    at-most-once CAS the scheduler/external-provider fire path uses, which\n"
    "    advances ``next_run_at`` for recurring jobs and settles a race between two\n"
    "    fires of the same occurrence. If the claim is lost (another fire is in\n"
    "    flight), this is a no-op. What the CAS never did is block a *concurrent*\n"
    "    tick; ``_run_claimed_job`` takes a per-job flock for that — see the\n"
    "    kube-agents patch note there, and ``tools/cron_tick_lock_scope.py``.\n"
)

# --- 8. a dispatched run claims the same lock the ticker does ---------------
# In _run_claimed_job rather than _execute_job_now, which is where this used to
# live. v2026.8.13 split the claim from the run so a background dispatch could
# take the CAS synchronously and hand the run to a daemon worker, and there are
# now four call sites for the run half — the flock has to be where the run is or
# three of them go unguarded.
#
# That does put it after claim_job_for_fire has advanced next_run_at, so a
# refusal here skips a scheduled occurrence for a run that never happened. It is
# the same trade upstream now makes for its own try_register_running_job()
# guard, two lines above; a skipped occurrence of a job that is *already
# executing* is a far smaller harm than the two overlapping runs sharing one
# output file that this patch exists to stop.
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
    "    try:\n"
    "        from cron.scheduler import (\n"
    "            release_running_job,\n"
    "            run_one_job,\n"
    "            try_register_running_job,\n"
    "        )\n"
)
DISPATCH_IMPORT_PATCHED = (
    "    _registered = False\n"
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
# The whole handler is the anchor, not just its last line: _execute_job_now
# ends in a byte-identical `mark_job_run`/`return` tail, and the `if
# _registered:` block above it is the only thing that tells the two apart.
# ``finally`` goes after the ``except``, which is why this appends to the end
# of the handler rather than opening a clause before it.
DISPATCH_RELEASE_ANCHOR = (
    "    except Exception as e:\n"
    '        logger.error("Failed to execute cron job %s immediately: %s", job_id, e)\n'
    "        if _registered:\n"
    "            # Registration succeeded but we raised before the run's own\n"
    "            # release ran (e.g. heartbeat setup) — don't leave the job\n"
    "            # permanently marked in-flight. Only release registrations WE\n"
    "            # took: a bare discard here could erase a ticker-owned entry.\n"
    "            try:\n"
    "                from cron.scheduler import release_running_job as _release\n"
    "\n"
    "                _release(job_id)\n"
    "            except Exception:\n"
    "                pass\n"
    "        try:\n"
    "            mark_job_run(job_id, False, str(e))\n"
    "        except Exception:\n"
    "            pass\n"
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

# --- 11. correct the comment that said the CAS was enough --------------------
# It is load-bearing prose: it is why nobody looked for the overlap. v2026.8.13
# reworded the second half around the background-dispatch split ("the claim
# (taken inside both paths below)") but kept the claim it makes about what the
# claim does, so the correction is still owed.
STALE_COMMENT_ANCHOR = (
    "            # Execute the job immediately rather than only scheduling it for the\n"
    "            # next scheduler tick — a manual `run` should actually run, even when\n"
    "            # no gateway/ticker is active (the #41037 case). The claim (taken\n"
    "            # inside both paths below) advances next_run_at and blocks a\n"
    "            # concurrent tick from double-firing.\n"
)
STALE_COMMENT_PATCHED = (
    "            # Execute the job immediately rather than only scheduling it for the\n"
    "            # next scheduler tick — a manual `run` should actually run, even when\n"
    "            # no gateway/ticker is active (the #41037 case). The claim (taken\n"
    "            # inside both paths below) advances next_run_at.\n"
    "            # kube-agents patch: what it does NOT do is block a concurrent\n"
    "            # tick, whatever this comment used to claim. tick() reaches\n"
    "            # run_one_job via advance_next_runs, which never stamps a\n"
    "            # fire_claim, and the claim a dispatch does stamp goes stale after\n"
    "            # 300s while an audit runs for twenty minutes. The per-job flock\n"
    "            # _run_claimed_job now holds — on both paths below, which is why\n"
    "            # it sits there and not here — is what actually blocks the\n"
    "            # overlap. See tools/cron_tick_lock_scope.py.\n"
)

# --- 12. a spawned tick is a scheduler restart, so it sweeps first ----------
# recover_interrupted_executions() runs only from the two gateway-ticker
# lifecycles, so the platform profile -- ticked by spawning this CLI -- had
# never once reaped an abandoned attempt. See the module docstring.
RECOVER_ANCHOR = (
    "def cron_tick():\n"
    '    """Run due jobs once and exit."""\n'
    "    from cron.scheduler import tick\n"
    "    tick(verbose=True)\n"
)
RECOVER_PATCHED = (
    "def cron_tick():\n"
    '    """Run due jobs once and exit."""\n'
    "    from cron.scheduler import tick\n"
    "\n"
    "    # kube-agents patch: this process IS the platform profile's whole\n"
    "    # scheduler lifecycle, so the recovery sweep that\n"
    "    # InProcessCronScheduler.start runs at startup has to happen here or it\n"
    "    # never happens at all. It only touches rows whose owner process is\n"
    "    # proved gone, and a sweep that fails must not cost us the tick.\n"
    "    # See tools/cron_tick_lock_scope.py.\n"
    "    try:\n"
    "        from cron.executions import recover_interrupted_executions\n"
    "\n"
    "        recovered = recover_interrupted_executions()\n"
    "        if recovered:\n"
    "            print(\n"
    '                f"Recovered {recovered} interrupted execution(s) whose owner "\n'
    '                f"process is gone; marked unknown."\n'
    "            )\n"
    "    except Exception as recover_err:\n"
    '        print(f"Execution recovery skipped: {recover_err}", file=sys.stderr)\n'
    "\n"
    "    tick(verbose=True)\n"
)

PATCHES = (
    (
        "cron/scheduler.py",
        (
            (IMPORT_ANCHOR, IMPORT_PATCHED, 1),
            (ACQUIRE_ANCHOR, ACQUIRE_PATCHED, 1),
            (RELEASE_ANCHOR, RELEASE_PATCHED, 1),
            (FINALLY_ANCHOR, FINALLY_PATCHED, 1),
            (GUARD_ANCHOR, GUARD_PATCHED, 1),
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
            (STALE_COMMENT_ANCHOR, STALE_COMMENT_PATCHED, 1),
        ),
    ),
    (
        "hermes_cli/cron.py",
        (
            (RECOVER_ANCHOR, RECOVER_PATCHED, 1),
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
