"""The credential proxy's remote MCP relay, driven over a real socket.

A fake upstream stands in for container.googleapis.com/mcp and records what
reached it, so what the relay claims is checked at the edge: which calls reach
Google at all, which headers and bytes leave the broker, which callers are
turned away, and what the model reads when a call is refused.
`GoogleApiRelay.fetch` runs for real; only the connection is pointed at the
fake and the token is fixed.
"""

import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import credential_proxy
import mcp_policy
from credential_proxy import CredentialProxyHandler, Policy

CALLER = "system:serviceaccount:kubeagents-system:kubeagents-platform-agent"
GKE = "/v1/mcp/gke"
USER_AGENT = "kube-agents/abc123 (daemon)"


def tools_call(name, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": {"parent": "projects/p/locations/-"}}}


class McpRelayOverTheSocketTest(unittest.TestCase):
    def setUp(self):
        owner = self
        self.upstream_requests = []
        self.reply = {"status": 200, "content_type": "application/json; charset=UTF-8",
                      "body": b'{"jsonrpc":"2.0","id":1,"result":{"content":[]}}'}

        class UpstreamHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                owner.upstream_requests.append(
                    (self.command, self.path, dict(self.headers), self.rfile.read(length))
                )
                reply = owner.reply
                self.send_response(reply["status"])
                if reply["content_type"]:
                    self.send_header("Content-Type", reply["content_type"])
                self.send_header("Content-Length", str(len(reply["body"])))
                self.end_headers()
                self.wfile.write(reply["body"])

            def log_message(self, _message, *_args):
                return

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)

        class FakeRelay(credential_proxy.GoogleApiRelay):
            def __init__(self, port):
                super().__init__()
                self.port = port
                self.hosts = []

            def authorization_header(self):
                return "Bearer broker-token"

            def project_id(self):
                return "kagents-dev"

            def connection(self, host):
                self.hosts.append(host)
                return http.client.HTTPConnection(
                    "127.0.0.1", self.port, timeout=credential_proxy.API_RELAY_CONNECT_TIMEOUT_S
                )

        self.relay = FakeRelay(self.upstream.server_address[1])

        for attribute in ("api_relay", "authenticator", "policy", "max_request_bytes"):
            self.addCleanup(
                self._restore,
                attribute,
                attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        CredentialProxyHandler.api_relay = self.relay
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.max_request_bytes = 1 << 20

        authenticator = credential_proxy.ServiceAccountAuthenticator(
            audience_roles={
                "kubeagents-credential-proxy": credential_proxy.CALLER_ROLE_SHELL,
                "kubeagents-credential-proxy-chat": credential_proxy.CALLER_ROLE_CHAT,
                "kubeagents-credential-proxy-session": credential_proxy.CALLER_ROLE_SESSION,
            },
            allowed_callers=frozenset({CALLER}),
            api_host="10.0.0.1",
            api_port="443",
            ca_file="",
            token_file="/nonexistent",
            cache_seconds=0.0,
        )
        roles = {
            "shell-token": credential_proxy.CALLER_ROLE_SHELL,
            "chat-token": credential_proxy.CALLER_ROLE_CHAT,
            "session-token": credential_proxy.CALLER_ROLE_SESSION,
        }

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

    def _request(self, path=GKE, message=None, token="chat-token", method="POST", headers=None, body=None):
        sent = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        sent.update(headers or {})
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        if body is None and message is not None:
            body = json.dumps(message).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    # -- what reaches Google ---------------------------------------------

    def test_a_read_tool_reaches_the_upstream_on_the_brokers_credential(self):
        status, headers, body = self._request(
            message=tools_call("get_cluster"),
            headers={"Mcp-Session-Id": "s1", "X-Goog-User-Project": "someone-else", "Cookie": "c"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["id"], 1)
        self.assertEqual(self.relay.hosts, ["container.googleapis.com"])
        [(method, path, sent, forwarded)] = self.upstream_requests
        self.assertEqual((method, path), ("POST", "/mcp"))
        self.assertEqual(sent["Authorization"], "Bearer broker-token")
        self.assertEqual(sent["X-Goog-User-Project"], "kagents-dev")
        self.assertEqual(sent["User-Agent"], USER_AGENT)
        # Nothing else the caller sent rides along.
        self.assertEqual(
            {name.lower() for name in sent},
            {"host", "authorization", "accept", "content-type", "content-length",
             "user-agent", "x-goog-user-project"},
        )
        # The upstream sees the broker's re-serialisation of what it evaluated.
        self.assertEqual(json.loads(forwarded), tools_call("get_cluster"))
        self.assertNotIn(b" ", forwarded)

    def test_a_write_tool_never_reaches_the_upstream(self):
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            status, _, body = self._request(message=tools_call("patch_k8s_resource", request_id=9))
        self.assertEqual(status, 200)
        reply = json.loads(body)
        self.assertEqual(reply["id"], 9)
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("policy rule: gcp.mcp.read-only", reply["result"]["content"][0]["text"])
        self.assertEqual(self.upstream_requests, [])
        self.assertTrue(any("mcp blocked request_id=" in line and "rule=gcp.mcp.read-only" in line
                            for line in logs.output))

    def test_a_smuggled_duplicate_name_never_reaches_the_upstream(self):
        body = (b'{"jsonrpc":"2.0","id":3,"method":"tools/call",'
                b'"params":{"name":"get_cluster","name":"delete_k8s_resource"}}')
        status, _, reply = self._request(body=body)
        self.assertEqual(status, 200)
        self.assertIn("gcp.mcp.unreadable-request", json.loads(reply)["error"]["message"])
        self.assertEqual(self.upstream_requests, [])

    def test_tools_list_is_cut_down_to_the_table(self):
        tools = [{"name": name} for name in ("get_cluster", "create_cluster", "patch_k8s_resource")]
        self.reply["body"] = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}).encode()
        status, _, body = self._request(message={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(status, 200)
        self.assertEqual([tool["name"] for tool in json.loads(body)["result"]["tools"]], ["get_cluster"])

    def test_a_notification_is_relayed_and_its_202_passed_back(self):
        self.reply.update(status=202, content_type="", body=b"")
        status, _, body = self._request(message={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, b""))
        self.assertEqual(len(self.upstream_requests), 1)

    # -- what the relay refuses to pass back -----------------------------

    def test_a_streamed_reply_is_refused_not_relayed(self):
        self.reply.update(content_type="text/event-stream", body=b"event: message\ndata: {}\n\n")
        status, _, body = self._request(message={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["code"], "UPSTREAM_NOT_JSON")

    def test_an_upstream_error_status_is_passed_through(self):
        self.reply.update(status=403, body=b'{"jsonrpc":"2.0","id":1,"result":{"isError":true,"content":[]}}')
        status, _, _ = self._request(message=tools_call("get_cluster"))
        self.assertEqual(status, 403)

    # -- who may call it -------------------------------------------------

    def test_only_the_gateway_role_reaches_the_relay(self):
        for token in ("shell-token", "session-token"):
            with self.subTest(token=token):
                status, _, body = self._request(message=tools_call("get_cluster"), token=token)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)["code"], "CALLER_ROLE_FORBIDDEN")
        status, _, _ = self._request(message=tools_call("get_cluster"), token=None)
        self.assertEqual(status, 401)
        self.assertEqual(self.upstream_requests, [])

    def test_an_unknown_server_is_refused(self):
        status, _, body = self._request(path="/v1/mcp/secretmanager", message=tools_call("get_cluster"))
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["rule"], mcp_policy.RULE_SERVER)
        self.assertEqual(self.upstream_requests, [])

    def test_get_is_405_and_an_oversized_body_is_413(self):
        status, headers, _ = self._request(method="GET")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")
        # Announced rather than sent: the broker answers before reading a body
        # over the cap and closes, which would reset a client still writing.
        status, _, _ = self._request(body=b"{}", headers={"Content-Length": str(2 << 20)})
        self.assertEqual(status, 413)
        self.assertEqual(self.upstream_requests, [])


if __name__ == "__main__":
    unittest.main()
