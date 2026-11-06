"""The broker's watcher routes, driven over a real socket.

A fake Google upstream answers the GKE describe and the Pub/Sub calls, and a
fake API server answers the Kubernetes reads, including a watch that streams
two events with a pause between them. `GoogleApiRelay.fetch` runs for real;
only the connections are pointed at the fakes and the token is fixed.
"""

import http.client
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

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
    # What clusters.get also returns, and the watcher route must not relay: a
    # client certificate and key where issuance is on, and fields no watcher reads.
    "masterAuth": {"clusterCaCertificate": "Q0EtUEVN", "clientCertificate": "Q0VSVA==", "clientKey": "S0VZ"},
    "nodePools": [{"name": "default-pool"}],
    "legacyAbac": {"enabled": True},
}
EVENT_GAP_S = 0.3
# What the fake API server answers a named Pod GET with: a Pod's whole
# ObjectMeta, which carries a client-side apply's manifest (env values
# included), other labels and annotations, and managedFields.
POD_METADATA_UPSTREAM = {
    "kind": "PartialObjectMetadata",
    "apiVersion": "meta.k8s.io/v1",
    "metadata": {
        "name": "web-0",
        "namespace": "default",
        "uid": "u-1",
        "labels": {"app": "web", "pod-template-hash": "6cfdb6b98b", "team": "payments"},
        "annotations": {
            "kubectl.kubernetes.io/last-applied-configuration": '{"spec":{"containers":[{"env":[{"name":"DB_PASSWORD","value":"hunter2"}]}]}}',
            "note": "x",
        },
        "managedFields": [{"manager": "kubectl", "fieldsV1": {"f:spec": {}}}],
        "ownerReferences": [
            {"apiVersion": "apps/v1", "kind": "StatefulSet", "name": "web", "uid": "u-2", "controller": True,
             "blockOwnerDeletion": True},
        ],
    },
}
POD_METADATA_PROJECTED = {
    "metadata": {"ownerReferences": [{"kind": "StatefulSet", "name": "web", "controller": True}]},
}
# For the peer that stops reading its stream: the window the broker gives
# it, how long a test waits for the broker to act, the handler thread's
# logging settle after the close, and the API server's chunk, written until
# the broker closes its side.
SHORT_IDLE_S = 0.5
CLOSE_WAIT_S = 5
SETTLE_S = 0.5
UNREAD_STREAM_CHUNK = b"a" * (64 * 1024)
# Captured before any test points the seam at a fake.
REAL_K8S_CONNECTION = CredentialProxyHandler._watcher_k8s_connection


