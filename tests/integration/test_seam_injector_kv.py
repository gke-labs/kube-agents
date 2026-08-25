"""Seam: the Go event-watcher injector ↔ the Python session KV server.

The two halves of the alert hand-off have only ever been tested against
same-language fakes of each other — Go's `injector_test.go` against an
httptest stub, Python's `test_session_kv_server.py` against a TestClient. The
existing drift scar (`kind_of_object` OR `kindOfObject` accepted server-side)
is what that gap looks like once it has already bitten. This test compiles and
runs the real Go client (`injector_integration_test.go`, env-gated in the
watcher package) against the real server over a real socket.

Skips when no Go toolchain is available, which makes a laptop without `go`
report OK on four tests it never ran -- install one before you believe a green
run here. CI's `test` and `coverage` jobs both set Go up for exactly that
reason, since this tier gates from inside `make test-python`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from _seams import API_KEY, KVServer, REPO_ROOT, http_json


def _go_binary():
    found = shutil.which("go")
    if found:
        return found
    # PATH plus the one conventional system location. Never a /tmp path: a
    # world-writable directory is an execution hazard on shared hosts.
    candidate = "/usr/local/go/bin/go"
    return candidate if Path(candidate).exists() else None


GO = _go_binary()


@unittest.skipUnless(GO, "no Go toolchain on PATH")
class InjectorKVSeamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tmp_path = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run_go(self, kv, test_filter, extra_env=None):
        env = dict(os.environ)
        env.update(
            {
                "SESSION_KV_INTEGRATION_URL": kv.url,
                "SESSION_KV_INTEGRATION_TOKEN": API_KEY,
                # A writable cache whatever the runner's HOME looks like.
                "GOCACHE": str(self.tmp_path / "gocache"),
                "GOFLAGS": "-count=1",
            }
        )
        if extra_env:
            env.update(extra_env)
        completed = subprocess.run(
            [GO, "test", "-run", test_filter, "./cmd/k8s-event-watcher/"],
            cwd=str(REPO_ROOT / "k8s-operator"),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(
            0,
            completed.returncode,
            f"go test {test_filter} failed:\n{completed.stdout}\n{completed.stderr}",
        )
        return completed.stdout

    def test_the_real_client_creates_a_session_and_injects_through_the_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            kv = KVServer(Path(tmp), env={"PLATFORM_API_URL": "http://127.0.0.1:1"})
            try:
                self._run_go(kv, "TestLiveKVCreateAndInject")
                # The payload crossed the double-JSON envelope: the server
                # derived Critical severity from reason=CrashLoopBackOff, so
                # the quota row for Critical exists and counts one send.
                status, quota = http_json(f"{kv.url}/v1/alert-quota")
                self.assertEqual(200, status)
                flattened = str(quota)
                self.assertIn("Critical", flattened)
            finally:
                kv.stop()

    def test_a_spent_quota_reads_back_as_suppressed_on_the_go_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            kv = KVServer(
                Path(tmp),
                env={
                    "PLATFORM_API_URL": "http://127.0.0.1:1",
                    "ALERT_DAILY_LIMIT_CRITICAL": "1",
                },
            )
            try:
                self._run_go(
                    kv,
                    "TestLiveKVQuotaSuppression",
                    extra_env={"SESSION_KV_INTEGRATION_QUOTA": "1"},
                )
            finally:
                kv.stop()

    def test_a_bad_bearer_is_an_error_on_both_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            kv = KVServer(Path(tmp), env={"PLATFORM_API_URL": "http://127.0.0.1:1"})
            try:
                self._run_go(kv, "TestLiveKVBadBearer")
            finally:
                kv.stop()

    def test_a_200_with_an_empty_body_reads_as_delivered(self):
        """The documented fallback semantics, pinned against drift.

        `injector.go` deliberately reads an empty or unparseable 2xx body as
        "delivered" — a daemon predating the status field always delivers, and
        guessing "dropped" would reopen every incident on every sighting. The
        daemon never answers empty today, so this pins the CLIENT semantics
        with a stub — the one assertion in this file where the fake sits on
        the Python side of the seam.
        """
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        class EmptyBody(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                if self.path == "/sessions":
                    body = b'{"sessionID": "k8s-evt-stub0001"}'
                    self.send_response(201)
                else:
                    body = b""
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        stub = ThreadingHTTPServer(("127.0.0.1", 0), EmptyBody)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)

        # An empty 2xx body is a tolerated answer, not a failure: the Go test
        # run against this stub must get past session-create and the inject
        # transport, and fail only on its status expectation (the stub answers
        # "" where the real daemon answers "injected"). What must NOT appear
        # is a transport or decode error — that would mean the client stopped
        # tolerating the documented legacy-daemon shape.
        env = dict(os.environ)
        env.update(
            {
                "SESSION_KV_INTEGRATION_URL": f"http://127.0.0.1:{stub.server_port}",
                "SESSION_KV_INTEGRATION_TOKEN": API_KEY,
                "GOCACHE": str(self.tmp_path / "gocache"),
            }
        )
        completed = subprocess.run(
            [GO, "test", "-run", "TestLiveKVCreateAndInject", "./cmd/k8s-event-watcher/"],
            cwd=str(REPO_ROOT / "k8s-operator"),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        combined = completed.stdout + completed.stderr
        # No transport/decode error from the client (its error strings all
        # carry the "injector:" prefix)...
        self.assertNotIn("injector: POST inject", combined)
        self.assertNotIn("injector: decode", combined)
        # ...and the run failed only on the Go test's own status expectation,
        # which proves the empty body was read as a status, not an error.
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("expected status injected", completed.stdout)


if __name__ == "__main__":
    unittest.main()
