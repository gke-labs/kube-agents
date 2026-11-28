"""The openers that reach Session KV and the platform agent take no proxy from the environment.

Run:
  python3 -m unittest discover -s agents/platform/scripts -p 'test_session_kv_openers_refuse_proxy.py' -v

Each of these modules builds its opener when it is imported. A pod that has a
proxy variable has it at that time, so each module is loaded here in a new
process with `http_proxy` set. An opener that is built with
`urllib.request.ProxyHandler({})` then has no proxy handler; one that is built
without it has the proxy of the environment, and the request, with its bearer
token, goes to that proxy.

The second class holds that each call goes through that opener: with the proxy
variables at a closed port, the call must still reach a live local server. For
`platform_mcp_server` and `session_kv_server`, that test is in the test file of
the module.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import findings_nudge  # noqa: E402
import inventory_findings  # noqa: E402
import stall_watch  # noqa: E402
import upgrade_readiness_review  # noqa: E402
from test_mcp_package_contract import requires_mcp  # noqa: E402

PROXY = "http://egress.invalid:3128"
# The module and the name of its opener.
OPENERS = (
    ("findings_nudge", "_SESSION_KV_OPENER"),
    ("inventory_findings", "_SESSION_KV_OPENER"),
    ("stall_watch", "_SESSION_KV_OPENER"),
    ("upgrade_readiness_review", "_SESSION_KV_OPENER"),
)
# These modules import `mcp` or `fastapi`, which a bare checkout does not have.
MCP_OPENERS = (
    ("agent_common_server", "_AGENT_OPENER"),
    ("platform_mcp_server", "_SESSION_KV_OPENER"),
    ("session_kv_server", "_GATEWAY_OPENER"),
)
# Prints the http proxy of the module's opener, then that of a default opener
# of the same process, which shows that the variable reached the process.
PROBE = (
    "import importlib, sys, urllib.request\n"
    "module = importlib.import_module(sys.argv[1])\n"
    "def http_proxies(opener):\n"
    "    return [h.proxies.get('http') for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]\n"
    "print(http_proxies(getattr(module, sys.argv[2])), http_proxies(urllib.request.build_opener()))\n"
)


class SessionKvOpenersTest(unittest.TestCase):
    def _holds_no_proxy(self, openers):
        environment = {key: value for key, value in os.environ.items() if key.lower() not in ("no_proxy", "http_proxy")}
        environment["http_proxy"] = PROXY
        # `session_kv_server` makes its database when it is loaded, and the
        # default path is in the pod only.
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        environment["SESSION_KV_DB_PATH"] = str(Path(scratch.name) / "session_kv.db")
        for module, opener in openers:
            with self.subTest(module=module):
                done = subprocess.run(
                    [sys.executable, "-c", PROBE, module, opener],
                    cwd=SCRIPTS, env=environment, capture_output=True, text=True, check=False,
                )
                self.assertEqual(0, done.returncode, done.stderr)
                self.assertEqual(f"[] [{PROXY!r}]", done.stdout.strip())

    def test_no_opener_takes_the_proxy_of_the_environment(self):
        self._holds_no_proxy(OPENERS)

    @requires_mcp
    def test_no_opener_of_a_server_module_takes_the_proxy_of_the_environment(self):
        self._holds_no_proxy(MCP_OPENERS)


class _LiveServer:
    """A local HTTP server that records the method, the path and the `Authorization`
    header of each request and answers `answer` as JSON; and a proxy environment
    whose proxy is a closed port, for a test that the call does not take the proxy."""

    def __init__(self, case, answer=None):
        seen = self.seen = []
        body = json.dumps(answer or {}).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                seen.append((self.command, self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer  # noqa: N815

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        case.addCleanup(server.server_close)
        case.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}"
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            proxy = f"http://127.0.0.1:{closed.getsockname()[1]}"
        # A plain `urlopen` builds its opener at the first call and keeps it. The
        # test drops the kept one, so that a call that went back to `urlopen`
        # reads this environment and goes to the closed port.
        for patcher in (
            mock.patch.dict(os.environ, {"http_proxy": proxy, "HTTP_PROXY": proxy, "SESSION_KV_API_KEY": "kv-token"}),
            mock.patch.object(urllib.request, "_opener", None),
        ):
            patcher.start()
            case.addCleanup(patcher.stop)
        for name in ("no_proxy", "NO_PROXY"):
            os.environ.pop(name, None)


class CallsDoNotTakeTheProxyTest(unittest.TestCase):
    """Session KV is on the pod's loopback and the platform agent is an in-cluster
    Service. With a proxy variable and no NO_PROXY entry for the host, urllib hands
    the request and its bearer token to the proxy."""

    def test_the_findings_nudge_reaches_session_kv(self):
        server = _LiveServer(self)
        findings_nudge._request(server.url, "/v1/findings")
        self.assertEqual([("GET", "/v1/findings", "Bearer kv-token")], server.seen)

    def test_the_inventory_findings_reach_session_kv(self):
        server = _LiveServer(self)
        inventory_findings._request(server.url, "/v1/findings", {"findings": []})
        self.assertEqual([("POST", "/v1/findings", "Bearer kv-token")], server.seen)

    def test_the_stall_watch_reaches_session_kv(self):
        server = _LiveServer(self)
        with mock.patch.object(stall_watch, "SESSION_KV_URL", server.url):
            stall_watch.session_kv(stall_watch.HEALTHZ_PATH)
        self.assertEqual([("GET", stall_watch.HEALTHZ_PATH, "Bearer kv-token")], server.seen)

    def test_the_upgrade_readiness_review_reaches_session_kv(self):
        server = _LiveServer(self)
        with mock.patch.dict(os.environ, {upgrade_readiness_review.KV_ENDPOINT_ENV: server.url}):
            upgrade_readiness_review.kv_request(upgrade_readiness_review.KV_FINDINGS_PATH, {"findings": []})
        self.assertEqual([("POST", upgrade_readiness_review.KV_FINDINGS_PATH, "Bearer kv-token")], server.seen)

    @requires_mcp
    def test_the_agent_call_reaches_the_platform_agent(self):
        import agent_common_server

        server = _LiveServer(self, {"choices": [{"message": {"content": "done"}}]})
        with mock.patch.dict(os.environ, {"PLATFORM_API_URL": server.url, "API_SERVER_KEY": "gateway-key"}):
            self.assertEqual("done", agent_common_server.call_agent("platform", "hello"))
        self.assertEqual([("POST", "/v1/chat/completions", "Bearer gateway-key")], server.seen)


if __name__ == "__main__":
    unittest.main()
