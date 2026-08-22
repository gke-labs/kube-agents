#!/usr/bin/env python3
"""Give a spawned ``hermes cron tick`` the scheduler lifecycle it never had.

The platform profile has no cron ticker thread. It is ticked once a minute by
``agents/chat/scripts/profile_cron_tick.py`` spawning ``hermes cron tick``,
whose CLI body is verbatim ``tick(verbose=True)``. Everything Hermes attaches to
the ticker's lifetime -- the in-flight job set, the startup recovery sweep, the
assumption that a tick is a short scheduling pass inside a long-lived process --
is therefore either absent or wrong on that profile. This module and the twelve
anchored edits in ``apply_cron_tick_lock_scope.py`` supply what is missing.

Head-of-line blocking: the tick lock covered execution
------------------------------------------------------
``cron/scheduler.py::tick`` takes an exclusive ``flock`` on
``<HERMES_HOME>/cron/.tick.lock`` and, when ``sync=True`` (the signature
default), does not release it until the ``finally`` that runs *after*
``concurrent.futures.as_completed`` has waited out every job it dispatched. The
lock therefore covered job EXECUTION, not just the scheduling decision.

That is invisible on the default profile, which the gateway ticks in-process
with ``sync=False``. It was not invisible on the platform profile, whose every
tick takes the ``sync=True`` default. So a fleet audit held the profile's lock
for its whole run, and every spawn during that window hit ``LOCK_EX | LOCK_NB``,
logged a DEBUG skip, and returned 0 having dispatched nothing.

Measured on the live cluster over 17 hours: 3 of 35 ``github-issue-resolver``
firings blocked, 418s / 179s / 1142s late, each unblocking within seconds of
the blocking audit's ``finished_at``. The dispatcher's own durations in the
default ledger are trimodal and give the mechanism away --
0.10 / 45.14 / 0.12 / 0.13 / 0.11 / 0.12 / 2.37 / 2.29 / 2.65 / 26.85 / 0.12
across 06:18:55-06:38:55: ~0.12s is 'nothing due, no spawn', 45.14s is the
budget handoff at the audit's start, ~2.4s is 'spawned and the child died on
the flock', 26.85s is the first spawn after the audit released. Platform
ledger: 38 terminal executions, 0 overlapping pairs. Default ledger: 707
terminal, 190 overlapping.

The lock's own comment (scheduler.py, above ``advance_next_runs``) states its
purpose: advance ``next_run_at`` for the whole due set BEFORE any execution
begins, 'to preserve at-most-once semantics'. That purpose is served in full by
holding the lock through dispatch. Holding it through execution buys one extra
thing and one only -- a cross-process in-flight guard, because
``_running_job_ids`` is a module-level set and every platform tick is a fresh
process. This module supplies both halves:

``AdvisoryLock``
    Owns a locked file handle with an IDEMPOTENT ``release()``, so the dispatch
    path can release the tick lock early while the untouched ``finally`` still
    releases it on the early-return paths (``can_dispatch`` gate, no due jobs)
    and on any exception.

``JobLocks``
    Replaces what the wide lock was implicitly providing: one ``flock`` file
    per job id, ``<HERMES_HOME>/cron/.job-<id>.lock``, claimed beside
    ``_running_job_ids.add`` and released beside ``_running_job_ids.discard``.

Why flock and not a ledger row
------------------------------
A ledger-based guard was the obvious design and is wrong here.
``cron/executions.py::_owner_is_live`` returns True on any exception ('fail
safe: inability to prove death must not rewrite state') -- correct for a
recovery sweep, but for a dispatch gate True means SUPPRESS, so an import
failure would silently kill the job forever. It also returns
``pid == os.getpid()`` when ``process_started_at`` is None, which from a fresh
tick process is always False for a sibling's row -- the guard would just vanish.
And a wedged row wedges forever: a ledger row left ``running`` by a killed
process is only cleared by ``recover_interrupted_executions``, whereas an flock
cannot wedge at all, because the kernel drops it when the holding process exits,
however it exits.

Failure polarity is deliberate, and it is the whole safety argument. This is a
DISPATCH gate: refusal means "do not run this job". So ``JobLocks.claim``
returns None for exactly one reason -- a live process holds the flock -- and a
claim for every other outcome, including an unresolvable store, an unwritable
lock directory, and a lock file that will not open. Getting that backwards would
stop every job on the profile while logging "already running in another
process", which is both silent and actively misleading to whoever debugs it.
Note that ``mkdir(parents=True, exist_ok=True)`` SUCCEEDS on an existing
read-only directory, so the directory guard alone does not cover the case where
``<HERMES_HOME>/cron/`` is unwritable but present -- the open has to be
separated from the flock for the two to be told apart, which is why
``claim`` calls the opener itself and hands the handle to ``_lock_handle``.

The caller owns its claim
-------------------------
``claim`` hands back the ``AdvisoryLock`` rather than recording it in a registry
the caller later releases by job id. That is not a style preference; releasing
by id was a leak.

``tick`` claims on the ticker thread and releases in ``_run_and_release``'s
``finally``, which runs on a ``ThreadPoolExecutor`` worker -- and, crucially,
OUTSIDE the ``contextvars.copy_context()`` that ``ctx.run(_process_job, j)``
enters. A registry keyed by lock path has to recompute that path to release,
and the path comes from ``_get_lock_paths`` -> ``get_hermes_home()``, whose
override is a ``ContextVar`` in ``hermes_constants``. A worker thread starts
with an empty context, so the recomputed path is the *process* ``HERMES_HOME``,
not the profile the claim was taken under. Under
``CronSchedulerProvider._start_multiplex``, which wraps each profile's tick in
``set_hermes_home_override``, the pop misses and the claim is never released:
that job is then suppressed on every later tick in that gateway's life, logging
"already running in another process" about a process that finished hours ago --
exactly the silent-stoppage failure the polarity rule above exists to prevent.
Holding the lock object closes that hole by construction. It also deletes the
registry dict, its mutex, and its double-check race.

Releasing the flock from a thread other than the one that took it is fine in
itself: an ``flock`` belongs to the open file description, which is shared by
every thread in the process. The bug was never the thread, it was recomputing
the identity.

The claim's lifetime is the object's lifetime, and that is a real edge for a
caller to get wrong. The flock lives on the handle the ``AdvisoryLock`` owns,
so the moment nothing references the lock, CPython closes the handle and the
job becomes claimable again -- no exception, no log line. ``if
_job_locks.claim(job_id) is None: ...`` and nothing further would compile, pass
a single-process smoke test, and guard nothing. That is why the patched
``tick`` binds ``_job_lock`` and passes it into ``_run_and_release`` as a
default argument, and why ``_run_claimed_job`` binds ``_run_lock`` for the
whole body.
``test_cron_tick_lock_scope.py::test_dropping_the_last_reference_to_a_claim_hands_the_job_back``
pins it.

A dispatched run takes the same lock
------------------------------------
``tools/cronjob_tools.py`` -- the body of ``cronjob(action='run')`` -- called
``run_one_job`` with no per-job lock at all. Its only guard was
``cron/jobs.py::claim_job_for_fire``, and the comment above the call site
claiming that guard "blocks a concurrent tick from double-firing" was simply
untrue in both directions: ``tick`` reaches ``run_one_job`` via
``advance_next_runs``, which never stamps a ``fire_claim``, so a dispatch always
wins the CAS against a run already in flight; and the ``fire_claim`` a dispatch
does stamp expires after ``claim_ttl_seconds=300`` while a compliance audit runs
for twenty minutes, so the tick then fires a second copy on top of the first.
Two concurrent runs of one audit share the job's output file and the profile's
scratch state, which is how a fleet audit came to publish another run's
findings.

The flock is claimed in ``_run_claimed_job`` -- the run half v2026.8.13 split
out of ``_execute_job_now``, so that a background dispatch can take the claim
synchronously and hand the run to a daemon worker -- and released in a
``finally``. Four call sites reach that run half and ``_execute_job_now`` is
only one of them, so guarding the run rather than the entry point is what keeps
the other three covered.

Guarding there puts the flock AFTER ``claim_job_for_fire``, and that costs one
thing: the CAS has already advanced ``next_run_at``, so a refusal skips a
scheduled occurrence of a job this call never ran. It is a deliberate trade, and
the same one upstream makes two lines above for its own
``try_register_running_job`` guard. A skipped occurrence of a job that is
*already executing* is a far smaller harm than the two overlapping runs sharing
one output file that this patch exists to stop.

A refused dispatch returns immediately rather than waiting for the lock. The
caller is an agent blocked inside a tool call, the run it would wait for can
last twenty minutes, and what it would get at the end is a redundant second run
of a job that has just finished. The refusal is returned as ``claimed: True``
with an ``error``, which ``cronjob``'s ``run`` branch surfaces to the model as
``execution_error``. ``claimed: False`` would read better -- nothing ran, and
``result["executed"]`` consequently says ``True`` when it should not -- but it
would also be a lie about the CAS, which did succeed and did advance
``next_run_at``. That branch skips ``_notify_provider_jobs_changed_safe()`` on
an unclaimed result, so reporting the refusal as unclaimed would leave an
external provider's one-shot un-reconciled against an occurrence this call has
already consumed.

A spawned tick is a scheduler restart
-------------------------------------
``recover_interrupted_executions`` marks ``claimed``/``running`` ledger rows
``unknown`` once their owner process is proved gone. It is reachable only from
``CronSchedulerProvider.recover_interrupted()``, called by
``InProcessCronScheduler.start`` and by ``_start_multiplex`` -- both of which
are gateway-ticker lifecycles, and neither of which runs for a profile ticked by
spawning a CLI. So on the platform profile nothing ever reaped an interrupted
attempt, and ``_prune_unlocked`` only deletes rows in a terminal state, which a
stuck row by definition is not.

The live platform ledger on 2026-08-08 held 96 rows: 90 ``completed`` and 6
permanently ``running``, the oldest from 2026-08-07T02:00:44Z, against 0 of 1000
on the default store the gateway does sweep. Five of the six were
``source='direct'`` -- ``cronjob(action='run')`` dispatches whose kanban worker
process exited mid-run -- and every one of their owner pids was gone, so every
one of them was reapable and none had been reaped. A run that dies leaves no
failure anywhere: ``cron runs`` shows it as still in flight, ``cron_health``
projects a run that is not running, and the row is immortal.

The fix is one line in ``hermes_cli/cron.py::cron_tick``, which is that
profile's entire scheduler lifecycle: sweep first, then tick. It cannot reap a
live run -- ``_owner_is_live`` checks both the pid and its start time, so a
still-running sibling tick or a live gateway is skipped -- and a sweep that
raises is logged and stepped over, because nothing about bookkeeping may stop a
tick from dispatching.

Testing
-------
Everything here is dependency-injected (``fcntl``/``msvcrt``/``open`` are
parameters) so ``test_cron_tick_lock_scope.py`` can drive it on a host without
Hermes installed. ``verify_cron_tick_lock_scope.py`` then exercises the patched
image itself, including a real second process holding the locks and a real
stuck ledger row being reaped.

Not fixed here, but fixed alongside: ``profile-cron-tick`` used to be scheduled
``interval: 1 minute`` and actually ran every two -- 493 of 545 inter-run gaps
were 120s, only 48 were 60s -- because Hermes re-anchors an ``interval`` job to
the moment the last run FINISHED while the gateway ticker sleeps a fixed sixty
seconds after each tick returns. That is the ``cron-tick-drift`` item, and it is
closed in the same change set by moving every job on both rosters to a cron
expression (``* * * * *``), which snaps up to the next wall-clock minute and so
cannot drift. ``test_profile_cron_tick.py::test_no_job_on_this_roster_uses_an_interval_schedule``
fails the build on any surviving ``interval`` job, so do not reintroduce one.
What remains after that is narrower and is NOT addressed anywhere: a dispatch
that actually ran a watchdog blocks up to ``DEFAULT_BUDGET_SECONDS`` (45) on the
subprocess and usually costs the minute after it. See the schedule bullet in
docs/site/src/content/docs/concepts/autonomous-watchdogs.md.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def sanitise_job_id(job_id: Any) -> str:
    """Map a job id onto a filename component. Never empty, never a path."""
    cleaned = _UNSAFE.sub("_", str(job_id))[:120]
    return cleaned or "_"


class AdvisoryLock:
    """A held advisory file lock whose ``release()`` is idempotent.

    The tick path calls ``release()`` once it has finished dispatching; the
    untouched ``finally`` calls it again. The second call must be a no-op, and
    must not unlock a handle some other object has since reopened -- hence the
    ``_released`` flag rather than a bare unlock.
    """

    def __init__(self, handle, fcntl_mod=None, msvcrt_mod=None) -> None:
        self._handle = handle
        self._fcntl = fcntl_mod
        self._msvcrt = msvcrt_mod
        self._released = False

    @classmethod
    def unheld(cls) -> "AdvisoryLock":
        """A claim that owns nothing, for ``JobLocks.claim``'s fail-open paths.

        Returning this rather than None keeps one rule at the call sites: a
        claim object means "run the job", None means "somebody else is running
        it". A storage problem must never be mistaken for the second, so the
        cases where there was nothing to lock still hand back something with a
        harmless ``release()``.
        """
        lock = cls(None)
        lock._released = True
        return lock

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._fcntl is not None:
            try:
                self._fcntl.flock(self._handle, self._fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        elif self._msvcrt is not None:
            try:
                self._msvcrt.locking(self._handle.fileno(), self._msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        try:
            self._handle.close()
        except Exception:
            pass


def _lock_handle(handle, fcntl_mod=None, msvcrt_mod=None) -> Optional[AdvisoryLock]:
    """flock an already-open handle. None -- and closed -- if it is held.

    Split out of ``JobLocks.claim`` so the caller can tell "the file would not
    open" from "another process holds it". The dispatch gate has to: the first
    must fire the job, the second must skip it.

    Closing the handle on the refused path is safe with ``flock`` and would not
    be with ``fcntl.lockf``: an flock lives on the open file description, so
    dropping the description we failed to lock cannot disturb a lock this
    process holds through a different one. Verified against both Linux and
    macOS before the in-process registry was removed.
    """
    try:
        if fcntl_mod is not None:
            fcntl_mod.flock(handle, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
        elif msvcrt_mod is not None:
            msvcrt_mod.locking(handle.fileno(), msvcrt_mod.LK_NBLCK, 1)
    except (OSError, IOError):
        try:
            handle.close()
        except Exception:
            pass
        return None
    return AdvisoryLock(handle, fcntl_mod, msvcrt_mod)


class JobLocks:
    """Cross-process mirror of ``_running_job_ids``, one flock file per job.

    ``lock_dir_fn`` is called on every claim rather than cached: the tick
    lock's own directory is resolved per call for exactly the same reason
    (``_get_lock_paths`` -- 'so profile/env changes are honored').

    There is no in-process registry of outstanding claims. The kernel is the
    registry: a second ``open()`` of a file this process already flocked gets
    its own open file description, and flock refuses it exactly as it refuses
    another process. Keeping a dict beside that duplicated the kernel's answer
    and forced ``release`` to recompute the lock path on a thread that could no
    longer resolve it -- see the module docstring.
    """

    def __init__(self, lock_dir_fn: Callable[[], Any], fcntl_mod=None,
                 msvcrt_mod=None, opener=open) -> None:
        self._lock_dir_fn = lock_dir_fn
        self._fcntl = fcntl_mod
        self._msvcrt = msvcrt_mod
        self._opener = opener

    def _lock_path(self, job_id: Any) -> Optional[str]:
        """This job's lock file, or None if the store cannot be resolved.

        The PATH is the identity, not the job id. ``lock_dir_fn`` is per store,
        so one process ticking two profiles gets two different files for the
        same id -- gateway multiplex does that, and so would per-cluster
        profiles scaffolded from one template, which share every job id by
        construction. One profile's claim must not refuse another profile's job.

        The bare ``except`` is the same fail-open rule as ``claim``: this is a
        dispatch gate, and nothing it cannot answer may become a skip.
        """
        try:
            directory = self._lock_dir_fn()
            directory.mkdir(parents=True, exist_ok=True)
            return str(directory / (".job-" + sanitise_job_id(job_id) + ".lock"))
        except Exception:
            return None

    def claim(self, job_id: Any) -> Optional[AdvisoryLock]:
        """The claim on ``job_id``, or None if this process may not run it.

        None means one thing and one thing only: a live process -- this one or
        another -- holds the flock. An unresolvable store, an unwritable lock
        directory and a lock file that will not open all hand back an
        ``AdvisoryLock.unheld()``, so the job runs. See the failure-polarity
        paragraph in the module docstring; a storage problem that answered None
        here would silence every job on the profile.

        The returned lock is the caller's to release, on whatever thread and in
        whatever context it finishes in.
        """
        path = self._lock_path(job_id)
        if path is None:
            return AdvisoryLock.unheld()  # cannot resolve the store -> run
        try:
            handle = self._opener(path, "w", encoding="utf-8")
        except (OSError, IOError):
            return AdvisoryLock.unheld()  # cannot open the lock file -> run
        try:
            return _lock_handle(handle, self._fcntl, self._msvcrt)
        except BaseException:
            # ``_lock_handle`` owns the handle on both paths it returns from --
            # closing it on the refusal, handing it to the lock otherwise. It
            # raising means neither happened, so the descriptor is still ours.
            try:
                handle.close()
            except Exception:
                pass
            raise
