#!/usr/bin/env python3
"""Tests for the credential proxy client shim.

The shim is what every `kubectl`/`gcloud`/`gh`/`git` in the agent container
actually is, so what it puts in the request body decides whether a command
reaches the right cluster - or is rejected outright.

Run:  python3 agents/platform/scripts/test_credential_proxy_client.py
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import credential_proxy_client


class RecordingResponse(io.BytesIO):
    """Stand-in for the urlopen context manager the client reads."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SubmittedPayloadTestCase(unittest.TestCase):
    def send(self, argv, environ):
        """Run the client against a stubbed proxy, returning the whole request.

        The stub replaces `open_broker_request` rather than `urlopen`: the
        client sends through its own opener so that the connect is bounded
        while the response is not.
        """
        captured = {}

        def fake_open(request, *args, **kwargs):
            captured["request"] = request
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return RecordingResponse(json.dumps({"exitCode": 0}).encode("utf-8"))

        with patch.dict("os.environ", environ, clear=False):
            with patch.object(credential_proxy_client, "open_broker_request", fake_open):
                with patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=io.StringIO()):
                    captured["exit_code"] = credential_proxy_client.execute(
                        "http://proxy", argv
                    )
        return captured

    def submit(self, argv, environ):
        """Run the client against a stubbed proxy, returning the request body."""
        return self.send(argv, environ)["payload"]


class TestKubeconfigForwarding(SubmittedPayloadTestCase):
    PINNED = "/opt/data/profiles/cluster-a/kubeconfig.yaml"

    def test_kubectl_carries_the_pin(self):
        # The whole point of the forward: a Cluster Agent's pinned kubeconfig
        # has to reach the sidecar, which does not inherit the caller's env.
        payload = self.submit(["kubectl", "get", "pods"], {"KUBECONFIG": self.PINNED})
        self.assertEqual(payload["kubeconfig"], self.PINNED)

    def test_gcloud_carries_the_pin(self):
        # gcloud writes it: `container clusters get-credentials` renders the
        # kubeconfig at $KUBECONFIG, which is how switch_kube_context works.
        payload = self.submit(["gcloud", "container", "clusters", "get-credentials", "c"],
                              {"KUBECONFIG": self.PINNED})
        self.assertEqual(payload["kubeconfig"], self.PINNED)

    def test_git_and_gh_do_not(self):
        # Neither reads KUBECONFIG, and the server rejects an out-of-workspace
        # path rather than ignoring it - so forwarding it here would 400 a
        # command that has nothing to do with Kubernetes.
        for argv in (["git", "status"], ["gh", "pr", "list"]):
            with self.subTest(argv=argv):
                payload = self.submit(argv, {"KUBECONFIG": "/tmp/somewhere.yaml"})
                self.assertNotIn("kubeconfig", payload)

    def test_absent_when_unset(self):
        payload = self.submit(["kubectl", "get", "pods"], {"KUBECONFIG": ""})
        self.assertNotIn("kubeconfig", payload)

    def test_trailing_newline_is_stripped(self):
        # Profile .env files routinely carry one, and an unstripped value fails
        # the server's containment check on a path that is actually fine.
        payload = self.submit(["kubectl", "get", "pods"], {"KUBECONFIG": self.PINNED + "\n"})
        self.assertEqual(payload["kubeconfig"], self.PINNED)


