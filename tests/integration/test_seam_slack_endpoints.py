"""Seam: the credential proxy's Slack relay HTTP surface, over a real socket.

The Slack ingress pair is patch ↔ proxy. The patch side's closures are covered
by `test_slack_relay_patch.py` against a mocked urlopen; what has never been
tested is the wire those closures actually speak — the proxy's
`/v1/chat/slack/*` endpoints as served by the real `CredentialProxyHandler`.
This file drives that surface over a real socket with a fake `SlackRelay`
behind it, pinning the exact response shapes the patch depends on: the
bootstrap workspace list, pull → event, settle semantics including the
unknown-receipt 404 (the "proxy restarted between pull and ack" case the
patch's loop must survive), and the 502-with-`slack`-fields error envelope the
patch turns back into a `SlackApiError`.

Deliberately not driven here: the patch's own `install()` and relay loop —
loading them requires faking eight hermes/bolt modules, and the closures'
behaviour against this exact wire contract is what their unit tests already
pin. The seam this file owns is the wire.
"""

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

from credential_proxy import CredentialProxyHandler  # noqa: E402


class FakeSlackRelay:
    def __init__(self):
        self.events = []
        self.settled = []
        self.api_error = None
        self._receipts = {"r-live": True}

    def bootstrap(self):
        return [{"teamId": "T1", "teamName": "acme", "botUserId": "B1"}]

    def pull(self, timeout_seconds: int = 20):
        return self.events.pop(0) if self.events else None

    def settle(self, receipt, acknowledge):
        if receipt not in self._receipts:
            return False
        self.settled.append((receipt, acknowledge))
        return True

    def api_call(self, team_id, method, arguments):
        if self.api_error is not None:
            raise self.api_error
        return {"ok": True, "ts": "123.456", "method": method, "team": team_id}


class _SlackApiErrorLike(Exception):
    """Shaped like `slack_sdk.errors.SlackApiError`, which is what the relay raises.

    `_slack_error_fields` reads `exc.response.data`; an earlier revision of
    this fixture carried only a `status_code`, so the extractor found no
    payload, returned None, and the endpoint emitted the generic body with no
    `slack` key at all -- leaving the assertion below unable to fail.
    """

    def __init__(self, message, data):
        super().__init__(message)
        self.response = _SlackResponseLike(data)


class _SlackResponseLike:
    def __init__(self, data):
        self.data = data


class SlackEndpointsSeamTest(unittest.TestCase):
    def setUp(self):
        self.relay = FakeSlackRelay()
        CredentialProxyHandler.slack_relay = self.relay
        CredentialProxyHandler.max_request_bytes = 65536
        CredentialProxyHandler.slack_max_request_bytes = 65536
        self.addCleanup(setattr, CredentialProxyHandler, "slack_relay", None)
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
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_bootstrap_returns_the_workspace_list_the_patch_tokenizes(self):
        status, body = self._call("/v1/chat/slack/bootstrap", payload={})
        self.assertEqual(200, status)
        self.assertEqual("T1", body["workspaces"][0]["teamId"])

    def test_pull_hands_out_an_event_then_signals_empty(self):
        self.relay.events.append({"receipt": "r-live", "envelope": {"type": "events_api"}})
        status, body = self._call("/v1/chat/slack/events")
        self.assertEqual(200, status)
        self.assertEqual("r-live", body["event"]["receipt"])
        status, body = self._call("/v1/chat/slack/events")
        self.assertEqual(200, status)
        self.assertIsNone(body["event"])

    def test_ack_settles_a_live_receipt_and_404s_an_unknown_one(self):
        status, body = self._call(
            "/v1/chat/slack/events/ack", payload={"receipt": "r-live"}
        )
        self.assertEqual(200, status)
        self.assertTrue(body["settled"])
        self.assertEqual([("r-live", True)], self.relay.settled)

        # The proxy-restarted-between-pull-and-ack case: the receipt is gone.
        # 404-with-settled-false is the contract; the patch's loop reads it
        # and moves on rather than crashing — a 5xx here would kill ingress.
        status, body = self._call(
            "/v1/chat/slack/events/ack", payload={"receipt": "r-stale"}
        )
        self.assertEqual(404, status)
        self.assertFalse(body["settled"])

    def test_an_api_failure_becomes_the_502_slack_envelope(self):
        self.relay.api_error = _SlackApiErrorLike(
            "channel_not_found",
            {
                "ok": False,
                "error": "channel_not_found",
                "needed": "channels:read",
                "provided": "chat:write",
                # Outside SLACK_ERROR_DIAGNOSTIC_FIELDS, and deliberately
                # secret-shaped: this payload answered a call made with the
                # relay's own credential, and the envelope below is both
                # logged and handed to the agent.
                "response_metadata": {"token": "xoxb-not-for-the-agent"},
            },
        )
        status, body = self._call(
            "/v1/chat/slack/api",
            payload={"teamId": "T1", "method": "chat.postMessage", "arguments": {}},
        )
        self.assertEqual(502, status)
        # The patch rebuilds SlackApiError from this envelope; without the
        # fields a transport fault and a Slack rejection are indistinguishable.
        # Asserted whole, not by membership: `error` alone is in every 4xx and
        # 5xx this handler emits, so deleting the `slack` key would not have
        # shown up here.
        self.assertEqual(
            {
                "ok": False,
                "error": "channel_not_found",
                "needed": "channels:read",
                "provided": "chat:write",
            },
            body["slack"],
        )
        self.assertNotIn("xoxb-not-for-the-agent", json.dumps(body))

    def test_a_successful_api_call_round_trips_method_and_team(self):
        status, body = self._call(
            "/v1/chat/slack/api",
            payload={
                "teamId": "T1",
                "method": "chat.postMessage",
                "arguments": {"channel": "C1", "text": "hi"},
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("chat.postMessage", body["response"]["method"])
        self.assertEqual("T1", body["response"]["team"])


if __name__ == "__main__":
    unittest.main()
