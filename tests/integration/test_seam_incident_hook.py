"""Seam: the incident_context gateway hook ↔ the real session KV server.

The hook rewrites every inbound chat message using what the KV server knows
about the thread, and it is deliberately fail-open — a KV that is down, slow,
or unauthenticated must never break normal message flow. Until this test, the
hook had no test of any kind, and its fail-open shape means a broken lookup is
silent by construction. Here the real server answers a real HTTP lookup from
the real hook; only the gateway event object is a stub, because the gateway is
the component this seam deliberately excludes.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from _seams import API_KEY, KVServer, REPO_ROOT, free_port, http_json

CHAT_ID = "spaces/AAA-space"
THREAD_ID = "spaces/AAA-space/threads/thr-1"
REPORT = "## Issue\npayments-api is OOMKilled\n## Options\n1. raise the limit"


class _Source:
    def __init__(self, platform="google_chat", chat_id=CHAT_ID, thread_id=THREAD_ID):
        self.platform = platform
        self.chat_id = chat_id
        self.thread_id = thread_id


class _Event:
    def __init__(self, text, source=None, raw_message=None):
        self.text = text
        self.source = source or _Source()
        self.raw_message = raw_message


def _load_hook(case, kv_url):
    """Import the plugin against a specific KV URL, fresh each time.

    Environment edits are registered on the test case for cleanup: this file
    runs inside a discovery sweep, and a leaked SESSION_KV_URL would point
    every later module's clients at a dead port.
    """
    for name, value in (("SESSION_KV_URL", kv_url), ("SESSION_KV_API_KEY", API_KEY)):
        previous = os.environ.get(name)
        os.environ[name] = value
        if previous is None:
            case.addCleanup(os.environ.pop, name, None)
        else:
            case.addCleanup(os.environ.__setitem__, name, previous)
    plugin_dir = REPO_ROOT / "agents" / "platform" / "plugins"
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))
    sys.modules.pop("incident_context", None)
    case.addCleanup(sys.modules.pop, "incident_context", None)
    return importlib.import_module("incident_context")


class IncidentHookSeamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.kv = KVServer(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.kv.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.hook = _load_hook(self, self.kv.url)

    def test_a_threaded_reply_gets_the_stored_report_prepended(self):
        status, _ = http_json(
            f"{self.kv.url}/v1/incidents",
            payload={"chat_id": CHAT_ID, "thread_id": THREAD_ID, "report": REPORT},
        )
        self.assertEqual(200, status)
        result = self.hook.on_inbound(event=_Event("what are my options?"))
        self.assertIsNotNone(result)
        self.assertEqual("rewrite", result["action"])
        self.assertIn("payments-api is OOMKilled", result["text"])
        self.assertIn("[User reply in thread]: what are my options?", result["text"])
        self.assertIn("UNTRUSTED DATA", result["text"])

    def test_a_thread_with_no_incident_and_no_recent_reports_is_untouched(self):
        source = _Source(thread_id="spaces/BBB-other/threads/none", chat_id="spaces/BBB-other")
        self.assertIsNone(self.hook.on_inbound(event=_Event("hello", source=source)))

    def test_a_slash_command_never_reaches_the_kv_server(self):
        # A recording stub in place of the KV: the hook must not even look.
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(self.path)
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        stub = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        hook = _load_hook(self, f"http://127.0.0.1:{stub.server_port}")
        self.assertIsNone(hook.on_inbound(event=_Event("/hermes sethome")))
        self.assertIsNone(hook.on_inbound(event=_Event("<@U123ABC> /hermes status")))
        self.assertEqual([], hits)

    def test_a_hung_kv_server_fails_open_within_the_timeout_budget(self):
        # The hook's own timeout is 2s per lookup; a wedged server must yield
        # None, not a hang and not an exception. Two lookups can fire (thread
        # then recent), so the budget is double the timeout plus slack.
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                time.sleep(10)

            def log_message(self, *args):
                pass

        stub = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        hook = _load_hook(self, f"http://127.0.0.1:{stub.server_port}")
        started = time.monotonic()
        self.assertIsNone(hook.on_inbound(event=_Event("is this thing on?")))
        self.assertLess(time.monotonic() - started, 6.0)


if __name__ == "__main__":
    unittest.main()
