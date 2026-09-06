"""Seam: A2A Google Chat ingress — the gateway's relay routes on the proxy.

The A2A gateway consumes Chat through the credential proxy the way the
legacy path does, but from its OWN GoogleChatRelay instance on its own
subscription — two consumers on one subscription split deliveries randomly,
so the isolation between /v1/chat/events and /v1/chat/a2a/events IS the
design (docs/designs/spec-chatops-gateway.md, "The Google Chat adapter").
The Go adapter is driven here as plain HTTP, which is all it is.

The API passthrough stays shared: both relay instances hold the same app
credential, and /v1/chat/api must work on an install that arms only the A2A
subscription.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from _seams import SCRIPTS_DIR

sys.path.insert(0, str(SCRIPTS_DIR))


class FakeRelay:
    """Queue-backed stand-in for one GoogleChatRelay instance."""

    def __init__(self, tag):
        self.tag = tag
        self.events = []
        self.settled = []  # (receipt, acknowledged)
        self.api_calls = []

    def pull(self):
        return self.events.pop(0) if self.events else None

    def settle(self, receipt, acknowledge):
        self.settled.append((receipt, acknowledge))
        return True

    def api_call(self, resource, method, arguments):
        self.api_calls.append((resource, method, arguments))
        return {"served_by": self.tag}


class A2AChatIngressSeam(unittest.TestCase):
    def setUp(self):
        from credential_proxy import CredentialProxyHandler

        self.handler = CredentialProxyHandler
        self.legacy = FakeRelay("legacy")
        self.a2a = FakeRelay("a2a")
        CredentialProxyHandler.chat_relay = self.legacy
        CredentialProxyHandler.a2a_chat_relay = self.a2a
        CredentialProxyHandler.max_request_bytes = 65536
        self.addCleanup(setattr, CredentialProxyHandler, "chat_relay", None)
        self.addCleanup(setattr, CredentialProxyHandler, "a2a_chat_relay", None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def _post(self, path, body):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def test_a2a_events_route_pulls_from_the_a2a_relay_only(self):
        self.legacy.events.append({"receipt": "L1", "data": "bGVnYWN5"})
        self.a2a.events.append({"receipt": "A1", "data": "YTJh"})

        status, body = self._get("/v1/chat/a2a/events")
        self.assertEqual(status, 200)
        self.assertEqual(body["event"]["receipt"], "A1")

        status, body = self._get("/v1/chat/events")
        self.assertEqual(status, 200)
        self.assertEqual(
            body["event"]["receipt"],
            "L1",
            "the legacy route must still serve the legacy subscription",
        )

    def test_a2a_ack_and_nack_settle_on_the_a2a_relay(self):
        status, body = self._post("/v1/chat/a2a/events/ack", {"receipt": "A1"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"settled": True})
        status, _ = self._post("/v1/chat/a2a/events/nack", {"receipt": "A2"})
        self.assertEqual(status, 200)
        self.assertEqual(self.a2a.settled, [("A1", True), ("A2", False)])
        self.assertEqual(
            self.legacy.settled, [], "an A2A settle must never touch the legacy receipts"
        )

    def test_a2a_routes_refuse_when_the_a2a_relay_is_unarmed(self):
        self.handler.a2a_chat_relay = None
        status, _ = self._get("/v1/chat/a2a/events")
        self.assertEqual(status, 503)
        status, _ = self._post("/v1/chat/a2a/events/ack", {"receipt": "X"})
        self.assertEqual(status, 503)

    def test_a2a_routes_demand_the_chat_role(self):
        """The new family's whole authentication is the /v1/chat/ prefix rule.

        Nothing else pinned ROUTE_ROLES before this file, and the A2A gateway
        is a third caller whose reachability depends on it: the sandbox's
        shell role must be refused here (403, not 401 — the caller is known,
        the route is not theirs), and the chat role admitted. The two
        directions verify each other — a vacuous rule would fail the 403 half.
        """
        from credential_proxy import Principal

        class RoleAuthenticator:
            authenticates = True
            role = "shell"

            def authenticate(self, headers):
                return Principal(workload="test-caller", role=self.role)

        auth = RoleAuthenticator()
        saved = self.handler.authenticator
        self.handler.authenticator = auth
        self.addCleanup(setattr, self.handler, "authenticator", saved)

        status, body = self._get("/v1/chat/a2a/events")
        self.assertEqual(status, 403)
        self.assertEqual(body.get("code"), "CALLER_ROLE_FORBIDDEN")

        auth.role = "chat"
        status, body = self._get("/v1/chat/a2a/events")
        self.assertEqual(status, 200)

    def test_api_passthrough_works_with_only_the_a2a_relay_armed(self):
        self.handler.chat_relay = None
        status, body = self._post(
            "/v1/chat/api",
            {"resource": ["spaces", "messages"], "method": "create", "arguments": {}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["response"], {"served_by": "a2a"})


if __name__ == "__main__":
    unittest.main()
