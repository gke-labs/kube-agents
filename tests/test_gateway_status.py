"""The scaled-zero-gateway fixture's status is the one Envoy Gateway writes for a proxy at zero.

`bench/tf/fleet/gateway_status.py` runs from a terraform_data local-exec,
because no controller owns the fixture's GatewayClass. These tests pin what it
writes: the Gateway Accepted and Programmed=False with reason NoResources (the
condition the stall check reads), its listener with one attached route, and
the route's parent status, each stamped with the object's generation; and that
a refused request fails the apply rather than leaving a Gateway with the CRD's
default Pending status.
"""

import base64
import importlib.util
import io
import json
import os
import pathlib
import unittest
import urllib.error
from unittest.mock import patch

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "bench" / "tf" / "fleet" / "gateway_status.py"

_spec = importlib.util.spec_from_file_location("gateway_status", _MODULE_PATH)
gateway_status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gateway_status)

ENV = {
    "K8S_HOST": "https://10.0.0.1",
    "K8S_CA_BASE64": base64.b64encode(b"not a real certificate").decode(),
    "FIXTURE_NAMESPACE": "seeded-scaled-zero",
    "FIXTURE_GATEWAY": "storefront-edge",
    "FIXTURE_ROUTE": "storefront",
    "FIXTURE_LISTENER": "http",
    "FIXTURE_GATEWAY_CLASS": "seeded-envoy",
    "FIXTURE_CONTROLLER": "gateway.envoyproxy.io/gatewayclass-controller",
}
GATEWAY_PATH = "/apis/gateway.networking.k8s.io/v1/namespaces/seeded-scaled-zero/gateways/storefront-edge"
ROUTE_PATH = "/apis/gateway.networking.k8s.io/v1/namespaces/seeded-scaled-zero/httproutes/storefront"
CLASS_PATH = "/apis/gateway.networking.k8s.io/v1/gatewayclasses/seeded-envoy"


class FakeApi:
    """Answers GETs with an object at a generation, records PATCHes."""

    def __init__(self, refuse=None):
        self.refuse = refuse
        self.patches = {}

    def __call__(self, request, context=None, timeout=None):
        path = request.full_url.removeprefix(ENV["K8S_HOST"])
        assert request.get_header("Authorization") == "Bearer t0ken"
        if path == self.refuse:
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"reason":"Forbidden"}'))
        if request.get_method() == "PATCH":
            assert request.get_header("Content-type") == "application/merge-patch+json"
            self.patches[path] = json.loads(request.data)
            body = {}
        else:
            body = {"metadata": {"generation": 3 if path == GATEWAY_PATH else 2}}
        return _Reply(body)


class _Reply(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run(api):
    with patch.dict(os.environ, ENV), patch.object(gateway_status, "access_token", return_value="t0ken"), patch.object(gateway_status.urllib.request, "urlopen", api), patch.object(
        gateway_status.ssl, "create_default_context", return_value=None
    ), patch("sys.stdout", io.StringIO()):
        return gateway_status.main()


class GatewayStatusTest(unittest.TestCase):
    def test_the_gateway_reads_programmed_false_no_resources(self):
        api = FakeApi()
        self.assertEqual(_run(api), 0)
        status = api.patches[GATEWAY_PATH + "/status"]["status"]
        by_type = {c["type"]: c for c in status["conditions"]}
        self.assertEqual((by_type["Programmed"]["status"], by_type["Programmed"]["reason"]), ("False", "NoResources"))
        self.assertEqual(by_type["Accepted"]["status"], "True")
        self.assertEqual({c["observedGeneration"] for c in status["conditions"]}, {3})
        listener = status["listeners"][0]
        self.assertEqual((listener["name"], listener["attachedRoutes"]), ("http", 1))
        self.assertTrue(all(c["status"] == "True" for c in listener["conditions"]))

    def test_the_route_names_its_gateway_and_has_no_backend_available(self):
        api = FakeApi()
        _run(api)
        parent = api.patches[ROUTE_PATH + "/status"]["status"]["parents"][0]
        self.assertEqual(parent["parentRef"], {"group": "gateway.networking.k8s.io", "kind": "Gateway", "name": "storefront-edge"})
        self.assertEqual(parent["controllerName"], "gateway.envoyproxy.io/gatewayclass-controller")
        by_type = {c["type"]: c for c in parent["conditions"]}
        self.assertEqual((by_type["BackendsAvailable"]["status"], by_type["BackendsAvailable"]["reason"]), ("False", "EndpointsNotFound"))
        self.assertEqual({c["observedGeneration"] for c in parent["conditions"]}, {2})

    def test_the_class_reads_accepted(self):
        api = FakeApi()
        _run(api)
        conditions = api.patches[CLASS_PATH + "/status"]["status"]["conditions"]
        self.assertEqual([(c["type"], c["status"], c["observedGeneration"]) for c in conditions], [("Accepted", "True", 2)])

    def test_the_token_is_taken_fresh_from_gcloud(self):
        done = gateway_status.subprocess.CompletedProcess(["gcloud"], 0, stdout="ya29.fresh\n", stderr="")
        with patch.object(gateway_status.subprocess, "run", return_value=done) as run:
            self.assertEqual(gateway_status.access_token(), "ya29.fresh")
        self.assertEqual(run.call_args.args[0], ["gcloud", "auth", "print-access-token"])
        failed = gateway_status.subprocess.CompletedProcess(["gcloud"], 1, stdout="", stderr="not logged in")
        with patch.object(gateway_status.subprocess, "run", return_value=failed), self.assertRaises(SystemExit):
            gateway_status.access_token()

    def test_a_refused_write_fails_the_apply(self):
        with self.assertRaises(SystemExit) as raised:
            _run(FakeApi(refuse=GATEWAY_PATH + "/status"))
        self.assertIn("403", str(raised.exception.code))

    def test_a_missing_variable_fails_before_any_request(self):
        api = FakeApi()
        with patch.dict(os.environ, {**ENV, "K8S_HOST": ""}), patch.object(gateway_status.urllib.request, "urlopen", api), patch("sys.stderr", io.StringIO()):
            self.assertEqual(gateway_status.main(), 1)
        self.assertEqual(api.patches, {})


class StatusWriterTriggerTest(unittest.TestCase):
    def test_a_changed_script_re_runs_the_writer(self):
        # A provisioner's command is not part of the resource's state, so
        # without the script's hash in triggers_replace an edited
        # gateway_status.py would plan nothing on a planted project.
        stack = (_REPO_ROOT / "bench" / "tf" / "fleet" / "defects-a.tf").read_text()
        block = stack[stack.index('resource "terraform_data" "scaled_zero_status"'):]
        block = block[: block.index("\n}\n")]
        self.assertIn('triggers_replace = [filesha256("${path.module}/gateway_status.py")]', block)
        self.assertIn('command     = "${path.module}/gateway_status.py"', block)


if __name__ == "__main__":
    unittest.main()
