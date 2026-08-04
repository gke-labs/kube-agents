#!/usr/bin/env python3
"""Tests for the credential proxy client shim.

The shim is what every `kubectl`/`gcloud`/`gh`/`git` in the agent container
actually is, so what it puts in the request body decides whether a command
reaches the right cluster - or is rejected outright.

Run:  python3 agents/platform/scripts/test_credential_proxy_client.py
"""

import io
import json
import sys
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
    def submit(self, argv, environ):
        """Run the client against a stubbed proxy, returning the request body."""
        captured = {}

        def fake_urlopen(request, *args, **kwargs):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return RecordingResponse(json.dumps({"exitCode": 0}).encode("utf-8"))

        with patch.dict("os.environ", environ, clear=False):
            with patch.object(credential_proxy_client.urllib.request, "urlopen", fake_urlopen):
                with patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=io.StringIO()):
                    credential_proxy_client.execute("http://proxy", argv)
        return captured["payload"]


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
