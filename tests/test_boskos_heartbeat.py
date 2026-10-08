"""Tests for hack/boskos_heartbeat.sh against a fake Boskos /update endpoint.

Behavioural, not timing-based: every assertion on the running daemon waits
for a condition with a generous deadline instead of demanding N beats in T
seconds, so a loaded or slow machine cannot fail a healthy daemon. What is
pinned: beats carry the right identity and keep coming; stdout stays quiet
while the detail log records; a 401 is reported once per transition and does
not stop the loop, and the stop summary then carries one WARNING naming the
lost lease; beats resume after a hang; missing env disables the daemon with
one line; a caller that dies without its EXIT trap (SIGKILL) takes the daemon
with it at the next beat, so it never holds the job's log pipe open; detail
lines carry a well-formed UTC stamp. One check is static: the script holds no
command substitution, because bash 5.2 can run the TERM trap from inside the
parser (the script's header says how).
"""

import os
import re
import signal
import subprocess
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_SCRIPT = REPO_ROOT / "hack" / "boskos_heartbeat.sh"

# Scaled-down interval so beats arrive quickly; correctness never depends on
# the loop hitting this period exactly.
TEST_INTERVAL_SECONDS = "0.2"
# Ceiling for any wait_until: far above worst-case scheduling noise, never
# slept in full on a healthy run.
WAIT_DEADLINE_SECONDS = 30.0
# Production ratio, asserted as arithmetic only: 30s beats against the ~5m
# Boskos reaper window leave a 10-beat budget.
PRODUCTION_INTERVAL_SECONDS = 30
PRODUCTION_EXPIRY_SECONDS = 5 * 60
LEASE_OWNER = "pull-kube-agents-smoke-test"
LEASE_NAME = "kube-agents-evals-4"
LEASE_STATE = "busy"
# A command or process substitution, which bash re-parses at expansion time
# (see test_no_command_substitution_while_the_trap_is_armed). `$((` is
# arithmetic and does not re-enter the parser, so it is allowed -- but only
# when the same line closes it with `))`: bash reads a `$((` that does not,
# as in `$((cd "$d" && ls) 2>&1)`, as a command substitution holding a
# subshell.
SUBSTITUTION_RE = re.compile(r"\$\((?!\()|`|<\(|>\(")
UNCLOSED_ARITHMETIC_RE = re.compile(r"\$\(\((?!.*\)\))")
# One beat in the detail log; curl's stderr shares the file, so only lines
# that carry a status are held to it.
DETAIL_LINE_RE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (ok|fail) http=\d{3}")
DETAIL_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# A POSIX TZ rule, UTC+14 (the sign is inverted in that syntax), so the test
# needs no tzdata: a stamp written in local time but labelled Z is 14h off.
FAR_FROM_UTC_TZ = "XYZ-14"
# Slack between the daemon writing a stamp and the test reading the clock.
STAMP_TOLERANCE_SECONDS = 300


def wait_until(condition, deadline=WAIT_DEADLINE_SECONDS):
    """Poll until condition() is truthy; return its last value."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    return condition()


def _has_exited(pid):
    """True once pid is gone, or is a zombie waiting for init to reap it.

    The kernel reparents an orphan to init (or a subreaper), which reaps it
    asynchronously; on the CI runner the daemon stays a zombie for a moment
    after it exits, and a zombie still answers kill(pid, 0).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
    ).stdout.strip()
    return state == "" or state.startswith("Z")


class _FakeBoskos(BaseHTTPRequestHandler):
    updates = []  # (name, owner, state)
    owner = LEASE_OWNER

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/update":
            self.send_response(404)
            self.end_headers()
            return
        q = parse_qs(parsed.query)
        name, owner, state = (q.get(k, [""])[0] for k in ("name", "owner", "state"))
        _FakeBoskos.updates.append((name, owner, state))
        # ranch.Update: owner mismatch -> OwnerNotMatch -> handlers.go 401.
        self.send_response(200 if owner == _FakeBoskos.owner else 401)
        self.end_headers()

    def log_message(self, *args):
        pass


class BoskosHeartbeatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBoskos)
        cls.host = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _FakeBoskos.updates.clear()
        _FakeBoskos.owner = LEASE_OWNER
        self.tmp = TemporaryDirectory()
        self.beat_log = Path(self.tmp.name) / "boskos-heartbeat.log"

    def tearDown(self):
        self.tmp.cleanup()

    def _spawn(self, **env_overrides):
        return self._popen(["bash", str(HEARTBEAT_SCRIPT)], env_overrides)

    def _spawn_caller(self, script, **env_overrides):
        """Run a bash caller that starts the daemon itself, same env as _spawn."""
        return self._popen(["bash", "-c", script], env_overrides)

    def _popen(self, argv, env_overrides):
        env = {
            **os.environ,
            "BOSKOS_HOST": self.host,
            "BOSKOS_RESOURCE_NAME": LEASE_NAME,
            "BOSKOS_OWNER_NAME": LEASE_OWNER,
            "BOSKOS_RESOURCE_STATE": LEASE_STATE,
            "BOSKOS_HEARTBEAT_INTERVAL_SECONDS": TEST_INTERVAL_SECONDS,
            "BOSKOS_HEARTBEAT_LOG": str(self.beat_log),
            **env_overrides,
        }
        return subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

    def _stop(self, proc):
        proc.send_signal(signal.SIGTERM)
        try:
            return proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.communicate()[0]

    def _logged_beats(self):
        """Detail-log lines that record a beat; curl's stderr shares the file."""
        if not self.beat_log.exists():
            return []
        return [ln for ln in self.beat_log.read_text().splitlines() if " http=" in ln]

    def _await_beats(self, n):
        count = wait_until(lambda: len(_FakeBoskos.updates) >= n)
        self.assertGreaterEqual(
            len(_FakeBoskos.updates), n,
            f"expected >= {n} beats within {WAIT_DEADLINE_SECONDS}s",
        )
        return count

    def test_beats_repeat_with_correct_identity(self):
        proc = self._spawn()
        self._await_beats(3)
        self._stop(proc)
        for name, owner, state in _FakeBoskos.updates:
            self.assertEqual((name, owner, state), (LEASE_NAME, LEASE_OWNER, LEASE_STATE))

    def test_stdout_stays_quiet_while_detail_log_records(self):
        proc = self._spawn()
        self._await_beats(5)
        stdout = self._stop(proc)
        # Job-log channel: start line and stop summary only.
        lines = [ln for ln in stdout.splitlines() if ln.strip()]
        self.assertLessEqual(len(lines), 3, f"stdout flooded:\n{stdout}")
        self.assertNotIn("WARNING", stdout, "a healthy lease must not warn")
        detail = wait_until(lambda: self.beat_log.read_text().splitlines())
        self.assertGreaterEqual(len(detail), 3)
        self.assertTrue(any(" ok http=200" in ln for ln in detail), detail)

    def test_detail_log_stamps_are_utc(self):
        proc = self._spawn(TZ=FAR_FROM_UTC_TZ)
        # Wait on the log itself, not the server's count: the daemon appends
        # a line only after curl returns, and a SIGTERM in between stops it
        # before the line lands.
        wait_until(lambda: len(self._logged_beats()) >= 2)
        self._stop(proc)
        beat_lines = self._logged_beats()
        self.assertGreaterEqual(len(beat_lines), 2, beat_lines)
        now = datetime.now(timezone.utc)
        for line in beat_lines:
            match = DETAIL_LINE_RE.fullmatch(line)
            self.assertIsNotNone(match, f"malformed detail line: {line!r}")
            stamped = datetime.strptime(match.group(1), DETAIL_STAMP_FORMAT).replace(tzinfo=timezone.utc)
            self.assertLess(abs((now - stamped).total_seconds()), STAMP_TOLERANCE_SECONDS,
                            f"stamp is not UTC: {line!r}")

    def test_owner_mismatch_logs_one_transition_and_loop_survives(self):
        _FakeBoskos.owner = "someone-else"  # every beat now 401s
        proc = self._spawn()
        self._await_beats(3)
        stdout = self._stop(proc)
        failed_lines = [ln for ln in stdout.splitlines() if "FAILED" in ln]
        self.assertEqual(len(failed_lines), 1, stdout)
        self.assertIn("http=401", failed_lines[0])
        # The stop summary is the end-of-run signal that the project was not
        # handed back (the wrapper's release swallows its own 401): exactly
        # one WARNING line, naming the resource, on the job log.
        warnings = [ln for ln in stdout.splitlines() if "WARNING" in ln]
        self.assertEqual(len(warnings), 1, stdout)
        self.assertIn(LEASE_NAME, warnings[0])
        self.assertIn("401", warnings[0])
        self.assertLess(stdout.index(warnings[0]), stdout.index("stopping for"),
                        "the WARNING precedes the stop summary line")

    def test_beats_resume_after_a_hang(self):
        # The production question is only "does a beat land after the hang,
        # inside the expiry budget" — the budget itself is arithmetic.
        self.assertGreater(
            PRODUCTION_EXPIRY_SECONDS // PRODUCTION_INTERVAL_SECONDS, 6,
            "a 3-minute hang (6 beats at 30s) must fit the reaper window",
        )
        proc = self._spawn()
        self._await_beats(2)
        os.kill(proc.pid, signal.SIGSTOP)
        time.sleep(1.5)  # several intervals of enforced silence
        frozen_count = len(_FakeBoskos.updates)
        os.kill(proc.pid, signal.SIGCONT)
        wait_until(lambda: len(_FakeBoskos.updates) > frozen_count)
        self._stop(proc)
        self.assertGreater(len(_FakeBoskos.updates), frozen_count,
                           "no beat resumed after SIGCONT")

    def test_stops_by_itself_when_the_caller_is_killed(self):
        # ci-eval-pr.sh and ci-teardown.sh kill the daemon from their EXIT
        # trap. A SIGKILLed caller never runs it; the daemon must then stop
        # on its own, because it holds the job's stdout pipe and Prow's
        # entrypoint waits on that pipe until the decoration timeout.
        pid_file = Path(self.tmp.name) / "daemon.pid"
        # The caller dies only once two beats are on record (bounded at the
        # wait_until ceiling, 600 polls of 50ms): the daemon stops itself at
        # its first beat after the caller is gone, so a caller on a fixed
        # timer leaves _await_beats(2) unsatisfiable on a machine slow enough
        # to land a single beat in that time.
        caller = (
            f'bash "{HEARTBEAT_SCRIPT}" & echo $! >"{pid_file}"; disown; '
            "for _ in $(seq 600); do "
            f'[ -f "{self.beat_log}" ] && [ "$(wc -l <"{self.beat_log}")" -ge 2 ] && break; '
            "sleep 0.05; done; "
            "kill -9 $$"
        )
        proc = self._spawn_caller(caller)
        self._await_beats(2)
        # communicate() returns only when the last holder of the pipe (the
        # daemon) has closed it; a daemon that outlives its caller hangs
        # here, exactly as it would hang the Prow job.
        try:
            stdout = proc.communicate(timeout=WAIT_DEADLINE_SECONDS)[0]
        except subprocess.TimeoutExpired:
            daemon_pid = int(pid_file.read_text().strip() or 0)
            if daemon_pid:
                os.kill(daemon_pid, signal.SIGKILL)
            proc.kill()
            self.fail("the daemon outlived its SIGKILLed caller")
        self.assertEqual(proc.returncode, -signal.SIGKILL, "the caller must die by SIGKILL")
        self.assertIn("started for", stdout)
        self.assertIn("stopping for", stdout)
        daemon_pid = int(pid_file.read_text().strip())
        self.assertTrue(wait_until(lambda: _has_exited(daemon_pid)),
                        "the daemon is still running after its caller died")

    def test_no_command_substitution_while_the_trap_is_armed(self):
        # bash 5.2 runs a pending trap from inside the parser when a signal
        # lands while it re-parses a $(...) or <(...) for expansion; the
        # script's header has the failure and the versions. The window is
        # microseconds per beat, so no behavioural test reaches it on purpose
        # and the construct is kept out of the script instead. Only full-line
        # comments are skipped: a trailing comment is not told apart from a
        # quoted "#", so it stays subject to the check.
        offenders = []
        lines = HEARTBEAT_SCRIPT.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue
            if SUBSTITUTION_RE.search(line) or UNCLOSED_ARITHMETIC_RE.search(line):
                offenders.append(f"{number}: {line.strip()}")
        self.assertEqual(offenders, [], "command substitution under an armed trap:\n"
                         + "\n".join(offenders))

    def test_disabled_without_boskos_env(self):
        proc = self._spawn(BOSKOS_HOST="")
        stdout, _ = proc.communicate(timeout=10)[0], proc.wait()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("disabled", stdout)
        self.assertEqual(_FakeBoskos.updates, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
