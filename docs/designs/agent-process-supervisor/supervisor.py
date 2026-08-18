"""Prototype of the supervisor described in ../agent-process-supervisor.md, 3.2-3.7.

NOT SHIPPING CODE. This exists to test the design's mechanisms before they are
implemented in k8s-operator/internal/controller/leader_elect.py. See README.md.

Implements, and deliberately mirrors the section numbering of, the design:

  3.2  the process table, with per-entry grace and reverse-order shutdown
  3.3  per-process backoff and cap, diverging on required vs optional
  3.4  the status file + the one-line `ready` the probes read
  3.5  a renew deadline on the local clock, and a bounded lease call
  3.7  ONE waitpid(-1), dispatching exit statuses into the table by pid

Leader election is stubbed to the presence of a file, so this runs without a
Kubernetes API server. Everything else is the real shape.
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections import deque

# Scaled down from the design's real values so experiments run in seconds.
# run_experiments.py rescales measurements back to the design's figures.
RESTART_CAP = 2  # design: 5
RESTART_WINDOW = 60  # design: 600
BACKOFF_MAX = 8  # design: 30
POLL = 1.0  # design: 2 + U(0,1)
GRACE = 2.0  # design: 10, and per entry rather than global
RENEW_DEADLINE = 2.0  # design: 8
LEASE_CALL_TIMEOUT = 0.5  # design: 3 (_request_timeout on every lease call)

STATUS = os.environ.get("SUPERVISOR_STATUS_FILE", "supervisor.json")
READY = os.environ.get("SUPERVISOR_READY_FILE", "supervisor.ready")
LEASE = os.environ.get("SUPERVISOR_LEASE_FILE", "lease")


def log(msg):
    print(f"[sup {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _killpg(pgid, sig):
    """Signal a process group, tolerating one that has already gone."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


