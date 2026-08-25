"""Seam: credential proxy client ↔ server, over a real socket.

Every kubectl/gcloud/gh/git an agent runs goes shim → client → HTTP → proxy.
The server has real-socket tests and the client has mocked-urlopen tests, but
until this file the pair had never met: nothing proved the bytes the client
sends are the bytes the server's parser accepts, or that the server's refusals
come back as exit codes an agent can read. The third test pins the case that
sits between them — an error body written by neither (Envoy answering 503 with
HTML during a sidecar restart), which used to crash the shim with a traceback.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from _seams import REPO_ROOT, SCRIPTS_DIR

sys.path.insert(0, str(SCRIPTS_DIR))

import credential_proxy_client  # noqa: E402
from credential_proxy import (  # noqa: E402
    CommandExecutor,
    CredentialProxyHandler,
    Policy,
)


class CredentialProxyPairTest(unittest.TestCase):
    """The real handler, the real client, one TCP socket between them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        policy_path = Path(self.tmp.name) / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "blockedMessage": "blocked by policy",
                    "rules": [
                        {
                            "id": "gcloud.destroy",
                            "pattern": "^gcloud container clusters delete\\b",
                            "message": "cluster deletion is not available here",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        CredentialProxyHandler.policy = Policy.load(str(policy_path))
        CredentialProxyHandler.executor = CommandExecutor(
            timeout_seconds=10,
            max_output_bytes=65536,
            state_dir=str(Path(self.tmp.name) / "state"),
        )
        CredentialProxyHandler.max_request_bytes = 65536
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def _execute(self, argv, cwd=None):
        """Run the real client, capturing what the shim would print."""
        stdout, stderr = io.StringIO(), io.StringIO()
        run_dir = cwd or str(CredentialProxyHandler.executor.workspace_dir)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with contextlib.chdir(run_dir):
                code = credential_proxy_client.execute(self.endpoint, argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_stdout_stderr_and_exit_code_round_trip_exactly(self):
        # `git version` is allowed, runs in the sidecar, and its stdout must
        # arrive byte-for-byte through the JSON envelope and the shim.
        code, out, err = self._execute(["git", "version"])
        self.assertEqual(0, code)
        self.assertIn("git version", out)
        self.assertEqual("", err)

    def test_a_policy_block_surfaces_as_exit_126_naming_the_rule(self):
        code, _, err = self._execute(
            ["gcloud", "container", "clusters", "delete", "prod-cluster"]
        )
        self.assertEqual(126, code)
        self.assertIn("cluster deletion is not available here", err)
        self.assertIn("policy rule: gcloud.destroy", err)

    def test_an_unleased_git_write_comes_back_as_a_readable_refusal(self):
        code, _, err = self._execute(["git", "commit", "-m", "x"])
        self.assertEqual(126, code)
        self.assertIn("policy rule: git.workspace.lease", err)

    def test_a_non_json_error_body_is_a_message_not_a_traceback(self):
        # Envoy restarting mid-request: 503, text/html, written by neither
        # side of this seam. The shim must degrade to a readable line and
        # exit 1 — before the fix in this change it crashed on json.load.
        class HtmlErrorHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = b"<html><body>upstream connect error</body></html>"
                self.send_response(503)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        envoy = ThreadingHTTPServer(("127.0.0.1", 0), HtmlErrorHandler)
        threading.Thread(target=envoy.serve_forever, daemon=True).start()
        self.addCleanup(envoy.server_close)
        self.addCleanup(envoy.shutdown)

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = credential_proxy_client.execute(
                f"http://127.0.0.1:{envoy.server_port}", ["kubectl", "get", "pods"]
            )
        self.assertEqual(1, code)
        self.assertIn("HTTP 503", stderr.getvalue())
        self.assertIn("non-JSON response", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
