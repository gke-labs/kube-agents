"""Seam: the credential proxy's Microsoft Teams relay HTTP surface, over a real socket."""

from __future__ import annotations

import http.server
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from _seams import SCRIPTS_DIR

sys.path.insert(0, str(SCRIPTS_DIR))

import credential_proxy
from credential_proxy import CredentialProxyHandler, TeamsRelay


class TeamsEndpointsSeamTest(unittest.TestCase):
    def setUp(self):
        self.relay = TeamsRelay(
            app_id="app-id-123",
            app_password="secret-password",
            tenant_id="tenant-allowed-1",
        )
        CredentialProxyHandler.teams_relay = self.relay
        CredentialProxyHandler.max_request_bytes = 65536
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

    def test_inbound_event_enqueues_on_valid_tenant_and_rejects_unauthorized_tenant_with_403(self):
        status, body = self._call(
            "/v1/chat/teams/events",
            {
                "type": "message",
                "text": "status check",
                "channelData": {"tenant": {"id": "tenant-allowed-1"}},
            },
            method="POST",
        )
        self.assertEqual(status, 202)
        self.assertEqual(body.get("status"), "accepted")

        # Mismatched tenant -> 403 Forbidden
        status, _ = self._call(
            "/v1/chat/teams/events",
            {
                "type": "message",
                "text": "forged",
                "channelData": {"tenant": {"id": "other-tenant"}},
            },
            method="POST",
        )
        self.assertEqual(status, 403)

        # Absent tenant when TEAMS_TENANT_ID is configured -> 403 Forbidden
        status, _ = self._call(
            "/v1/chat/teams/events",
            {"type": "message", "text": "missing tenant"},
            method="POST",
        )
        self.assertEqual(status, 403)

        # Malformed activity field or untrusted serviceUrl -> 400 Bad Request
        status, _ = self._call(
            "/v1/chat/teams/events",
            {
                "type": "message",
                "serviceUrl": "https://attacker.trafficmanager.net/",
                "channelData": {"tenant": {"id": "tenant-allowed-1"}},
            },
            method="POST",
        )
        self.assertEqual(status, 400)

    def test_pull_and_settle_ack_and_nack(self):
        self._call(
            "/v1/chat/teams/events",
            {
                "type": "message",
                "text": "audit cluster",
                "conversation": {"id": "c-1", "tenantId": "tenant-allowed-1"},
            },
            method="POST",
        )
        status, body = self._call("/v1/chat/teams/events", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(body["event"]["event"]["text"], "audit cluster")
        receipt = body["event"]["receipt"]

        status, body = self._call(
            "/v1/chat/teams/events/ack", {"receipt": receipt}, method="POST"
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["settled"])

        status, body = self._call(
            "/v1/chat/teams/events/nack", {"receipt": "r-missing"}, method="POST"
        )
        self.assertEqual(status, 404)
        self.assertFalse(body["settled"])

    def test_api_forwarding_with_oauth_token_exchange_and_method_or_host_restrictions(self):
        upstream_requests = []

        class FakeBotFrameworkUpstream(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length).decode("utf-8")
                upstream_requests.append(
                    (self.path, self.headers.get("Authorization"), raw_body)
                )
                if self.path.endswith("/oauth2/v2.0/token"):
                    payload = {
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "access_token": "minted-bot-jwt-99",
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(payload).encode("utf-8"))
                    return
                if self.path == "/teams/v3/conversations/123/activities":
                    payload = {"id": "activity-42"}
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(payload).encode("utf-8"))
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, format, *args):
                del format, args

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeBotFrameworkUpstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        upstream_base = f"http://127.0.0.1:{upstream.server_port}"

        with (
            mock.patch.object(
                credential_proxy,
                "TEAMS_OAUTH_URL_TEMPLATE",
                f"{upstream_base}/{{tenant}}/oauth2/v2.0/token",
            ),
            mock.patch.object(
                credential_proxy,
                "is_trusted_teams_service_url",
                side_effect=lambda u: u == f"{upstream_base}/teams/",
            ),
        ):
            status, body = self._call(
                "/v1/chat/teams/api",
                {
                    "serviceUrl": f"{upstream_base}/teams/",
                    "endpoint": "v3/conversations/123/activities",
                    "method": "POST",
                    "payload": {"type": "message", "text": "reply"},
                },
                method="POST",
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["response"]["id"], "activity-42")
            self.assertEqual(len(upstream_requests), 2)
            self.assertEqual(
                upstream_requests[0][0], "/tenant-allowed-1/oauth2/v2.0/token"
            )
            self.assertIn("client_id=app-id-123", upstream_requests[0][2])
            self.assertEqual(
                upstream_requests[1][0], "/teams/v3/conversations/123/activities"
            )
            self.assertEqual(upstream_requests[1][1], "Bearer minted-bot-jwt-99")

        # Destructive method or non-activity endpoint (including /activities/history) -> 400 Bad Request
        status, _ = self._call(
            "/v1/chat/teams/api",
            {
                "serviceUrl": "https://smba.trafficmanager.net/teams/",
                "endpoint": "v3/conversations/123/activities/456",
                "method": "DELETE",
            },
            method="POST",
        )
        self.assertEqual(status, 400)

        status, _ = self._call(
            "/v1/chat/teams/api",
            {
                "serviceUrl": "https://smba.trafficmanager.net/teams/",
                "endpoint": "v3/conversations/123/activities/history",
                "method": "POST",
            },
            method="POST",
        )
        self.assertEqual(status, 400)

        status, _ = self._call(
            "/v1/chat/teams/api",
            {
                "serviceUrl": "https://smba.trafficmanager.net/teams/",
                "endpoint": "v3/conversations/123/members",
                "method": "GET",
            },
            method="POST",
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
