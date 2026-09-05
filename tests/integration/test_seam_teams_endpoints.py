"""Seam: the credential proxy's Microsoft Teams relay HTTP surface, over a real socket."""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from _seams import SCRIPTS_DIR

sys.path.insert(0, str(SCRIPTS_DIR))

from credential_proxy import CredentialProxyHandler


class FakeTeamsRelay:
    def __init__(self):
        self.events = []
        self.settled = []
        self.api_error = None
        self._receipts = {"r-live": True}

    def enqueue_event(self, event):
        self.events.append(event)
        return True

    def pull(self, timeout_seconds: int = 20):
        return {"event": self.events.pop(0), "receipt": "r-live"} if self.events else None

    def settle(self, receipt, acknowledge):
        if receipt not in self._receipts:
            return False
        self.settled.append((receipt, acknowledge))
        return True

    def api_call(self, service_url, path, method="POST", payload=None):
        if self.api_error is not None:
            raise self.api_error
        return {"status": "ok", "serviceUrl": service_url, "endpoint": path}


class TeamsEndpointsSeamTest(unittest.TestCase):
    def setUp(self):
        self.relay = FakeTeamsRelay()
        CredentialProxyHandler.teams_relay = self.relay
        CredentialProxyHandler.max_request_bytes = 65536
        CredentialProxyHandler.teams_max_request_bytes = 65536
        self.addCleanup(setattr, CredentialProxyHandler, "teams_relay", None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def _call(self, path, payload=None, method=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method=method or ("POST" if body is not None else "GET"),
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.status, json.load(exc)

    def test_inbound_webhook_event_enqueues_and_returns_accepted(self):
        status, body = self._call(
            "/api/v1/teams/events",
            {"type": "message", "text": "status check"},
            method="POST",
        )
        self.assertEqual(status, 202)
        self.assertEqual(body.get("status"), "accepted")
        self.assertEqual(len(self.relay.events), 1)

    def test_pull_returns_event_and_receipt(self):
        self.relay.events.append({"type": "message", "text": "audit cluster"})
        status, body = self._call("/v1/chat/teams/events", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(body["event"]["event"]["text"], "audit cluster")
        self.assertEqual(body["event"]["receipt"], "r-live")

    def test_settle_ack_and_nack(self):
        status, body = self._call(
            "/v1/chat/teams/events/ack", {"receipt": "r-live"}, method="POST"
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["settled"])

        status, body = self._call(
            "/v1/chat/teams/events/nack", {"receipt": "r-missing"}, method="POST"
        )
        self.assertEqual(status, 404)
        self.assertFalse(body["settled"])

    def test_api_forwarding(self):
        status, body = self._call(
            "/v1/chat/teams/api",
            {
                "serviceUrl": "https://smba.trafficmanager.net/teams/",
                "endpoint": "v3/conversations/123/activities",
                "method": "POST",
                "payload": {"type": "message", "text": "reply"},
            },
            method="POST",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["response"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
