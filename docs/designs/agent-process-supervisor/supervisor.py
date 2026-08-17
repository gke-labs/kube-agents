"""Prototype of the supervisor described in ../agent-process-supervisor.md, 3.2-3.7.

NOT SHIPPING CODE. This exists to test the design's mechanisms before they are
implemented in k8s-operator/internal/controller/leader_elect.py. See README.md.

Implements, and deliberately mirrors the section numbering of, the design:

  3.2  the process table, with criticality and reverse-order shutdown
  3.3  per-process backoff and cap, diverging on required vs optional
  3.4  the status file with updated_at, and a final ready:false on the way out
  3.7  ONE waitpid, dispatching exit statuses into the table by pid

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
RESTART_CAP = 3  # design: 5
RESTART_WINDOW = 60  # design: 300
BACKOFF_MAX = 8  # design: 30
POLL = 1.0  # design: 3 + U(0,1)
GRACE = 2.0  # design: 10

STATUS = os.environ.get("SUPERVISOR_STATUS_FILE", "supervisor.json")
LEASE = os.environ.get("SUPERVISOR_LEASE_FILE", "lease")


def log(msg):
    print(f"[sup {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Supervised:
    """One row of the process table (3.2)."""

    def __init__(self, name, argv, required):
        self.name = name
        self.argv = argv
        self.required = required
        self.proc = None
        self.state = "pending"  # pending | running | backoff | exited | gave_up | stopped
        self.exit = None
        self.backoff = 1
        self.retry_at = 0.0
        self.restarts = deque()  # monotonic timestamps, trimmed to RESTART_WINDOW

    # -- 3.3 -------------------------------------------------------------
    def start(self, now):
        try:
            # DEVNULL so a prototype child cannot hold a caller's pipe open.
            self.proc = subprocess.Popen(
                self.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self.state = "running"
            log(f"{self.name}: started pid={self.proc.pid}")
        except OSError as exc:  # missing binary, unwritable path, ...
            log(f"{self.name}: start FAILED: {exc}")
            self.proc = None
            self.penalise(now)  # a failed start is a restart, or it spins

    def on_exit(self, code, now):
        """Called ONLY by Supervisor.reap(). Never calls self.proc.poll() -- see 3.7."""
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
        self.restarts.append(now)
        while self.restarts and now - self.restarts[0] > RESTART_WINDOW:
            self.restarts.popleft()
        if len(self.restarts) >= RESTART_CAP:
            self.state = "gave_up"
            log(f"{self.name}: GAVE UP after {len(self.restarts)} restarts (required={self.required})")
            return
        self.state = "backoff"
        self.retry_at = now + self.backoff
        self.backoff = min(self.backoff * 2, BACKOFF_MAX)
        log(f"{self.name}: backoff {self.backoff}s")

    def stop(self, grace=GRACE):
        """Terminate, wait out the grace, then SIGKILL. Returns seconds taken."""
        if self.proc is None:
            return 0.0
        t0 = time.monotonic()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            log(f"{self.name}: grace expired, SIGKILL")
            self.proc.kill()
            self.proc.wait()
        self.proc = None
        self.state = "stopped"
        return time.monotonic() - t0


class Supervisor:
    def __init__(self, table, mode):
        self.table = table
        self.mode = mode  # "solo" | "elected"
        self.role = "solo" if mode == "solo" else "follower"
        self.cleanup_ran = False

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

    def is_leader(self):
        return True if self.mode == "solo" else os.path.exists(LEASE)

    # -- 3.4 ---------------------------------------------------------------
    def write_status(self, ready, degraded):
        doc = {
            "role": self.role,
            "ready": ready,
            "degraded": degraded,
            "updated_at": time.time(),
            "processes": [
                {"name": p.name, "required": p.required, "state": p.state} for p in self.table
            ],
        }
        tmp = STATUS + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(doc, fh)
        os.replace(tmp, STATUS)  # atomic

    def shutdown(self, why):
        """The one exit path. Never a bare sys.exit -- see P3 and 3.3."""
        if self.cleanup_ran:
            return 0.0
        self.cleanup_ran = True
        log(f"cleanup: {why}")
        total = 0.0
        for p in reversed(self.table):  # reverse start order (3.2)
            total += p.stop()
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

            if not self.is_leader():
                if any(p.proc for p in self.table):
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
