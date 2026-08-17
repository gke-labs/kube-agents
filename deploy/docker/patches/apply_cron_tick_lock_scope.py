#!/usr/bin/env python3
"""Wire tools/cron_tick_lock_scope.py into the Hermes source tree.

Eleven anchored edits across three files -- six in ``cron/scheduler.py``, four
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
GUARD_ANCHOR = (
    "            with _running_lock:\n"
    "                if job_id in _running_job_ids:\n"
    "                    logger.info(\"Job '%s' already running — skipping\", job.get(\"name\", job_id))\n"
    "                    return None\n"
    "                _running_job_ids.add(job_id)\n"
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
    "                    with _running_lock:\n"
    '                        _running_job_ids.discard(j["id"])\n'
)
GUARD_PATCHED = (
    "            with _running_lock:\n"
    "                if job_id in _running_job_ids:\n"
    "                    logger.info(\"Job '%s' already running — skipping\", job.get(\"name\", job_id))\n"
    "                    return None\n"
    "                _running_job_ids.add(job_id)\n"
    "            # kube-agents patch: _running_job_ids is module-level, so it is\n"
    "            # empty in every freshly spawned `hermes cron tick`. With the tick\n"
    "            # lock now released at dispatch, a second process can reach here\n"
    "            # for the same job if that job outlives its own period. Mirror the\n"
    "            # claim with a per-job flock the kernel releases on process death.\n"
    "            # See tools/cron_tick_lock_scope.py.\n"
    "            _job_lock = _job_locks.claim(job_id)\n"
    "            if _job_lock is None:\n"
    "                logger.info(\n"
    "                    \"Job '%s' already running in another process — skipping\",\n"
    '                    job.get("name", job_id),\n'
    "                )\n"
    "                with _running_lock:\n"
    "                    _running_job_ids.discard(job_id)\n"
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
    "                    with _running_lock:\n"
    '                        _running_job_ids.discard(j["id"])\n'
    "                    lock.release()\n"
)

# --- 6. release it on the dispatch-failure path too -------------------------
SUBMIT_ERR_ANCHOR = (
    "            except Exception as submit_err:\n"
    "                with _running_lock:\n"
    "                    _running_job_ids.discard(job_id)\n"
)
SUBMIT_ERR_PATCHED = (
    "            except Exception as submit_err:\n"
    "                with _running_lock:\n"
    "                    _running_job_ids.discard(job_id)\n"
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
    "    Claims the job's per-job flock first — the same lock ``cron/scheduler.py``\n"
    "    takes at dispatch — and refuses outright when a scheduled tick or another\n"
    "    dispatch is already running the job. Then the ``claim_job_for_fire`` CAS,\n"
    "    which advances ``next_run_at`` for recurring jobs and settles a race\n"
    "    between two fires of the same occurrence; if that claim is lost, this is\n"
    "    a no-op. The CAS on its own never blocked a concurrent tick — see the\n"
    "    kube-agents patch note below, and ``tools/cron_tick_lock_scope.py``.\n"
)

# --- 8. a dispatched run claims the same lock, before the store CAS ---------
# Before, not after: claim_job_for_fire advances next_run_at, so a refusal
# discovered afterwards would have skipped a scheduled occurrence for a run
# that never happened.
DISPATCH_CLAIM_ANCHOR = (
    '    job_id = job["id"]\n'
    "    try:\n"
    "        from cron.scheduler import run_one_job\n"
    "\n"
    "        # At-most-once claim: bail without running if a tick/other fire owns it.\n"
    "        if not claim_job_for_fire(job_id):\n"
)
DISPATCH_CLAIM_PATCHED = (
    '    job_id = job["id"]\n'
    "    # kube-agents patch: a dispatched run overlapped a scheduled one, and\n"
    "    # two runs of one fleet audit share the job's output file and scratch\n"
    "    # state. Take the same per-job flock tick takes, for the whole run.\n"
    "    # See tools/cron_tick_lock_scope.py.\n"
    "    _run_lock = None\n"
    "    try:\n"
    "        from cron.scheduler import _job_locks, run_one_job\n"
    "\n"
    "        _run_lock = _job_locks.claim(job_id)\n"
    "        if _run_lock is None:\n"
    "            return {\n"
    '                "claimed": False,\n'
    '                "success": False,\n'
    '                "error": (\n'
    '                    "Job is already running — a scheduled tick or another "\n'
    '                    "dispatch holds it, and a second copy would share this "\n'
    "                    \"run's output file and scratch state. Not started. The \"\n"
    '                    "run in flight records its own result; check it with "\n'
    '                    "`hermes cron runs <job_id>` before dispatching again."\n'
    "                ),\n"
    "            }\n"
    "\n"
    "        # At-most-once claim: bail without running if a tick/other fire owns it.\n"
    "        if not claim_job_for_fire(job_id):\n"
)

# --- 9. release it on every path out of _execute_job_now --------------------
DISPATCH_RELEASE_ANCHOR = (
    "    except Exception as e:\n"
    '        logger.error("Failed to execute cron job %s immediately: %s", job_id, e)\n'
    "        try:\n"
    "            mark_job_run(job_id, False, str(e))\n"
    "        except Exception:\n"
    "            pass\n"
    '        return {"claimed": True, "success": False, "error": str(e)}\n'
)
DISPATCH_RELEASE_PATCHED = (
    "    except Exception as e:\n"
    '        logger.error("Failed to execute cron job %s immediately: %s", job_id, e)\n'
    "        try:\n"
    "            mark_job_run(job_id, False, str(e))\n"
    "        except Exception:\n"
    "            pass\n"
    '        return {"claimed": True, "success": False, "error": str(e)}\n'
    "    finally:\n"
    "        # kube-agents patch: every path out, including the BaseException the\n"
    "        # except above does not catch. The claim was taken on this thread and\n"
    "        # is released on it. See tools/cron_tick_lock_scope.py.\n"
    "        if _run_lock is not None:\n"
    "            _run_lock.release()\n"
)

# --- 10. correct the comment that said the CAS was enough --------------------
# It is load-bearing prose: it is why nobody looked for the overlap.
STALE_COMMENT_ANCHOR = (
    "            # Execute the job immediately rather than only scheduling it for the\n"
    "            # next scheduler tick — a manual `run` should actually run, even when\n"
    "            # no gateway/ticker is active (the #41037 case). The claim inside\n"
    "            # _execute_job_now advances next_run_at and blocks a concurrent tick\n"
    "            # from double-firing.\n"
)
STALE_COMMENT_PATCHED = (
    "            # Execute the job immediately rather than only scheduling it for the\n"
    "            # next scheduler tick — a manual `run` should actually run, even when\n"
    "            # no gateway/ticker is active (the #41037 case).\n"
    "            # kube-agents patch: claim_job_for_fire does NOT block a concurrent\n"
    "            # tick, whatever this comment used to claim. tick() reaches\n"
    "            # run_one_job via advance_next_runs, which never stamps a\n"
    "            # fire_claim, and the claim a dispatch does stamp goes stale after\n"
    "            # 300s while an audit runs for twenty minutes. The per-job flock\n"
    "            # _execute_job_now now holds is what actually blocks the overlap.\n"
    "            # See tools/cron_tick_lock_scope.py.\n"
)

# --- 11. a spawned tick is a scheduler restart, so it sweeps first ----------
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
