"""The broker's platform Pub/Sub relay, driven over a real socket.

The gateway's Pub/Sub platform adapter (agentplugins/pubsub-platform) pulls its
subscriptions through `/v1/pubsub/gcp/`, under the `chat` role, confined to the
subscription set the operator rendered. A fake Google upstream answers the
Pub/Sub calls; `GoogleApiRelay.fetch` runs for real with the connection pointed
at the fake and the token fixed.
"""

import http.client
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import credential_proxy
import watcher_policy
from credential_proxy import CredentialProxyHandler, Policy

CALLER = "system:serviceaccount:kubeagents-system:platform-agent-gateway-agent"
ALERTS = "projects/kagents-dev/subscriptions/gke-stockout-alerts-sub"
SECOND = "projects/kagents-dev/subscriptions/second-route-sub"
DRIFT = "projects/kagents-dev/subscriptions/platform-agent-drift-audit-sub"
PREFIX = watcher_policy.PLATFORM_PUBSUB_PREFIX
PULL = f"{PREFIX}pubsub.googleapis.com/v1/{ALERTS}:pull"


class PlatformPubsubRelayOverTheSocketTest(unittest.TestCase):
    def setUp(self):
        owner = self
        self.google_requests = []
        # Extra bytes on every pull answer, for the response-cap tests.
        self.pull_padding = 0

        class Google(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _answer(self, body, status=200):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                owner.google_requests.append(("GET", self.path, dict(self.headers), b""))
                self._answer(b'{"name":"' + ALERTS.encode() + b'"}')

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                owner.google_requests.append(("POST", self.path, dict(self.headers), self.rfile.read(length)))
                if self.path.endswith(":pull"):
                    self._answer(json.dumps({"receivedMessages": [{"ackId": "ack-1", "message": {
                        "data": "eyJ4IjogMX0=", "messageId": "m-1",
                        "attributes": {"k": "v", "pad": "x" * owner.pull_padding}}}]}).encode())
                else:
                    self._answer(b"{}")

            def log_message(self, *_):
                return

        self.google = ThreadingHTTPServer(("127.0.0.1", 0), Google)
        threading.Thread(target=self.google.serve_forever, daemon=True).start()
        self.addCleanup(self.google.server_close)
        self.addCleanup(self.google.shutdown)
        google_port = self.google.server_address[1]

        class FakeRelay(credential_proxy.GoogleApiRelay):
            def authorization_header(self):
                return "Bearer broker-token"

            def connection(self, host):
                owner.upstream_hosts.append(host)
                return http.client.HTTPConnection("127.0.0.1", google_port, timeout=5)

        self.upstream_hosts = []
        for attribute in ("api_relay", "authenticator", "policy", "max_request_bytes",
                          "watcher_subscription", "platform_pubsub_subscriptions"):
            self.addCleanup(
                self._restore, attribute, attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        CredentialProxyHandler.api_relay = FakeRelay()
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.max_request_bytes = 1 << 20
        CredentialProxyHandler.watcher_subscription = DRIFT
        CredentialProxyHandler.platform_pubsub_subscriptions = frozenset({ALERTS, SECOND})

        authenticator = credential_proxy.ServiceAccountAuthenticator(
            audience_roles={
                "kubeagents-credential-proxy": credential_proxy.CALLER_ROLE_SHELL,
                "kubeagents-credential-proxy-chat": credential_proxy.CALLER_ROLE_CHAT,
                "kubeagents-credential-proxy-watcher": credential_proxy.CALLER_ROLE_WATCHER,
            },
            allowed_callers=frozenset({CALLER}),
            api_host="10.0.0.1", api_port="443", ca_file="", token_file="/nonexistent", cache_seconds=0.0,
        )
        roles = {"shell-token": credential_proxy.CALLER_ROLE_SHELL,
                 "chat-token": credential_proxy.CALLER_ROLE_CHAT,
                 "watcher-token": credential_proxy.CALLER_ROLE_WATCHER}

        def fake_review(token):
            if token not in roles:
                raise credential_proxy.AuthenticationError("not our token")
            return credential_proxy.Principal(workload=CALLER, uid="sa-uid", role=roles[token])

        authenticator._review = fake_review
        CredentialProxyHandler.authenticator = authenticator
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    @staticmethod
    def _restore(attribute, was_set, original):
        if was_set:
            setattr(CredentialProxyHandler, attribute, original)
        elif attribute in CredentialProxyHandler.__dict__:
            delattr(CredentialProxyHandler, attribute)

    def _request(self, method, path, token="chat-token", body=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_a_pull_on_a_configured_subscription_reaches_pubsub_on_the_brokers_credential(self):
        status, body = self._request("POST", PULL, body=b'{"maxMessages":10}')
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["receivedMessages"][0]["ackId"], "ack-1")
        [(method, upstream_path, headers, sent)] = self.google_requests
        self.assertEqual((method, upstream_path), ("POST", f"/v1/{ALERTS}:pull"))
        self.assertEqual(headers["Authorization"], "Bearer broker-token")
        self.assertEqual(sent, b'{"maxMessages":10}')
        self.assertEqual(self.upstream_hosts, ["pubsub.googleapis.com"])

    def test_acknowledge_modify_ack_deadline_and_get_are_forwarded_on_every_listed_subscription(self):
        for sub in (ALERTS, SECOND):
            base = f"{PREFIX}pubsub.googleapis.com/v1/{sub}"
            cases = (
                ("POST", base + ":acknowledge", b'{"ackIds":["ack-1"]}'),
                ("POST", base + ":modifyAckDeadline", b'{"ackIds":["ack-1"],"ackDeadlineSeconds":0}'),
                ("GET", base, None),
            )
            for method, path, body in cases:
                with self.subTest(sub=sub, path=path):
                    status, answer = self._request(method, path, body=body)
                    self.assertEqual(status, 200, answer)

    def test_a_pull_answer_over_the_relays_default_cap_is_still_relayed_to_the_adapter(self):
        """A pull is a lease: Pub/Sub has delivered the batch and started its ack
        deadline by the time the broker reads the answer, and the ack ids are
        in the body. A refused answer would leave the batch undeliverable until
        the deadline, forever for a message that is always over the cap. The
        platform family carries a cap no legal Pub/Sub message can exceed; the
        read-only watcher family keeps the relay's own."""
        self.pull_padding = 4000
        with mock.patch.object(credential_proxy, "API_RELAY_MAX_RESPONSE_BYTES", 1024), \
                mock.patch.object(credential_proxy, "PLATFORM_PUBSUB_MAX_RESPONSE_BYTES", 16384):
            status, body = self._request("POST", PULL, body=b'{"maxMessages":1}')
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["receivedMessages"][0]["ackId"], "ack-1")
            watcher_pull = f"{watcher_policy.WATCHER_GCP_PREFIX}pubsub.googleapis.com/v1/{DRIFT}:pull"
            status, body = self._request("POST", watcher_pull, token="watcher-token", body=b'{"maxMessages":1}')
            self.assertEqual(status, 502, body)
            self.assertEqual(json.loads(body)["code"], "UPSTREAM_UNRELAYABLE")

    def test_the_platform_cap_covers_one_largest_pubsub_message(self):
        """10 MiB of payload is 13.4 MiB once base64-encoded, plus the envelope."""
        self.assertGreaterEqual(
            credential_proxy.PLATFORM_PUBSUB_MAX_RESPONSE_BYTES,
            credential_proxy.PUBSUB_MAX_MESSAGE_BYTES * 4 // 3 + 64 * 1024,
        )

    def test_the_watchers_subscription_is_outside_the_platform_set(self):
        status, body = self._request("POST", f"{PREFIX}pubsub.googleapis.com/v1/{DRIFT}:pull", body=b"{}")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["rule"], watcher_policy.RULE_PUBSUB_SUBSCRIPTION)
        self.assertEqual(self.google_requests, [])

    def test_only_the_chat_role_reaches_the_platform_relay(self):
        for token in ("watcher-token", "shell-token"):
            with self.subTest(token=token):
                status, body = self._request("POST", PULL, token=token, body=b"{}")
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)["code"], "CALLER_ROLE_FORBIDDEN")
        status, _ = self._request("POST", PULL, token="", body=b"{}")
        self.assertEqual(status, 401)
        self.assertEqual(self.google_requests, [])

    def test_the_chat_role_does_not_reach_the_watcher_relay_through_this_change(self):
        status, body = self._request("POST", f"{watcher_policy.WATCHER_GCP_PREFIX}pubsub.googleapis.com/v1/{DRIFT}:pull",
                                     body=b"{}")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["code"], "CALLER_ROLE_FORBIDDEN")

    def test_the_gke_api_is_refused_on_the_platform_relay(self):
        status, body = self._request("GET", f"{PREFIX}container.googleapis.com/v1/projects/p/locations/l/clusters/c")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["rule"], watcher_policy.RULE_PUBSUB_ROUTE)
        self.assertEqual(self.google_requests, [])

    def test_an_empty_set_refuses_every_call_and_names_the_rule(self):
        CredentialProxyHandler.platform_pubsub_subscriptions = frozenset()
        status, body = self._request("POST", PULL, body=b"{}")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["rule"], watcher_policy.RULE_PUBSUB_SUBSCRIPTION)

    def test_a_query_the_relay_does_not_forward_is_refused_in_this_familys_name(self):
        status, body = self._request("POST", PULL + "?fields=x", body=b'{"maxMessages":1}')
        self.assertEqual(status, 400)
        self.assertIn("the platform Pub/Sub relay forwards no query keys", json.loads(body)["error"])
        self.assertEqual(self.google_requests, [])

    def test_the_audit_lines_name_the_family_and_the_rule(self):
        with self.assertLogs("credential-proxy", "INFO") as logs:
            status, _ = self._request("POST", PULL, body=b'{"maxMessages":1}')
        self.assertEqual(status, 200)
        text = "\n".join(logs.output)
        self.assertIn("platform pubsub request_id=", text)
        self.assertIn("platform pubsub forwarded request_id=", text)
        self.assertIn("rule=gcp.pubsub.subscription-pull", text)
        self.assertNotIn("broker-token", text)


