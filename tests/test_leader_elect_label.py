"""The gateway leader label as leader_elect.py keeps it: set once this pod holds the
lease and its Hermes answers, cleared otherwise, reconciled every loop so a refused
patch is retried, cleared before the exit that follows a Hermes crash, and cleared by
a wrapper restarted on a pod still labelled because that clear was refused.

The script imports the kubernetes client at module level and reads its environment
there, so it is loaded from its path with that client stubbed; nothing here talks to
an API server.

Run: python3 -m unittest discover -s tests -p 'test_leader_elect_label.py' -v
"""

import importlib.util
import pathlib
import sys
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "leader_elect.py"
_POD = "agent-gateway-aaa"


class _ApiException(Exception):
    def __init__(self, status=500):
        super().__init__(f"status {status}")
        self.status = status


def _load():
    kubernetes = types.ModuleType("kubernetes")
    client = types.ModuleType("kubernetes.client")
    rest = types.ModuleType("kubernetes.client.rest")
    rest.ApiException = _ApiException
    client.rest = rest
    config = types.ModuleType("kubernetes.config")
    kubernetes.client, kubernetes.config = client, config
    with mock.patch.dict(sys.modules, {"kubernetes": kubernetes, "kubernetes.client": client, "kubernetes.client.rest": rest, "kubernetes.config": config}), \
            mock.patch.dict("os.environ", {"HOSTNAME": _POD, "LEADER_ELECTION_LEASE_NAME": "", "LEADER_ELECTION_NAMESPACE": ""}):
        spec = importlib.util.spec_from_file_location("leader_elect_under_test", _SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class _FakePods:
    def __init__(self, refuse=0):
        self.refuse = refuse
        self.patches = []

    def patch_namespaced_pod(self, name, namespace, body):
        if self.refuse:
            self.refuse -= 1
            raise _ApiException(503)
        self.patches.append(body["metadata"]["labels"]["kubeagents.io/is-leader"])


class LeaderLabelTest(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_the_label_means_the_lease_and_a_hermes_that_answers(self):
        self.assertFalse(self.m.desired_label("somebody-else", True, True))
        self.assertFalse(self.m.desired_label(_POD, False, False), "the lease alone is not the label")
        self.assertFalse(self.m.desired_label(_POD, True, False), "a Hermes that does not answer yet is not the label")
        self.assertTrue(self.m.desired_label(_POD, True, True))

    def test_a_refused_patch_is_retried_until_the_api_accepts_it(self):
        pods = _FakePods(refuse=2)
        state = None
        state = self.m.reconcile_label(pods, True, state)
        self.assertIsNone(state, "a refused set was recorded as done")
        state = self.m.reconcile_label(pods, True, state)
        self.assertIsNone(state)
        state = self.m.reconcile_label(pods, True, state)
        self.assertIs(state, True)
        self.assertEqual(["true"], pods.patches)
        self.assertIs(self.m.reconcile_label(pods, True, state), True)
        self.assertEqual(["true"], pods.patches, "a confirmed state was patched again")

    def test_a_restarted_wrapper_clears_a_label_it_did_not_set(self):
        # The pod led, its Hermes died, the container restarted: the label is
        # still on the pod, the new wrapper knows nothing, and the lease has
        # moved. The first reconcile clears it, retrying a refusal.
        pods = _FakePods(refuse=1)
        state = self.m.reconcile_label(pods, False, None)
        self.assertIsNone(state)
        state = self.m.reconcile_label(pods, False, state)
        self.assertIs(state, False)
        self.assertEqual([None], pods.patches, "the label was not cleared")

    def test_losing_the_lease_clears_the_label_and_a_refused_clear_is_retried(self):
        pods = _FakePods()
        state = self.m.reconcile_label(pods, True, None)
        pods.refuse = 1
        state = self.m.reconcile_label(pods, False, state)
        self.assertIs(state, True, "a refused clear was recorded as done")
        state = self.m.reconcile_label(pods, False, state)
        self.assertIs(state, False)
        self.assertEqual(["true", None], pods.patches)

    def test_a_crash_clears_the_label_before_the_wrapper_exits(self):
        # Hermes died under a leading wrapper: the label comes off before the
        # exit that restarts the pod, so the re-scaffold window is not spent
        # labelled; the exit code is Hermes' own.
        pods = _FakePods()
        with self.assertRaises(SystemExit) as raised:
            self.m.exit_on_crash(pods, True, 137)
        self.assertEqual(137, raised.exception.code)
        self.assertEqual([None], pods.patches, "the label was not cleared before the exit")
        pods = _FakePods(refuse=1)
        with self.assertRaises(SystemExit):
            self.m.exit_on_crash(pods, True, 1)
        self.assertEqual([], pods.patches, "a refused clear must still exit; the restarted wrapper clears it")

    def test_the_probe_ignores_a_proxy_set_on_the_container(self):
        # urlopen's default opener reads http_proxy from the environment, which
        # would carry a loopback probe off the pod; the probe's opener is built
        # with ProxyHandler({}), which build_opener drops as it adds no method,
        # so a proxy in the environment at import time leaves no handler behind.
        def proxy_handlers(opener):
            return [h.proxies for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]

        with mock.patch.dict("os.environ", {"http_proxy": "http://127.0.0.1:9"}):
            self.assertEqual([{"http": "http://127.0.0.1:9"}], proxy_handlers(urllib.request.build_opener()), "the control: the default opener does pick the proxy up")
            self.assertEqual([], proxy_handlers(_load().HERMES_PROBE_OPENER))

    def test_hermes_answers_on_any_http_answer_and_not_on_a_refusal(self):
        def unauthorized(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

        def refused(request, timeout):
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

        self.assertTrue(self.m.hermes_answers(opener=unauthorized), "a 401 is an answer from Hermes")
        self.assertTrue(self.m.hermes_answers(opener=lambda request, timeout: object()))
        self.assertFalse(self.m.hermes_answers(opener=refused))
        self.assertFalse(self.m.hermes_answers(opener=mock.Mock(side_effect=TimeoutError())))


if __name__ == "__main__":
    unittest.main()