class WatcherRelayOverTheSocketTest(unittest.TestCase):
    def setUp(self):
        owner = self
        self.google_requests = []
        self.k8s_requests = []
        self.pod_status, self.pod_body = 200, json.dumps(POD_METADATA_UPSTREAM).encode()
        self.stream_unread = False

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
                if owner.stream_unread:
                    # A stream wider than any socket buffer, for the peer
                    # that stops reading it: written until the broker
                    # closes its side of the upstream connection.
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        while True:
                            self.wfile.write(UNREAD_STREAM_CHUNK)
                            self.wfile.flush()
                    except OSError:
                        return
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
                status, body = 200, b'{"kind":"EventList","items":[]}'
                if "/pods/" in self.path:
                    status, body = owner.pod_status, owner.pod_body
                self.send_response(status)
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

    def _request(self, method, path, token="watcher-token", body=None, headers=None, response_headers=None):
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if response_headers is not None:
                response_headers.update(response.getheaders())
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

    def test_a_cluster_read_relays_only_the_fields_the_watchers_read(self):
        path = "/v1/watcher/gcp/container.googleapis.com/v1/projects/kagents-dev/locations/us-east4/clusters/fleet-a"
        status, body = self._request("GET", path + "?alt=json&prettyPrint=false")
        self.assertEqual(status, 200, body)
        cluster = json.loads(body)
        self.assertEqual(cluster["masterAuth"], {"clusterCaCertificate": "Q0EtUEVN"})
        self.assertEqual(set(cluster), {"name", "endpoint", "controlPlaneEndpointsConfig", "masterAuth"})
        self.assertNotIn(b"S0VZ", body)

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

    def test_a_watch_stream_says_it_will_close_and_ends_with_eof(self):
        # The one response of unknown length the credentialed handler writes:
        # no Content-Length, `Connection: close`, and the connection closed
        # after the body, so a keep-alive client knows where the stream ends.
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        connection.request("GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/events?watch=true",
                           headers={"Authorization": "Bearer watcher-token"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual("close", (response.getheader("Connection") or "").lower())
        self.assertIsNone(response.getheader("Content-Length"))
        self.assertIsNone(response.getheader("Transfer-Encoding"))
        # A close-delimited read returns only at EOF: the server closed, within
        # the client's timeout, or this raises.
        self.assertTrue(response.will_close)
        body = response.read()
        self.assertEqual(["e1", "e2"], [json.loads(line)["object"]["metadata"]["name"] for line in body.splitlines()])
        connection.close()

    def test_a_watcher_that_stops_reading_its_stream_is_logged_as_the_peers_stall_not_an_upstream_failure(self):
        # The request read deadline leaves KEEPALIVE_IDLE_SECONDS on the
        # socket for the whole request, so the relay's write to a watcher
        # that stops reading for the window ends with a TimeoutError. That
        # is the broker's own peer, not the API server: the line says so
        # and names the request, the upstream-failure line that names the
        # API server is not written, and the cached address stands.
        self.stream_unread = True
        path = "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/events?watch=true"
        with mock.patch.object(credential_proxy, "KEEPALIVE_IDLE_SECONDS", SHORT_IDLE_S):
            with self.assertLogs(credential_proxy.LOGGER, level="DEBUG") as logs:
                with socket.create_connection(("127.0.0.1", self.server.server_address[1]), timeout=CLOSE_WAIT_S) as unread:
                    unread.sendall(f"GET {path} HTTP/1.1\r\nHost: broker\r\nAuthorization: Bearer watcher-token\r\n\r\n".encode("ascii"))
                    deadline = time.monotonic() + CLOSE_WAIT_S
                    while not any("not read by the peer" in line for line in logs.output):
                        if time.monotonic() > deadline:
                            self.fail(f"the cut stream was not logged: {logs.output}")
                        time.sleep(0.1)
                time.sleep(SETTLE_S)  # the handler thread finishes logging after the close
        cut = [line for line in logs.output if "not read by the peer" in line]
        self.assertEqual(1, len(cut), logs.output)
        self.assertTrue(cut[0].startswith("WARNING:") and "type=TimeoutError" in cut[0], cut[0])
        self.assertIn("request_id=", cut[0])
        self.assertFalse(any("upstream failed" in line for line in logs.output), logs.output)
        self.assertFalse(any("Traceback" in line for line in logs.output), logs.output)
        self.assertTrue(any("watcher k8s forwarded" in line for line in logs.output), logs.output)
        self.assertIn(("kagents-dev", "us-east4", "fleet-a"), CredentialProxyHandler.watcher_endpoints)

    def test_a_get_carrying_a_body_on_the_cloud_passthrough_announces_its_close(self):
        # The Cloud API passthrough writes its response without _json, and a
        # GET reads no body, so a GET that announces one leaves it unread:
        # the response must say `Connection: close` and the connection close
        # after it, or the body is read as the next request line.
        path = "/v1/watcher/gcp/container.googleapis.com/v1/projects/kagents-dev/locations/us-east4/clusters/fleet-a?alt=json&prettyPrint=false"
        body = b'{"unread":true}'
        with socket.create_connection(("127.0.0.1", self.server.server_address[1]), timeout=10) as sock:
            sock.sendall(
                f"GET {path} HTTP/1.1\r\nHost: broker\r\nAuthorization: Bearer watcher-token\r\n"
                f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
            )
            response = http.client.HTTPResponse(sock)
            response.begin()
            relayed = response.read()
            self.assertEqual(200, response.status, relayed)
            self.assertEqual("close", (response.getheader("Connection") or "").lower(), response.getheaders())
            self.assertEqual(b"", sock.recv(1), "the connection stayed open")
        [(method, upstream_path, _, _)] = self.google_requests
        self.assertEqual((method, upstream_path), ("GET", "/v1/projects/kagents-dev/locations/us-east4/clusters/fleet-a?alt=json&prettyPrint=false"))

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

    def test_a_count_list_reaches_the_api_server_only_with_the_metadata_accept_it_forwards(self):
        metadata = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
        status, body = self._request(
            "GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/apis/apps/v1/deployments?limit=1", headers={"Accept": metadata}
        )
        self.assertEqual(status, 200, body)
        [(path, headers)] = self.k8s_requests
        self.assertEqual(path, "/apis/apps/v1/deployments?limit=1")
        self.assertEqual(headers["Accept"], metadata)
        # The policy reads the Accept the broker forwards, so an Accept the
        # forwarding shape drops (too long) is evaluated as absent: refused,
        # not relayed as a list whose one item comes back whole. The over-long
        # one is metadata-only on every range, so the policy on its own admits
        # it; only the coupling to the forwarded value refuses it, which is
        # what a policy reading the raw header would get wrong.
        overlong = ",".join([metadata] * 5)
        self.assertTrue(watcher_policy.accepts_metadata_only(overlong))
        self.assertFalse(credential_proxy.WATCHER_K8S_ACCEPT_SHAPE.match(overlong))
        for accept in (None, "application/json", metadata + ",application/json", overlong):
            with self.subTest(accept=accept):
                status, body = self._request(
                    "GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/apis/apps/v1/deployments?limit=1",
                    headers={"Accept": accept} if accept else None,
                )
                self.assertEqual(status, 403, body)
                self.assertIn(watcher_policy.RULE_K8S_ACCEPT, json.loads(body)["message"])
        status, _ = self._request(
            "GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/apis/apps/v1/deployments?limit=2", headers={"Accept": metadata}
        )
        self.assertEqual(status, 403)
        self.assertEqual(len(self.k8s_requests), 1)

    def test_a_named_pod_reaches_the_api_server_only_as_metadata(self):
        # The event watcher's owner lookup. The broker forwards the Accept the
        # policy read, so the API server answers with the Pod's ObjectMeta.
        metadata = "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"
        status, body = self._request(
            "GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/namespaces/default/pods/web-0",
            headers={"Accept": metadata},
        )
        self.assertEqual(status, 200, body)
        [(path, headers)] = self.k8s_requests
        self.assertEqual(path, "/api/v1/namespaces/default/pods/web-0")
        self.assertEqual(headers["Accept"], metadata)
        # Only the projection leaves the broker: no annotation (the
        # last-applied manifest and its env value among them), no other label,
        # no managedFields.
        self.assertEqual(json.loads(body), POD_METADATA_PROJECTED)
        self.assertNotIn(b"hunter2", body)
        self.assertNotIn(b"managedFields", body)
        self.k8s_requests.clear()
        for accept in (None, "application/json", metadata + ",application/json"):
            with self.subTest(accept=accept):
                status, body = self._request(
                    "GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/namespaces/default/pods/web-0",
                    headers={"Accept": accept} if accept else None,
                )
                self.assertEqual(status, 403, body)
                self.assertIn(watcher_policy.RULE_K8S_ACCEPT, json.loads(body)["message"])
        self.assertEqual(self.k8s_requests, [])

    def _pod_metadata(self, response_headers=None):
        return self._request(
            "GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/namespaces/default/pods/web-0",
            headers={"Accept": "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"},
            response_headers=response_headers,
        )

    def test_a_projected_pod_answer_carries_its_length(self):
        # Framed by length, so it ends where the body ends under HTTP/1.1
        # keep-alive as well as under a close.
        response_headers = {}
        status, body = self._pod_metadata(response_headers)
        self.assertEqual(status, 200, body)
        self.assertEqual(response_headers.get("Content-Length"), str(len(body)))

    def test_a_pod_read_that_carries_a_body_closes_with_its_answer(self):
        # The route reads no body on a GET, so the answer says it closes,
        # and the unread bytes are never parsed as a next request.
        response_headers = {}
        status, body = self._request(
            "GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/namespaces/default/pods/web-0",
            body=b"{}",
            headers={"Accept": "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"},
            response_headers=response_headers,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(response_headers.get("Connection"), "close")
        response_headers.clear()
        self._pod_metadata(response_headers)
        self.assertNotEqual(response_headers.get("Connection"), "close")

    def test_a_pod_answer_the_broker_cannot_project_is_refused_not_relayed(self):
        # A 200 that is not a PartialObjectMetadata (a full Pod, should the
        # server ignore the Accept) and an answer past the size cap are each a
        # 502 with none of the upstream body.
        full_pod = json.dumps({"kind": "Pod", "metadata": POD_METADATA_UPSTREAM["metadata"], "spec": {}}).encode()
        oversize = json.dumps(dict(POD_METADATA_UPSTREAM, pad="x" * credential_proxy.WATCHER_POD_METADATA_MAX_BYTES)).encode()
        # An error answer past the cap is refused too, not relayed cut short.
        for name, status_code, upstream in (
            ("full pod", 200, full_pod), ("not json", 200, b"not json"), ("oversize", 200, oversize),
            ("oversize error", 500, oversize),
        ):
            with self.subTest(name):
                self.pod_status, self.pod_body = status_code, upstream
                status, body = self._pod_metadata()
                self.assertEqual(status, 502, body)
                self.assertEqual(json.loads(body)["code"], "UPSTREAM_UNRELAYABLE")
                self.assertNotIn(b"hunter2", body)

    def test_a_pod_read_the_api_server_refuses_is_relayed_as_its_status(self):
        status_body = b'{"kind":"Status","apiVersion":"v1","status":"Failure","reason":"NotFound","code":404}'
        self.pod_status, self.pod_body = 404, status_body
        status, body = self._pod_metadata()
        self.assertEqual(status, 404)
        self.assertEqual(body, status_body)

    def test_a_refusal_is_a_kubernetes_status_naming_the_rule(self):
        # client-go turns this into a StatusError with the message verbatim, so
        # the drift join's lookup error says which rule refused the read.
        status, body = self._request(
            "GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/nodes/node-1"
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

    def test_a_cached_ca_that_will_not_load_is_answered_and_forgotten(self):
        # The real connection builder, so the TLS context is built from the
        # cached CA and fails to load it.
        key = ("kagents-dev", "us-east4", "fleet-a")
        CredentialProxyHandler.watcher_endpoints[key] = (time.monotonic() + 300, "10.0.0.9", "not a certificate")
        CredentialProxyHandler._watcher_k8s_connection = REAL_K8S_CONNECTION
        status, body = self._request("GET", "/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/events")
        self.assertEqual(status, 502, body)
        self.assertEqual(json.loads(body)["code"], "UPSTREAM_UNAVAILABLE")
        self.assertNotIn(key, CredentialProxyHandler.watcher_endpoints)

    def test_both_routes_log_the_caller_before_the_decision(self):
        # The broker calls on its own credential, so this line is the only
        # record of which ServiceAccount presented the watcher token.
        # The whole target too: the object read is what the line is for.
        for path, target in (
            ("/v1/k8s/kagents-dev/us-east4/fleet-a/api/v1/namespaces/default/configmaps/app",
             "kagents-dev/us-east4/fleet-a/api/v1/namespaces/default/configmaps/app"),
            (f"/v1/watcher/gcp/pubsub.googleapis.com/v1/{SUB}", f"pubsub.googleapis.com/v1/{SUB}"),
        ):
            with self.subTest(path=path):
                with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                    self._request("GET", path)
                self.assertTrue(
                    any("principal=" in line and CALLER in line and f"target={target}" in line for line in logs.output),
                    logs.output,
                )

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
