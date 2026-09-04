"""Tests for hack/boskos_heartbeat.sh against a fake Boskos /update endpoint.

Behavioural, not timing-based: every assertion waits for a condition with a
generous deadline instead of demanding N beats in T seconds, so a loaded or
slow machine cannot fail a healthy daemon. What is pinned: beats carry the
right identity and keep coming; stdout stays quiet while the detail log
records; a 401 is reported once per transition and does not stop the loop;
beats resume after a hang; missing env disables the daemon with one line.
"""

import os
import signal
import subprocess
import threading
import time
import unittest
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


def wait_until(condition, deadline=WAIT_DEADLINE_SECONDS):
    """Poll until condition() is truthy; return its last value."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    return condition()


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
            ["bash", str(HEARTBEAT_SCRIPT)],
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
        detail = wait_until(lambda: self.beat_log.read_text().splitlines())
        self.assertGreaterEqual(len(detail), 3)
        self.assertTrue(any(" ok http=200" in ln for ln in detail), detail)

    def test_owner_mismatch_logs_one_transition_and_loop_survives(self):
        _FakeBoskos.owner = "someone-else"  # every beat now 401s
        proc = self._spawn()
        self._await_beats(3)
        stdout = self._stop(proc)
        failed_lines = [ln for ln in stdout.splitlines() if "FAILED" in ln]
        self.assertEqual(len(failed_lines), 1, stdout)
        self.assertIn("http=401", failed_lines[0])

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

    def test_disabled_without_boskos_env(self):
        proc = self._spawn(BOSKOS_HOST="")
        stdout, _ = proc.communicate(timeout=10)[0], proc.wait()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("disabled", stdout)
        self.assertEqual(_FakeBoskos.updates, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