class TestCallerCredential(SubmittedPayloadTestCase):
    """The client half of the broker's authentication.

    The server-side tests prove an unauthenticated call is refused. Nothing
    proved the client attaches a valid one: deleting the
    `headers.update(authorization_headers())` line left the entire Python suite
    green while the split deployment was completely broken — every command a
    401 — and the sidecar deployment, which sends no header at all, looked
    exactly the same.
    """

    def token_file(self, contents):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = Path(directory) / "token"
        path.write_text(contents, encoding="utf-8")
        return path

    def test_no_header_when_the_token_file_is_not_configured(self):
        # The sidecar deployment. The broker is on the Pod's own loopback
        # behind a socket only its container can open and asks for nothing, and
        # this is half of why the gate-off behaviour is unchanged.
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual({}, credential_proxy_client.authorization_headers())

        sent = self.send(["kubectl", "get", "pods"], {"CREDENTIAL_PROXY_TOKEN_FILE": ""})
        self.assertIsNone(sent["request"].get_header("Authorization"))

    def test_the_configured_token_is_sent_as_a_bearer_credential(self):
        path = self.token_file("a-projected-service-account-token")
        headers = self.send(
            ["kubectl", "get", "pods"], {"CREDENTIAL_PROXY_TOKEN_FILE": str(path)}
        )["request"]
        self.assertEqual(
            "Bearer a-projected-service-account-token",
            headers.get_header("Authorization"),
        )

    def test_the_projected_newline_is_stripped(self):
        # A projected token file has no trailing newline today, but a Secret or
        # a hand-written one does, and " \n" inside the header value is a
        # malformed credential rather than a rejected one.
        path = self.token_file("token-with-newline\n")
        headers = self.send(
            ["kubectl", "get", "pods"], {"CREDENTIAL_PROXY_TOKEN_FILE": str(path)}
        )["request"]
        self.assertEqual("Bearer token-with-newline", headers.get_header("Authorization"))

    def test_an_unreadable_token_file_fails_with_its_own_message(self):
        # Sending the request anyway would earn an undifferentiated 401 and
        # point the operator at the broker, when the fault is the projection.
        captured = {}

        def fake_open(request, *args, **kwargs):
            captured["sent"] = True
            return RecordingResponse(b"{}")

        stderr = io.StringIO()
        environ = {"CREDENTIAL_PROXY_TOKEN_FILE": "/nonexistent/token"}
        with patch.dict("os.environ", environ, clear=False):
            with patch.object(credential_proxy_client, "open_broker_request", fake_open):
                with patch("sys.stderr", new=stderr):
                    exit_code = credential_proxy_client.execute(
                        "http://proxy", ["kubectl", "get", "pods"]
                    )

        self.assertEqual(1, exit_code)
        self.assertNotIn("sent", captured, "a request with no credential must not be sent")
        self.assertIn("credential proxy token unavailable", stderr.getvalue())

    def test_an_empty_token_file_is_a_failure_not_an_empty_header(self):
        # The kubelet writes a projected token atomically, but a Secret mounted
        # before its data exists is empty, and "Bearer " is a 401 with no clue.
        path = self.token_file("")
        with patch.dict("os.environ", {"CREDENTIAL_PROXY_TOKEN_FILE": str(path)}, clear=False):
            with self.assertRaises(credential_proxy_client.TokenUnavailable):
                credential_proxy_client.authorization_headers()


class TestConnectTimeout(unittest.TestCase):
    """A bounded connect, and a response that is not bounded.

    Envoy routes /v1/exec with `timeout: 0s` on purpose: a proxied
    `get-credentials` or a large clone runs for minutes. A total timeout would
    cap the command; no timeout at all leaves the agent's kubectl blocked
    forever against a broker Pod that is Pending. So the connect is bounded and
    nothing else is.
    """

    def test_the_socket_timeout_is_cleared_once_connected(self):
        connection = credential_proxy_client.BrokerConnection("broker", 8765)
        observed = {}

        class FakeSocket:
            def settimeout(self, value):
                observed["after_connect"] = value

        def fake_connect(self):
            observed["during_connect"] = self.timeout
            self.sock = FakeSocket()

        with patch.object(
            credential_proxy_client.http.client.HTTPConnection, "connect", fake_connect
        ):
            connection.connect()

        self.assertEqual(
            credential_proxy_client.BROKER_CONNECT_TIMEOUT_SECONDS,
            observed["during_connect"],
            "reaching a Pending broker Pod must not block forever",
        )
        self.assertIsNone(
            observed["after_connect"],
            "a long-running proxied command must not be cut off by a client timeout",
        )

    def test_the_opener_does_not_follow_a_redirect(self):
        # urllib re-sends Authorization across a cross-host redirect, so a 302
        # from a compromised broker would hand the projected token to whatever
        # the Location names.
        handlers = [
            handler
            for handler in credential_proxy_client._BROKER_OPENER.handlers
            if isinstance(handler, credential_proxy_client._NoRedirect)
        ]
        self.assertEqual(1, len(handlers))
        self.assertIsNone(
            handlers[0].redirect_request(
                None, None, 302, "Found", {}, "http://elsewhere.invalid/"
            ),
            "a redirect out of the broker must not be followed",
        )

    def test_the_opener_uses_that_connection(self):
        # Building the opener with the wrong handler would silently restore the
        # stdlib connection and its unbounded connect.
        handlers = [
            handler
            for handler in credential_proxy_client._BROKER_OPENER.handlers
            if isinstance(handler, credential_proxy_client._BrokerHTTPHandler)
        ]
        self.assertEqual(1, len(handlers))


if __name__ == "__main__":
    unittest.main(verbosity=2)
