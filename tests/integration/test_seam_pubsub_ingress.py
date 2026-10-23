"""Seam: Pub/Sub platform ingress — the plugin adapter ↔ credential proxy, real HTTP.

The alert front door: the Pub/Sub platform adapter (agentplugins/pubsub-platform)
runs in the gateway pod, which holds no Google credential, and pulls its
subscriptions through the credential proxy's `/v1/pubsub/gcp/` relay. Here the
real adapter module drives the real `CredentialProxyHandler` over a socket, with
the broker's real role gate in front of the route and the adapter presenting the
projected token the operator mounts; the fakes sit at the seam's two ends — a
Pub/Sub upstream the broker's Cloud API relay is pointed at, the token review
(the cluster's TokenReview answer), and the hermes gateway package the adapter
imports (the component this repo does not own).

This is the test that reads red on the adapter as it stood when the gateway lost
Workload Identity: that adapter never spoke to the broker, so nothing here would
have pulled, and the one subscription the broker admits is the one the operator
rendered, not whatever the adapter asks for.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from _seams import REPO_ROOT, SCRIPTS_DIR

sys.path.insert(0, str(SCRIPTS_DIR))

ADAPTER = REPO_ROOT / "agentplugins" / "pubsub-platform" / "files" / "platforms" / "pubsub" / "adapter.py"
PROJECT = "seam-project"
ALERTS = f"projects/{PROJECT}/subscriptions/gke-stockout-alerts-sub"
ROUTE = "gke_stockout_alerts"
GATEWAY = "system:serviceaccount:kubeagents-system:platform-agent-gateway-agent"
# What the cluster's TokenReview says about each token the test can mount.
TOKEN_ROLES = {"gateway-chat-token": "chat", "watcher-token": "watcher"}


def load_adapter_module():
    """The adapter with hermes' gateway package stubbed, as its unit tests load it."""
    stubs = {}
    for name in ("gateway", "gateway.platforms", "gateway.platforms.base", "gateway.config", "gateway.session"):
        stubs[name] = types.ModuleType(name)
    base = stubs["gateway.platforms.base"]

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config

    for attribute in ("SendResult", "MessageEvent", "MessageType", "ProcessingOutcome"):
        setattr(base, attribute, type(attribute, (), {}))
    base.BasePlatformAdapter = BasePlatformAdapter
    stubs["gateway.config"].Platform = lambda *a, **k: None
    stubs["gateway.config"].PlatformConfig = type("PlatformConfig", (), {})
    stubs["gateway.session"].build_session_key = lambda *a, **k: "session-key"
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    spec = importlib.util.spec_from_file_location("seam_pubsub_adapter", ADAPTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, saved


class FakePubsub(BaseHTTPRequestHandler):
    """Google's side: one queued message, then idle; records acks."""

    protocol_version = "HTTP/1.1"
    queue: list = []
    calls: list = []
    lock = threading.Lock()

    def _answer(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        with self.lock:
            type(self).calls.append(("GET", self.path, None))
        self._answer({"name": ALERTS})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        with self.lock:
            type(self).calls.append(("POST", self.path, body))
            queued = type(self).queue[:]
            type(self).queue.clear()
        if self.path.endswith(":pull"):
            self._answer({"receivedMessages": queued} if queued else {})
        else:
            self._answer({})

    def log_message(self, *_):
        return


class PubsubIngressSeamTest(unittest.TestCase):
    def setUp(self):
        import credential_proxy
        from credential_proxy import CredentialProxyHandler

        FakePubsub.queue = []
        FakePubsub.calls = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakePubsub)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        upstream_port = self.upstream.server_address[1]

        class FakeApiRelay(credential_proxy.GoogleApiRelay):
            def authorization_header(self):
                return "Bearer the-brokers-google-token"

            def connection(self, host):
                return http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=5)

        for attribute in ("api_relay", "authenticator", "policy", "max_request_bytes", "platform_pubsub_subscriptions"):
            self.addCleanup(self._restore, CredentialProxyHandler, attribute,
                            attribute in CredentialProxyHandler.__dict__, CredentialProxyHandler.__dict__.get(attribute))
        CredentialProxyHandler.api_relay = FakeApiRelay()
        # The real authenticator and role gate; only the TokenReview is faked.
        authenticator = credential_proxy.ServiceAccountAuthenticator(
            audience_roles={
                "kubeagents-credential-proxy-chat": credential_proxy.CALLER_ROLE_CHAT,
                "kubeagents-credential-proxy-watcher": credential_proxy.CALLER_ROLE_WATCHER,
            },
            allowed_callers=frozenset({GATEWAY}),
            api_host="10.0.0.1", api_port="443", ca_file="", token_file="/nonexistent", cache_seconds=0.0,
        )

        def fake_review(token):
            if token not in TOKEN_ROLES:
                raise credential_proxy.AuthenticationError("not a token this cluster issued")
            return credential_proxy.Principal(workload=GATEWAY, uid="sa-uid", role=TOKEN_ROLES[token])

        authenticator._review = fake_review
        CredentialProxyHandler.authenticator = authenticator
        CredentialProxyHandler.policy = credential_proxy.Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.max_request_bytes = 65536
        # What the operator renders from the AgentPlugin config: this one subscription.
        CredentialProxyHandler.platform_pubsub_subscriptions = frozenset({ALERTS})
        self.broker = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.broker.serve_forever, daemon=True).start()
        self.addCleanup(self.broker.server_close)
        self.addCleanup(self.broker.shutdown)

        self.module, saved = load_adapter_module()
        self.addCleanup(self._restore_modules, saved)
        self.token_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.token_dir.cleanup)
        self.token_file = os.path.join(self.token_dir.name, "token")
        self.mount_token("gateway-chat-token")
        env = mock.patch.dict(os.environ, {
            "PUBSUB_RELAY_URL": f"http://127.0.0.1:{self.broker.server_port}",
            "PUBSUB_RELAY_PROJECT": PROJECT,
            "CREDENTIAL_PROXY_TOKEN_FILE": self.token_file,
        })
        env.start()
        self.addCleanup(env.stop)

    def mount_token(self, token):
        """The projected token as the kubelet writes it."""
        with open(self.token_file, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")

    @staticmethod
    def _restore(cls, attribute, was_set, original):
        if was_set:
            setattr(cls, attribute, original)
        elif attribute in cls.__dict__:
            delattr(cls, attribute)

    @staticmethod
    def _restore_modules(saved):
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _adapter(self, route_config):
        adapter = self.module.PubSubAdapter.__new__(self.module.PubSubAdapter)
        adapter._subscriptions_config = {ROUTE: route_config}
        adapter._relay = None
        adapter._pull_threads = []
        adapter._stop_pulling = threading.Event()
        adapter._running = True
        adapter.processed = []
        adapter.fatal = []

        async def process(route_name, cfg, message):
            adapter.processed.append((route_name, message))
            message.ack()

        adapter._process_message = process
        adapter._set_fatal_error = lambda code, detail, retryable: adapter.fatal.append(code)
        adapter._mark_connected = lambda: None
        adapter._mark_disconnected = lambda: None
        return adapter

    def _run(self, adapter, until, run_for=6.0):
        async def drive():
            ok = await adapter.connect()
            loop = asyncio.get_event_loop()
            deadline = loop.time() + run_for
            while loop.time() < deadline and not until():
                await asyncio.sleep(0.05)
            await adapter.disconnect()
            return ok

        return asyncio.run(drive())

    def test_an_alert_is_pulled_through_the_broker_processed_and_acked_through_the_broker(self):
        payload = {"jsonPayload": {"messageId": "scale.up.error.out.of.resources"}}
        FakePubsub.queue = [{"ackId": "ack-7", "message": {
            "messageId": "m-7", "attributes": {}, "data": base64.b64encode(json.dumps(payload).encode()).decode()}}]
        adapter = self._adapter({"subscription": "gke-stockout-alerts-sub", "topic": "gke-stockout-alerts-topic"})
        acked = lambda: any(path.endswith(":acknowledge") for _, path, _ in FakePubsub.calls)  # noqa: E731
        ok = self._run(adapter, until=acked)
        self.assertTrue(ok)
        self.assertEqual([(ROUTE, "m-7")], [(route, m.message_id) for route, m in adapter.processed])
        self.assertEqual(json.loads(adapter.processed[0][1].data), payload)
        upstream = [(method, path) for method, path, _ in FakePubsub.calls]
        self.assertIn(("GET", f"/v1/{ALERTS}"), upstream, "the preflight read went through the broker")
        self.assertIn(("POST", f"/v1/{ALERTS}:pull"), upstream)
        ack = next(body for _, path, body in FakePubsub.calls if path.endswith(":acknowledge"))
        self.assertEqual(ack, {"ackIds": ["ack-7"]})
        self.assertEqual(adapter.fatal, [])

    def test_a_token_of_another_role_is_refused_on_the_route_and_nothing_is_pulled(self):
        """The route admits the chat role only; the adapter presents whatever
        token is mounted, and the broker's role gate is what decides."""
        self.mount_token("watcher-token")
        adapter = self._adapter({"subscription": "gke-stockout-alerts-sub"})
        with self.assertLogs(self.module.__name__, "WARNING") as logs:
            self._run(adapter, until=lambda: any("CALLER_ROLE_FORBIDDEN" in line for line in logs.output), run_for=4.0)
        self.assertEqual(FakePubsub.calls, [], "the broker forwarded a call for a token outside the chat role")
        self.assertIn("CALLER_ROLE_FORBIDDEN", "\n".join(logs.output))
        self.assertEqual(adapter.processed, [])

    def test_a_subscription_the_operator_did_not_render_is_refused_by_the_broker_not_pulled(self):
        adapter = self._adapter({"subscription": "projects/seam-project/subscriptions/someone-elses-sub"})
        with self.assertLogs(self.module.__name__, "WARNING") as logs:
            self._run(adapter, until=lambda: any("gcp.pubsub.subscription" in line for line in logs.output), run_for=4.0)
        self.assertEqual(FakePubsub.calls, [], "the broker forwarded a call for an unlisted subscription")
        text = "\n".join(logs.output)
        self.assertIn("rule=gcp.pubsub.subscription", text)
        self.assertEqual(adapter.processed, [])


if __name__ == "__main__":
    unittest.main()