class Supervised:
    """One row of the process table (3.2)."""

    def __init__(self, name, argv, required, grace=GRACE):
        self.name = name
        self.argv = argv
        self.required = required
        self.grace = grace  # 3.2: per entry. 3.5 sums this column.
        self.proc = None
        self.state = "pending"  # pending | running | backoff | exited | gave_up | stopped
        self.exit = None
        self.backoff = 1
        self.retry_at = 0.0
        self.failures = deque()  # monotonic timestamps, trimmed to RESTART_WINDOW

    # -- 3.3 -------------------------------------------------------------
    def start(self, now):
        try:
            # start_new_session: the child leads its own process group so stop()
            # can signal the GROUP. Without it a grandchild outlives the handover
            # 3.5 guarantees -- Popen.terminate() reaches the parent only.
            # DEVNULL so a prototype child cannot hold a caller's pipe open.
            self.proc = subprocess.Popen(
                self.argv,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.state = "running"
            log(f"{self.name}: started pid={self.proc.pid}")
        except OSError as exc:  # missing binary, unwritable path, ...
            log(f"{self.name}: start FAILED: {exc}")
            self.proc = None
            self.penalise(now)  # a failed start is a restart, or it spins

    def on_exit(self, code, now):
        """The ONLY entry point for a child exiting; Supervisor.reap() calls it.
        Never calls self.proc.poll() -- see 3.7."""
        log(f"{self.name}: exited {code}")
        self.exit = code
        self.proc = None
        self.state = "exited"
        self.penalise(now)

    def tick(self, now):
        """Once per iteration. False => a REQUIRED process is past its cap."""
        if self.state == "pending" or (self.state == "backoff" and now >= self.retry_at):
            self.start(now)
        return self.state != "gave_up" or not self.required

    def penalise(self, now):
        """Public: 3.7's reaper is a legitimate caller."""
        self.failures.append(now)
        while self.failures and now - self.failures[0] > RESTART_WINDOW:
            self.failures.popleft()
        # STRICTLY greater. The deque counts FAILURES; the Nth failure is the
        # (N-1)th restart, so `>=` retires after CAP-1 restarts while the design
        # says CAP. See 3.3, and E12 for what that off-by-one was hiding.
        if len(self.failures) > RESTART_CAP:
            self.state = "gave_up"
            log(f"{self.name}: GAVE UP after {RESTART_CAP} restarts (required={self.required})")
            return
        self.state = "backoff"
        self.retry_at = now + self.backoff
        self.backoff = min(self.backoff * 2, BACKOFF_MAX)
        log(f"{self.name}: backoff {self.backoff}s")

    def stop(self, final=False):
        """Signal the process GROUP, wait out this entry's grace, SIGKILL it.
        Returns seconds taken.

        Runs on the poll loop's thread, never inside reap() and never
        concurrently with it (3.7). The wait() below is a TARGETED waitpid and is
        only safe because of that ordering.

        `final` separates the two callers, and 3.3 turns on the difference:
        cleanup is terminating, so the entry may stay stopped, but a demotion
        must leave it startable or a reacquired lease resumes with an empty
        table -- a leader holding the label and serving nothing.
        """
        t0 = time.monotonic()
        if self.proc is not None:
            pgid = self.proc.pid  # start_new_session made the child its own leader
            _killpg(pgid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=self.grace)
            except subprocess.TimeoutExpired:
                log(f"{self.name}: grace expired, SIGKILL to the group")
                _killpg(pgid, signal.SIGKILL)
                self.proc.wait()
            # Sweep the group even when the child exited promptly. SIGTERM to the
            # group is NOT enough: a grandchild that ignores it outlives a parent
            # that does not, wait() returns straight away, and the SIGKILL branch
            # above never runs. 3.5's guarantee is about everything the leader
            # ran, not just its direct child. (The child is reaped by now, so its
            # pid could in principle be reused as another group's id; pids are
            # handed out sequentially, so the window is microseconds wide, and
            # this is the trade every process-group supervisor makes.)
            _killpg(pgid, signal.SIGKILL)
            self.proc = None
        # These transitions run even when there was nothing to stop. An entry in
        # `backoff` has no process and still has to be reset; an early return on
        # `self.proc is None` skipped that, so a demotion mid-backoff carried a
        # stale retry_at into the next promotion. Same class of bug as E9.
        if self.state != "gave_up":  # sticky: the cap retired it, and `degraded` reads this
            self.state = "stopped" if final else "pending"
            if not final:
                self.backoff, self.retry_at = 1, 0.0  # a demotion is not a failure
        return time.monotonic() - t0


class Supervisor:
    def __init__(self, table, mode, renew_deadline=RENEW_DEADLINE):
        self.table = table
        self.mode = mode  # "solo" | "elected"
        self.role = "solo" if mode == "solo" else "follower"
        self.cleanup_ran = False
        # 3.5. `renew_deadline=None` is the pre-fix behaviour E11 contrasts
        # against: leadership ends only when a call comes back and says so.
        self.renew_deadline = renew_deadline
        self.last_renew = float("-inf")
        self.lease_call_delay = 0.0  # E11 turns this up to simulate a slow API

    # -- 3.7: the single point of truth for child exits -------------------
    def reap(self, now):
        by_pid = {p.proc.pid: p for p in self.table if p.proc is not None}
        orphans = 0
        while True:
            try:
                pid, sts = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break  # no children at all
            if pid == 0:
                break  # children exist, none have exited
            code = os.waitstatus_to_exitcode(sts)
            entry = by_pid.get(pid)
            if entry is None:
                orphans += 1  # a reparented grandchild; reaped and discarded
                continue
            entry.on_exit(code, now)
        if orphans:
            log(f"reaped {orphans} orphan(s)")

    # -- 3.5 ---------------------------------------------------------------
    def read_lease(self):
        """The stubbed lease call, BOUNDED like the real one's _request_timeout.

        Returns True/False, or None when the call did not complete in time. An
        untimed call is what made 3.5's first term meaningless (E11).
        """
        waited = min(self.lease_call_delay, LEASE_CALL_TIMEOUT)
        if waited:
            time.sleep(waited)
        if self.lease_call_delay > LEASE_CALL_TIMEOUT:
            return None  # timed out
        return os.path.exists(LEASE)

    def is_leader(self, now):
        """3.5: leadership ends on the LOCAL clock, not on a reply.

        A call that times out neither confirms nor denies, so the deadline
        decides -- which is the whole point, because a hung API server produces
        an unbounded run of exactly those.
        """
        if self.mode == "solo":
            return True
        held = self.read_lease()
        if held is True:
            self.last_renew = now
            return True
        if held is False:
            return False
        if self.renew_deadline is None:
            # Pre-fix: an inconclusive call leaves the leader leading, forever.
            return self.role == "leader"
        return now - self.last_renew <= self.renew_deadline

    # -- 3.4 ---------------------------------------------------------------
    def write_status(self, ready, degraded):
        now = time.time()
        doc = {
            "role": self.role,
            "ready": ready,
            "degraded": degraded,
            "updated_at": now,
            "processes": [
                {"name": p.name, "required": p.required, "state": p.state} for p in self.table
            ],
        }
        self._replace(STATUS, json.dumps(doc))
        # The probes read THIS, and it is renamed second so a reader catching the
        # pair mid-update sees a stale `ready` rather than a fresh one describing
        # an older document.
        self._replace(READY, f"{int(now)} {1 if ready else 0}\n")

    @staticmethod
    def _replace(path, text):
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)  # atomic

    def shutdown(self, why):
        """The one exit path. Never a bare sys.exit -- see P3 and 3.3."""
        if self.cleanup_ran:
            return 0.0
        self.cleanup_ran = True
        log(f"cleanup: {why}")
        total = 0.0
        for p in reversed(self.table):  # reverse start order (3.2)
            total += p.stop(final=True)
        # 3.3: say so before going, or the probe reports Ready for a dying container
        self.write_status(ready=False, degraded=True)
        log(f"cleanup: dropped label + released lease; shutdown took {total:.1f}s")
        return total

    def run(self, iterations=None):
        signal.signal(signal.SIGTERM, lambda *_: (self.shutdown("SIGTERM"), sys.exit(0)))
        i = 0
        while iterations is None or i < iterations:
            i += 1
            now = time.monotonic()
            self.reap(now)

            if not self.is_leader(now):
                if any(p.proc for p in self.table) or any(
                    p.state in ("backoff", "running") for p in self.table
                ):
                    for p in reversed(self.table):
                        p.stop()
                self.role = "follower"
                self.write_status(ready=True, degraded=False)  # followers stay Ready (3.4)
                time.sleep(POLL)
                continue

            self.role = "leader" if self.mode == "elected" else "solo"
            for p in self.table:
                if not p.tick(now):
                    self.shutdown(f"required process {p.name} past its cap")
                    sys.exit(1)

            required_ok = all(p.state == "running" for p in self.table if p.required)
            degraded = any(p.state == "gave_up" for p in self.table if not p.required)
            self.write_status(ready=required_ok, degraded=degraded)
            time.sleep(POLL)
