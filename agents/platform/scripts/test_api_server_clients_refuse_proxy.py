#!/usr/bin/env python3
"""The three clients of the Kubernetes API server refuse a proxy from the environment.

The API server is inside the cluster. Each client builds an opener for each
call, with `ProxyHandler({})` and with the TLS context of that call. These
tests make a throwaway CA with the `openssl` binary (`make_pki`, from the forge
CA tests), serve TLS on loopback as the API server, and point the proxy
variables at a listener that counts its connections. Each call must get its
answer from the API server, which shows that the call's CA file is in use, and
the listener must see no connection. They are skipped where `openssl` is absent.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socketserver
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_report_store  # noqa: E402
import credential_proxy  # noqa: E402
import scope_publisher  # noqa: E402
from test_forge_ca_trust import make_pki  # noqa: E402

OPENSSL = shutil.which("openssl")
AUDIENCE = "kubeagents-credential-proxy"
CALLER = "system:serviceaccount:kubeagents-system:agent"
OWN_TOKEN = "own-token"
TOKEN_REVIEW_PATH = "/apis/authentication.k8s.io/v1/tokenreviews"
ISSUER = "https://issuer.example"


class _ApiServer:
    """A TLS listener on loopback that records each request and answers by path."""

    def __init__(self, test: unittest.TestCase, cert: Path, key: Path, answers: dict[str, dict]) -> None:
        self.seen: list[tuple[str, str, str | None]] = []
        seen = self.seen

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append((self.command, self.path, self.headers.get("Authorization")))
                body = json.dumps(answers[self.path]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer  # noqa: N815 - the stdlib's names

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cert), str(key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        test.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        test.addCleanup(server.shutdown)
        self.port = server.server_address[1]


class _CountingProxy:
    """A listener on loopback that counts the connections it gets and closes each one."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.connections = 0
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                proxy.connections += 1

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        test.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        test.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"


@unittest.skipUnless(OPENSSL, "openssl is needed to make a throwaway CA")
class ApiServerClientsRefuseProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.ca, cls.cert, cls.key = make_pki(Path(cls.tmp.name) / "pki", key_usage=True)

    def setUp(self):
        # The directory of a pod's ServiceAccount: its token and the cluster's CA.
        self.sa_dir = Path(self.tmp.name) / self.id().rsplit(".", 1)[-1]
        self.sa_dir.mkdir()
        (self.sa_dir / "token").write_text(OWN_TOKEN, encoding="utf-8")
        shutil.copy(self.ca, self.sa_dir / "ca.crt")
        self.proxy = _CountingProxy(self)
        environment = mock.patch.dict(
            os.environ,
            {name: self.proxy.url for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")},
        )
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("no_proxy", "NO_PROXY"):
            os.environ.pop(name, None)
        # A plain `urlopen` with no context keeps the opener of its first call. The
        # test drops the kept one, so that such a call reads this environment.
        kept = mock.patch.object(urllib.request, "_opener", None)
        kept.start()
        self.addCleanup(kept.stop)

    def serve(self, answers: dict[str, dict]) -> _ApiServer:
        return _ApiServer(self, self.cert, self.key, answers)

    def test_the_token_review_does_not_go_through_the_proxy(self):
        status = {"authenticated": True, "audiences": [AUDIENCE], "user": {"username": CALLER, "uid": "sa-uid"}}
        api = self.serve({TOKEN_REVIEW_PATH: {"status": status}})
        authenticator = credential_proxy.ServiceAccountAuthenticator(
            audience_roles={AUDIENCE: ""},
            allowed_callers=frozenset({CALLER}),
            api_host="127.0.0.1",
            api_port=str(api.port),
            ca_file=str(self.sa_dir / "ca.crt"),
            token_file=str(self.sa_dir / "token"),
            cache_seconds=0.0,
        )
        authenticator.authenticate({"Authorization": "Bearer agent-token"})
        self.assertEqual([("POST", TOKEN_REVIEW_PATH, f"Bearer {OWN_TOKEN}")], api.seen)
        self.assertEqual(0, self.proxy.connections)

    def test_the_read_of_the_signing_keys_does_not_go_through_the_proxy(self):
        api = self.serve({
            audit_report_store.DISCOVERY_PATH: {"issuer": ISSUER},
            audit_report_store.JWKS_PATH: {"keys": []},
        })
        answer = audit_report_store.fetch_cluster_keys(f"https://127.0.0.1:{api.port}", str(self.sa_dir))
        self.assertEqual((ISSUER, []), answer)
        self.assertEqual(
            [
                ("GET", audit_report_store.DISCOVERY_PATH, f"Bearer {OWN_TOKEN}"),
                ("GET", audit_report_store.JWKS_PATH, f"Bearer {OWN_TOKEN}"),
            ],
            api.seen,
        )
        self.assertEqual(0, self.proxy.connections)

    def test_the_configmap_call_does_not_go_through_the_proxy(self):
        path = scope_publisher.CONFIGMAP_PATH.format(namespace="kube-agents", name="pa-resolved-scope")
        api = self.serve({path: {"metadata": {"resourceVersion": "7"}}})
        with mock.patch.object(scope_publisher, "KUBE_TOKEN_FILE", self.sa_dir / "token"), \
                mock.patch.object(scope_publisher, "KUBE_CA_FILE", self.sa_dir / "ca.crt"), \
                mock.patch.dict(os.environ, {
                    scope_publisher.KUBE_HOST_ENV: "127.0.0.1",
                    scope_publisher.KUBE_PORT_ENV: str(api.port),
                }):
            answer = scope_publisher.read_previous("kube-agents", "pa-resolved-scope")
        self.assertEqual((None, "7"), answer)
        self.assertEqual([("GET", path, f"Bearer {OWN_TOKEN}")], api.seen)
        self.assertEqual(0, self.proxy.connections)


if __name__ == "__main__":
    unittest.main()
