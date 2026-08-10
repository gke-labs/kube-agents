#!/usr/bin/env python3
"""Host tests for cron_tick_lock_scope. No Hermes install required.

These cover the properties the in-image gate cannot cheaply reach: that
``release()`` is safe to call twice (the patch calls it twice by design, once
at dispatch and once in the untouched ``finally``), that ``JobLocks`` fails
toward FIRING rather than toward silence, and that a claim can be released by
a caller that can no longer resolve the store it was taken under -- the leak
that removing the by-id registry was meant to close.
"""

import fcntl
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cron_tick_lock_scope import (  # noqa: E402
    AdvisoryLock, JobLocks, _lock_handle, sanitise_job_id,
)

# A real second process, because that is the only claimant that matters: every
# platform tick is a freshly spawned `hermes cron tick`, so the guard is
# worthless if it only holds within one interpreter. `held` is a name and not
# an inline expression on purpose: the claim lives exactly as long as the
# object, so dropping the last reference to it hands the job back.
_HOLDER = """
import fcntl, sys, time
sys.path.insert(0, {here!r})
from cron_tick_lock_scope import JobLocks
from pathlib import Path
locks = JobLocks(lambda: Path({tmp!r}), fcntl)
held = locks.claim("github-issue-resolver")
print("CLAIMED" if held else "REFUSED", flush=True)
time.sleep(30)
"""


