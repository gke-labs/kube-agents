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

    def test_a2a_event_routes_demand_the_a2a_chat_role(self):
        """The A2A event routes are the third caller's alone.

        The legacy chat caller is the LLM-driven Hermes pod — the injectable
        adversary of this repo's standing threat model — and with only the
        shared chat role it could pull and ack the A2A gateway's events,
        silently consuming user asks (turn-stealing). So the event routes
        demand the a2a-chat role: shell 403, chat 403, a2a-chat 200 (403,
        not 401 — the caller is known, the route is not theirs). Nothing
        pinned ROUTE_ROLES anywhere before this file.
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

        for refused in ("shell", "chat"):
            auth.role = refused
            status, body = self._get("/v1/chat/a2a/events")
            self.assertEqual(status, 403, f"role {refused} must not reach a2a events")
            self.assertEqual(body.get("code"), "CALLER_ROLE_FORBIDDEN")
            status, body = self._post("/v1/chat/a2a/events/ack", {"receipt": "X"})
            self.assertEqual(status, 403, f"role {refused} must not ack a2a events")

        auth.role = "a2a-chat"
        status, _ = self._get("/v1/chat/a2a/events")
        self.assertEqual(status, 200)

        # And the other direction: the A2A gateway may not pull or settle
        # the legacy consumer's events either — that is the route it would
        # use to steal the Hermes pod's turns.
        status, body = self._get("/v1/chat/events")
        self.assertEqual(status, 403, "a2a-chat must not reach the legacy events")
        self.assertEqual(body.get("code"), "CALLER_ROLE_FORBIDDEN")
        status, _ = self._post("/v1/chat/events/ack", {"receipt": "L1"})
        self.assertEqual(status, 403, "a2a-chat must not ack the legacy events")

    def test_the_api_passthrough_admits_both_chat_roles(self):
        """Both consumers post through the same credential; neither may pull
        the other's events, but /v1/chat/api belongs to both — and to
        nobody else."""
        from credential_proxy import Principal

        class RoleAuthenticator:
            authenticates = True
            role = "chat"

            def authenticate(self, headers):
                return Principal(workload="test-caller", role=self.role)

        auth = RoleAuthenticator()
        saved = self.handler.authenticator
        self.handler.authenticator = auth
        self.addCleanup(setattr, self.handler, "authenticator", saved)

        body = {"resource": ["spaces", "messages"], "method": "create", "arguments": {}}
        for admitted in ("chat", "a2a-chat"):
            auth.role = admitted
            status, _ = self._post("/v1/chat/api", body)
            self.assertEqual(status, 200, f"role {admitted} must reach the api passthrough")
        auth.role = "shell"
        status, _ = self._post("/v1/chat/api", body)
        self.assertEqual(status, 403)

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
