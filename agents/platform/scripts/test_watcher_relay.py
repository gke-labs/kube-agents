"""The broker's watcher routes, driven over a real socket.

A fake Google upstream answers the GKE describe and the Pub/Sub calls, and a
fake API server answers the Kubernetes reads, including a watch that streams
two events with a pause between them. `GoogleApiRelay.fetch` runs for real;
only the connections are pointed at the fakes and the token is fixed.
"""

import http.client
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import credential_proxy
import watcher_policy
from credential_proxy import CredentialProxyHandler, Policy

CALLER = "system:serviceaccount:kubeagents-system:kubeagents-platform-agent"
SUB = "projects/kagents-dev/subscriptions/platform-agent-drift-audit-sub"
CLUSTER = {
    "name": "fleet-a",
    "endpoint": "fleet-a.example.internal",
    "controlPlaneEndpointsConfig": {
        "dnsEndpointConfig": {"endpoint": "fleet-a.example.internal", "allowExternalTraffic": True}
    },
}
EVENT_GAP_S = 0.3


class WatcherRelayOverTheSocketTest(unittest.TestCase):
    def setUp(self):
        owner = self
        self.google_requests = []
        self.k8s_requests = []

        class Google(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _answer(self, body):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                owner.google_requests.append(("GET", self.path, dict(self.headers), b""))
                if "/clusters/" in self.path:
                    self._answer(json.dumps(CLUSTER).encode())
                else:
                    self._answer(b'{"name":"' + SUB.encode() + b'"}')

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                owner.google_requests.append(("POST", self.path, dict(self.headers), self.rfile.read(length)))
                self._answer(b'{"receivedMessages":[]}')

            def log_message(self, *_):
                return

        class ApiServer(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802
                owner.k8s_requests.append((self.path, dict(self.headers)))
                if "watch=true" in self.path:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for name in ("e1", "e2"):
                        line = json.dumps({"type": "ADDED", "object": {"metadata": {"name": name}}}).encode() + b"\n"
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                        self.wfile.flush()
                        time.sleep(EVENT_GAP_S)
                    self.wfile.write(b"0\r\n\r\n")
                    return
                body = b'{"kind":"EventList","items":[]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                return

        self.google = ThreadingHTTPServer(("127.0.0.1", 0), Google)
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), ApiServer)
        for server in (self.google, self.api):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        google_port, api_port = self.google.server_address[1], self.api.server_address[1]

        class FakeRelay(credential_proxy.GoogleApiRelay):
            def authorization_header(self):
                return "Bearer broker-token"

            def connection(self, host):
                return http.client.HTTPConnection("127.0.0.1", google_port, timeout=5)

        def fake_k8s_connection(handler, host, ca_pem):
            owner.k8s_hosts.append((host, ca_pem))
            return http.client.HTTPConnection("127.0.0.1", api_port, timeout=5)

        self.k8s_hosts = []
        for attribute in ("api_relay", "authenticator", "policy", "max_request_bytes",
                          "watcher_subscription", "watcher_endpoints", "_watcher_k8s_connection"):
            self.addCleanup(
                self._restore, attribute, attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        CredentialProxyHandler.api_relay = FakeRelay()
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.max_request_bytes = 1 << 20
        CredentialProxyHandler.watcher_subscription = SUB
        CredentialProxyHandler.watcher_endpoints = {}
        CredentialProxyHandler._watcher_k8s_connection = fake_k8s_connection

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

    def _request(self, method, path, token="watcher-token", body=None):
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

    # -- Google -----------------------------------------------------------

    def test_a_pull_on_the_subscription_reaches_pubsub_on_the_brokers_credential(self):
        path = f"/v1/watcher/gcp/pubsub.googleapis.com/v1/{SUB}:pull?alt=json&prettyPrint=false"
        status, body = self._request("POST", path, body=b'{"maxMessages":10}')
        self.assertEqual(status, 200, body)
        [(method, upstream_path, headers, sent)] = self.google_requests
        self.assertEqual((method, upstream_path), ("POST", f"/v1/{SUB}:pull?alt=json&prettyPrint=false"))
        self.assertEqual(headers["Authorization"], "Bearer broker-token")
        self.assertEqual(sent, b'{"maxMessages":10}')

    def test_another_subscription_never_reaches_pubsub(self):
        other = "projects/kagents-dev/subscriptions/other"
        status, body = self._request("POST", f"/v1/watcher/gcp/pubsub.googleapis.com/v1/{other}:pull", body=b"{}")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["rule"], watcher_policy.RULE_GCP_SUBSCRIPTION)
        self.assertEqual(self.google_requests, [])

    def test_a_content_length_that_is_not_a_number_is_400_not_413(self):
        path = f"/v1/watcher/gcp/pubsub.googleapis.com/v1/{SUB}:pull"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            connection.putrequest("POST", path)
            connection.putheader("Authorization", "Bearer watcher-token")
            connection.putheader("Content-Length", "abc")
            connection.endheaders()
            response = connection.getresponse()
            status, body = response.status, response.read()
        finally:
            connection.close()
        self.assertEqual(status, 400, body)
        self.assertEqual(json.loads(body)["error"], "invalid content length")
        self.assertEqual(self.google_requests, [])

    def test_a_query_outside_the_cloud_relays_shape_or_cap_is_refused(self):
        base = "/v1/watcher/gcp/container.googleapis.com/v1/projects/kagents-dev/locations/us-east4/clusters/fleet-a"
        for query in ("alt=json{", "alt=" + "j" * 9000):
            with self.subTest(query=query[:20]):
                status, _ = self._request("GET", f"{base}?{query}")
                self.assertEqual(status, 400)
        self.assertEqual(self.google_requests, [])

    def test_a_query_key_beyond_alt_and_prettyprint_is_refused(self):
        path = "/v1/watcher/gcp/container.googleapis.com/v1/projects/kagents-dev/locations/us-east4/clusters/fleet-a?key=x"
        status, _ = self._request("GET", path)
        self.assertEqual(status, 400)
        self.assertEqual(self.google_requests, [])

    # -- Kubernetes ----------------------------------------------------------

    def test_an_events_list_resolves_the_cluster_and_reads_it(self):
        status, body = self._request("GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/events?limit=500")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["kind"], "EventList")
        self.assertEqual(self.k8s_hosts, [("fleet-a.example.internal", "")])
        [(path, headers)] = self.k8s_requests
        self.assertEqual(path, "/api/v1/events?limit=500")
        self.assertEqual(headers["Authorization"], "Bearer broker-token")
        # The describe is remembered: a second read costs no GKE call.
        self._request("GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/namespaces/default/configmaps/app")
        self.assertEqual(len(self.k8s_requests), 2)
        self.assertEqual(sum("/clusters/" in p for _, p, _, _ in self.google_requests), 1)

    def test_a_watch_streams_each_event_as_it_arrives(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        connection.request("GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/events?watch=true",
                           headers={"Authorization": "Bearer watcher-token"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        first = response.readline()
        arrived = time.monotonic()
        second = response.readline()
        self.assertEqual(json.loads(first)["object"]["metadata"]["name"], "e1")
        self.assertEqual(json.loads(second)["object"]["metadata"]["name"], "e2")
        # The first event reached the client before the second was written.
        self.assertGreaterEqual(time.monotonic() - arrived, EVENT_GAP_S * 0.5)
        connection.close()

    def test_secrets_writes_and_other_lists_never_reach_the_api_server(self):
        for method, path in (
            ("GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/namespaces/default/secrets/db"),
            ("GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/pods"),
            ("POST", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/namespaces/default/pods"),
        ):
            with self.subTest(method=method, path=path):
                status, _ = self._request(method, path, body=b"{}" if method == "POST" else None)
                self.assertEqual(status, 403)
        self.assertEqual(self.k8s_requests, [])

    def test_a_refusal_is_a_kubernetes_status_naming_the_rule(self):
        # client-go turns this into a StatusError with the message verbatim, so
        # the drift join's lookup error says which rule refused the read.
        status, body = self._request(
            "GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/namespaces/default/pods/web-0"
        )
        self.assertEqual(status, 403)
        refusal = json.loads(body)
        self.assertEqual(
            (refusal["kind"], refusal["apiVersion"], refusal["status"], refusal["reason"], refusal["code"]),
            ("Status", "v1", "Failure", "Forbidden", 403),
        )
        self.assertIn(f"SECURITY_POLICY_BLOCKED ({watcher_policy.RULE_K8S_KIND})", refusal["message"])
        self.assertEqual(refusal["details"]["causes"][0]["message"], watcher_policy.RULE_K8S_KIND)
        self.assertEqual(self.k8s_requests, [])

    def test_a_query_outside_the_relay_shape_is_400(self):
        # `{` is a byte http.client sends but the relay's query shape refuses.
        status, _ = self._request("GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/events?labelSelector={")
        self.assertEqual(status, 400)
        self.assertEqual(self.k8s_requests, [])

    def test_a_failed_connect_forgets_the_cached_address(self):
        self._request("GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/events")
        self.assertIn(("kagents-dev", "us-east4", "fleet-a"), CredentialProxyHandler.watcher_endpoints)
        original = CredentialProxyHandler._watcher_k8s_connection

        def refused(handler, host, ca_pem):
            return http.client.HTTPConnection("127.0.0.1", 1, timeout=2)

        CredentialProxyHandler._watcher_k8s_connection = refused
        try:
            status, _ = self._request("GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/events")
        finally:
            CredentialProxyHandler._watcher_k8s_connection = original
        self.assertEqual(status, 502)
        self.assertNotIn(("kagents-dev", "us-east4", "fleet-a"), CredentialProxyHandler.watcher_endpoints)

    # -- who may call it ---------------------------------------------------

    def test_only_the_watcher_role_reaches_either_route(self):
        for token in ("chat-token", "shell-token"):
            for path in ("/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/events",
                         f"/v1/watcher/gcp/pubsub.googleapis.com/v1/{SUB}"):
                with self.subTest(token=token, path=path):
                    status, body = self._request("GET", path, token=token)
                    self.assertEqual(status, 403)
                    self.assertEqual(json.loads(body)["code"], "CALLER_ROLE_FORBIDDEN")
        self.assertEqual(self.google_requests, [])
        self.assertEqual(self.k8s_requests, [])


class ClusterAddressTest(unittest.TestCase):
    def test_the_same_choice_as_client_config_for_identity(self):
        import base64
        ca = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n").decode()
        address = credential_proxy.watcher_cluster_address
        self.assertEqual(address(CLUSTER), ("fleet-a.example.internal", ""))
        mirrored = {"endpoint": "d.example", "controlPlaneEndpointsConfig": {"dnsEndpointConfig": {"endpoint": "d.example"}}}
        self.assertEqual(address(mirrored), ("d.example", ""))
        ip = {"endpoint": "10.1.2.3", "masterAuth": {"clusterCaCertificate": ca},
              "controlPlaneEndpointsConfig": {"dnsEndpointConfig": {"endpoint": "d.example"}}}
        self.assertEqual(address(ip)[0], "10.1.2.3")
        self.assertIn("BEGIN CERTIFICATE", address(ip)[1])
        self.assertIsNone(address({"endpoint": "10.1.2.3"}))
        self.assertIsNone(address({}))




class WatcherAudienceTest(unittest.TestCase):
    """The watcher role is conferred by its own audience, and only after the chat split."""

    BASE = {
        "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
        "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent",
        "KUBERNETES_SERVICE_HOST": "10.0.0.1",
    }

    def build(self, **extra):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {**self.BASE, **extra}, clear=True):
            return credential_proxy.build_authenticator()

    def test_the_watcher_audience_confers_the_watcher_role(self):
        authenticator = self.build(
            CREDENTIAL_PROXY_CHAT_AUDIENCE="aud-chat",
            CREDENTIAL_PROXY_SESSION_AUDIENCE="aud-session",
            CREDENTIAL_PROXY_WATCHER_AUDIENCE="aud-watcher",
        )
        self.assertEqual(authenticator.audience_roles["aud-watcher"], credential_proxy.CALLER_ROLE_WATCHER)
        self.assertEqual(authenticator.audience_roles["aud-session"], credential_proxy.CALLER_ROLE_SESSION)

    def test_a_watcher_audience_equal_to_another_is_not_conferred(self):
        authenticator = self.build(
            CREDENTIAL_PROXY_CHAT_AUDIENCE="aud-chat", CREDENTIAL_PROXY_WATCHER_AUDIENCE="aud-chat"
        )
        self.assertEqual(authenticator.audience_roles["aud-chat"], credential_proxy.CALLER_ROLE_CHAT)


if __name__ == "__main__":
    unittest.main()