class LockScopeTests(unittest.TestCase):
    """Every case gets its own lock directory, so none can see another's files."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)

    def lock_file(self, name):
        """``_lock_handle`` on a fresh file, released at teardown, or None.

        An unreleased ``AdvisoryLock`` leaks its file handle, and unittest
        surfaces that as a ResourceWarning on every CI run — noise that trains
        readers to ignore warnings from this suite.
        """
        handle = open(self.tmp / name, "w", encoding="utf-8")
        # close() is idempotent, so this stays correct on the paths where
        # ``_lock_handle`` or the lock already closed it, and covers the one
        # where it raised before either could.
        self.addCleanup(handle.close)
        lock = _lock_handle(handle, fcntl)
        if lock is not None:
            self.addCleanup(lock.release)
        return lock

    def claim(self, locks, job_id):
        """``JobLocks.claim`` whose surviving claim is released at teardown."""
        lock = locks.claim(job_id)
        if lock is not None:
            self.addCleanup(lock.release)
        return lock

    def job_locks(self, lock_dir_fn=None):
        return JobLocks(lock_dir_fn or (lambda: self.tmp), fcntl)

    def test_releasing_a_lock_twice_is_a_noop_the_second_time(self):
        lock = self.lock_file("a.lock")
        self.assertIsNotNone(lock)
        self.assertFalse(lock.released)
        lock.release()
        self.assertTrue(lock.released)
        lock.release()  # must not raise, must not reopen
        self.assertIsNotNone(self.lock_file("a.lock"))

    def test_releasing_closes_the_handle_even_with_no_locking_module(self):
        handle = open(self.tmp / "b.lock", "w", encoding="utf-8")
        self.addCleanup(handle.close)  # idempotent; the assertion below is the point
        lock = _lock_handle(handle, None, None)
        self.assertIsNotNone(lock)
        lock.release()
        self.assertTrue(handle.closed)

    def test_an_unheld_claim_starts_released_and_releases_harmlessly(self):
        """``claim``'s fail-open paths hand this back; it must own nothing."""
        lock = AdvisoryLock.unheld()
        self.assertTrue(lock.released)
        lock.release()

    def test_a_second_lock_on_the_same_file_is_refused_while_the_first_is_held(self):
        first = self.lock_file("c.lock")
        self.assertIsNotNone(first)
        self.assertIsNone(self.lock_file("c.lock"))
        first.release()
        self.assertIsNotNone(self.lock_file("c.lock"))

    def test_a_refused_lock_does_not_disturb_the_claim_already_held(self):
        """``_lock_handle`` closes the handle it failed to lock.

        Safe with ``flock`` because the lock lives on the open file
        description; with ``fcntl.lockf`` this same close would drop the claim
        the first handle holds, and the job would run twice.
        """
        first = self.lock_file("d.lock")
        self.assertIsNotNone(first)
        self.assertIsNone(self.lock_file("d.lock"))
        self.assertIsNone(self.lock_file("d.lock"))
        self.assertFalse(first.released)

    def test_one_store_lets_a_single_process_claim_a_job_id_only_once(self):
        """The kernel is the registry: no dict, and still no double claim."""
        locks = self.job_locks()
        first = self.claim(locks, "github-issue-resolver")
        self.assertIsNotNone(first)
        self.assertIsNone(self.claim(locks, "github-issue-resolver"))
        first.release()
        self.assertIsNotNone(self.claim(locks, "github-issue-resolver"))

    def test_a_sibling_claimant_is_refused_until_the_holder_releases(self):
        a = self.job_locks()
        b = self.job_locks()  # stands in for a sibling process
        held = self.claim(a, "github-issue-resolver")
        self.assertIsNotNone(held)
        self.assertIsNone(self.claim(b, "github-issue-resolver"))
        self.assertIsNotNone(self.claim(b, "compliance-audit"))
        held.release()
        self.assertIsNotNone(self.claim(b, "github-issue-resolver"))

    def test_a_claim_survives_the_store_moving_out_from_under_its_owner(self):
        """The regression that deleting the by-id registry fixed.

        ``tick`` claims on the ticker thread and releases inside
        ``_run_and_release``, on a ThreadPoolExecutor worker whose context is
        empty — so ``get_hermes_home()`` there answers with the process home,
        not the profile the claim was taken under. A registry keyed by lock
        path missed on that recomputed path and left the claim held for the
        gateway's whole life, silently suppressing the job. Holding the lock
        object instead makes the release independent of what the store resolves
        to by the time it runs.
        """
        store = [self.tmp / "profile-a" / "cron"]
        locks = self.job_locks(lambda: store[0])
        held = locks.claim("compliance-audit")
        self.assertIsNotNone(held)
        store[0] = self.tmp / "profile-b" / "cron"  # the worker's empty context
        held.release()
        store[0] = self.tmp / "profile-a" / "cron"
        self.assertIsNotNone(
            self.claim(locks, "compliance-audit"),
            "the claim leaked when the store resolved elsewhere at release time")

    def test_dropping_the_last_reference_to_a_claim_hands_the_job_back(self):
        """Why the patch binds the claim and carries it into the worker.

        The flock lives on the handle the ``AdvisoryLock`` owns, so once
        nothing references the lock, CPython closes the handle and the job is
        claimable again — no exception, no log line. Writing
        ``if _job_locks.claim(job_id) is None:`` and going no further would
        therefore have compiled, passed a single-process smoke test, and
        guarded nothing. ``_run_and_release(..., lock=_job_lock)`` keeps the
        reference alive for exactly the run's duration.
        """
        import gc
        import warnings

        locks = self.job_locks()
        with warnings.catch_warnings():
            # The dropped handle is the point of the test, not a leak to report.
            warnings.simplefilter("ignore", ResourceWarning)
            self.assertIsNotNone(locks.claim("github-issue-resolver"))
            gc.collect()
        self.assertIsNotNone(
            self.claim(locks, "github-issue-resolver"),
            "an unreferenced claim outlived the object that owned it")

    def test_two_stores_sharing_a_job_id_do_not_refuse_each_other(self):
        """One process may tick several profiles; the store is part of the id.

        Per-cluster profiles are scaffolded from one template and so share
        every job id by construction.
        """
        first, second = self.tmp / "a" / "cron", self.tmp / "b" / "cron"
        store = [first]
        locks = self.job_locks(lambda: store[0])
        on_first = self.claim(locks, "compliance-audit")
        self.assertIsNotNone(on_first)
        store[0] = second
        on_second = self.claim(locks, "compliance-audit")
        self.assertIsNotNone(
            on_second, "a claim on one profile refused the same id on another")
        on_first.release()
        store[0] = first
        self.assertIsNotNone(self.claim(locks, "compliance-audit"))
        store[0] = second
        self.assertIsNone(self.claim(locks, "compliance-audit"),
                          "releasing one store's claim released the other's")

    def test_the_claim_is_honoured_across_real_operating_system_processes(self):
        """The property the patch actually needs: two OS processes, one winner.

        Also proves the kernel — not a janitor — hands the claim back, which is
        the whole reason flock was chosen over a ledger row.
        """
        src = _HOLDER.format(here=str(Path(__file__).parent), tmp=str(self.tmp))
        holder = subprocess.Popen([sys.executable, "-c", src],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "CLAIMED")
            mine = self.job_locks()
            self.assertIsNone(
                self.claim(mine, "github-issue-resolver"),
                "a second process claimed a job another process is running")
            self.assertIsNotNone(
                self.claim(mine, "compliance-audit"),
                "the holder's claim leaked onto an unrelated job id")
        finally:
            holder.kill()
            holder.wait(timeout=30)
            if holder.stdout is not None:
                holder.stdout.close()
        # SIGKILL leaves no chance to release cleanly; the kernel must do it.
        deadline = time.monotonic() + 10
        reclaimed = None
        while time.monotonic() < deadline and reclaimed is None:
            reclaimed = self.claim(self.job_locks(), "github-issue-resolver")
            if reclaimed is None:
                time.sleep(0.1)
        self.assertIsNotNone(
            reclaimed, "the claim outlived the SIGKILLed process that held it")

    def test_an_unusable_lock_directory_still_fires_the_job(self):
        def broken():
            raise OSError("read-only file system")
        self.assertIsNotNone(self.claim(self.job_locks(broken), "compliance-audit"))

    def test_a_surprising_exception_resolving_the_store_still_fires_the_job(self):
        """Only a held flock may answer None — not an unexpected exception.

        In the image ``lock_dir_fn`` reaches ``get_hermes_home()``. A
        ValueError out of there once meant the whole tick aborted having
        dispatched nothing.
        """
        def broken():
            raise ValueError("HERMES_HOME is not a path")
        self.assertIsNotNone(self.claim(self.job_locks(broken), "compliance-audit"))

    def test_a_lock_file_that_will_not_open_still_fires_the_job(self):
        """The reachable half of the fail-open rule.

        ``mkdir(parents=True, exist_ok=True)`` succeeds on an EXISTING
        read-only directory, so the directory guard never fires for the case
        that actually happens: `<HERMES_HOME>/cron/` present but unwritable, or
        the volume out of space. If the open failure were read as "held", every
        job on the profile would stop firing while the log claimed each one was
        already running elsewhere.
        """
        def refuse(*_args, **_kwargs):
            raise PermissionError("read-only lock directory")
        locks = JobLocks(lambda: self.tmp, fcntl, None, refuse)
        lock = locks.claim("compliance-audit")
        self.assertIsNotNone(lock)
        self.assertTrue(lock.released, "a fail-open claim pretended to hold a lock")
        # And it holds nothing, so a working claimant is not refused by it.
        self.assertIsNotNone(self.claim(self.job_locks(), "compliance-audit"))

    def test_a_job_id_becomes_a_safe_filename_component(self):
        self.assertEqual(sanitise_job_id("github-issue-resolver"),
                         "github-issue-resolver")
        self.assertNotIn("/", sanitise_job_id("../../etc/passwd"))
        self.assertEqual(sanitise_job_id(""), "_")
        self.assertEqual(len(sanitise_job_id("x" * 500)), 120)


if __name__ == "__main__":
    unittest.main()