class PlatformPubsubSubscriptionsFromEnvTest(unittest.TestCase):
    """What serve() reads PLATFORM_PUBSUB_SUBSCRIPTIONS as."""

    def test_the_variable_is_a_comma_list_of_qualified_subscriptions(self):
        with mock.patch.dict(os.environ, {"PLATFORM_PUBSUB_SUBSCRIPTIONS": f" {ALERTS}, {SECOND} ,,"}):
            self.assertEqual(credential_proxy.platform_pubsub_subscriptions(), frozenset({ALERTS, SECOND}))

    def test_unset_or_empty_is_the_empty_set(self):
        for value in ({}, {"PLATFORM_PUBSUB_SUBSCRIPTIONS": ""}, {"PLATFORM_PUBSUB_SUBSCRIPTIONS": " , "}):
            with self.subTest(value=value), mock.patch.dict(os.environ, value, clear=False):
                os.environ.pop("PLATFORM_PUBSUB_SUBSCRIPTIONS", None)
                os.environ.update(value)
                self.assertEqual(credential_proxy.platform_pubsub_subscriptions(), frozenset())

    def test_an_entry_that_is_not_a_qualified_subscription_is_dropped_not_fatal(self):
        """A typo in one AgentPlugin route must not take the whole broker down.

        The operator renders the list from user-authored configs. A malformed entry
        is dropped with a warning that counts it and does not repeat it; the well
        formed entries stay, and the broker boots.
        """
        for bad in ("gke-stockout-alerts-sub", "projects/p/topics/t", "projects/p/subscriptions/", "projects/p/subscriptions/a b",
                    "projects/My-Project/subscriptions/alerts"):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, {"PLATFORM_PUBSUB_SUBSCRIPTIONS": f"{ALERTS},{bad}"}):
                with self.assertLogs("credential-proxy", "WARNING") as logs:
                    got = credential_proxy.platform_pubsub_subscriptions()
                self.assertEqual(got, frozenset({ALERTS}))
                text = "\n".join(logs.output)
                self.assertIn("PLATFORM_PUBSUB_SUBSCRIPTIONS holds 1 entry", text)
                self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
