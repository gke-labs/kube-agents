"""The openers that reach Session KV and Hindsight take no proxy from the environment.

Run:
  python3 -m unittest discover -s agents/chat/scripts -p 'test_openers_refuse_proxy.py' -v

Each of these modules builds its opener when it is imported. A pod that has a
proxy variable has it at that time, so each module is loaded here in a new
process with `http_proxy` set. An opener that is built with
`urllib.request.ProxyHandler({})` then has no proxy handler; one that is built
without it has the proxy of the environment, and the request goes to that proxy.

The second class holds that each call goes through that opener: with the proxy
variables at a closed port, the call must still reach a live local server.
"""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import sys
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
# bootstrap_delivery imports modules that the image copies beside it from here.
PLATFORM_SCRIPTS = SCRIPTS.parents[1] / "platform" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(1, str(PLATFORM_SCRIPTS))

import bootstrap_delivery  # noqa: E402
import memory_file_import  # noqa: E402
import memory_ttl_curator  # noqa: E402

PROXY = "http://egress.invalid:3128"
# The module and the name of its opener.
OPENERS = (
    ("bootstrap_delivery", "_SESSION_KV_OPENER"),
    ("memory_file_import", "_HINDSIGHT_OPENER"),
    ("memory_ttl_curator", "_HINDSIGHT_OPENER"),
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


class OpenersTest(unittest.TestCase):
    def test_no_opener_takes_the_proxy_of_the_environment(self):
        environment = {key: value for key, value in os.environ.items() if key.lower() not in ("no_proxy", "http_proxy")}
        environment["http_proxy"] = PROXY
        environment["PYTHONPATH"] = os.pathsep.join(
            path for path in (str(PLATFORM_SCRIPTS), os.environ.get("PYTHONPATH")) if path
        )
        for module, opener in OPENERS:
            with self.subTest(module=module):
                done = subprocess.run(
                    [sys.executable, "-c", PROBE, module, opener],
                    cwd=SCRIPTS, env=environment, capture_output=True, text=True, check=False,
                )
                self.assertEqual(0, done.returncode, done.stderr)
                self.assertEqual(f"[] [{PROXY!r}]", done.stdout.strip())


class _LiveServer:
    """A local HTTP server that records the method, the path and the `Authorization`
    header of each request and answers `{}`; and a proxy environment whose proxy is
    a closed port, for a test that the call does not take the proxy."""

    def __init__(self, case):
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                seen.append((self.command, self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

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
            mock.patch.dict(os.environ, {"http_proxy": proxy, "HTTP_PROXY": proxy}),
            mock.patch.object(urllib.request, "_opener", None),
        ):
            patcher.start()
            case.addCleanup(patcher.stop)
        for name in ("no_proxy", "NO_PROXY"):
            os.environ.pop(name, None)


class CallsDoNotTakeTheProxyTest(unittest.TestCase):
    """Session KV is on the pod's loopback and Hindsight is an in-cluster Service.
    With a proxy variable and no NO_PROXY entry for the host, urllib hands the
    request to the proxy."""

    def test_the_shown_mark_reaches_session_kv(self):
        server = _LiveServer(self)
        with mock.patch.dict(os.environ, {bootstrap_delivery.SESSION_KV_AUTH_ENV: "kv-token"}):
            bootstrap_delivery._post_surfaced(server.url, "f-1", {})
        self.assertEqual([("POST", "/v1/findings/f-1/surfaced", "Bearer kv-token")], server.seen)

    def test_the_summary_request_reaches_session_kv(self):
        server = _LiveServer(self)
        environment = {bootstrap_delivery.SESSION_KV_AUTH_ENV: "kv-token", bootstrap_delivery.FINDINGS_ENDPOINT_ENV: server.url}
        with mock.patch.dict(os.environ, environment):
            bootstrap_delivery._post_cron_report("compliance-audit", "Compliance")
        self.assertEqual([("POST", bootstrap_delivery.CRON_REPORTS_PATH, "Bearer kv-token")], server.seen)

    def test_the_memory_file_import_reaches_hindsight(self):
        server = _LiveServer(self)
        memory_file_import.Hindsight(server.url, "hindsight-key").call("GET", "/banks", retries=1)
        self.assertEqual([("GET", "/v1/default/banks", "Bearer hindsight-key")], server.seen)

    def test_the_memory_ttl_curator_reaches_hindsight(self):
        server = _LiveServer(self)
        memory_ttl_curator.Hindsight(server.url, "hindsight-key").call("GET", "/banks", retries=1)
        self.assertEqual([("GET", "/v1/default/banks", "Bearer hindsight-key")], server.seen)


if __name__ == "__main__":
    unittest.main()
