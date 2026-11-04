"""seeded-a's ConfigConnector object is applied once the add-on has installed its CRD.

`bench/tf/fleet/config_connector.py` runs from a terraform_data local-exec,
because a kubernetes_manifest cannot plan before the CRD exists. These tests
pin what it sends (one server-side apply of the cluster-scoped object in
namespaced mode, with the fleet's labels), that a 404 while the CRD is missing
is tried again until a deadline, and that any other refusal fails the apply.
"""

import base64
import importlib.util
import io
import json
import os
import pathlib
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FLEET = _REPO_ROOT / "bench" / "tf" / "fleet"
_MODULE_PATH = _FLEET / "config_connector.py"

_spec = importlib.util.spec_from_file_location("config_connector", _MODULE_PATH)
config_connector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config_connector)

NAME = "configconnector.core.cnrm.cloud.google.com"
ENV = {
    "K8S_HOST": "https://10.0.0.1",
    "K8S_CA_BASE64": base64.b64encode(b"not a real certificate").decode(),
    "CONFIG_CONNECTOR_NAME": NAME,
    "CONFIG_CONNECTOR_MODE": "namespaced",
    "CONFIG_CONNECTOR_LABELS": json.dumps({"managed-by": "kube-agents-seeded-fleet"}),
}
OBJECT_PATH = f"/apis/core.cnrm.cloud.google.com/v1beta1/configconnectors/{NAME}"


class FakeApi:
    """Answers each PATCH with the next status in `codes` (200 when they run out)."""

    def __init__(self, *codes):
        self.codes = list(codes)
        self.requests = []

    def __call__(self, request, context=None, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        assert request.get_header("Authorization") == "Bearer t0ken"
        self.requests.append((request.get_method(), url.path, urllib.parse.parse_qs(url.query), request.get_header("Content-type"), json.loads(request.data)))
        code = self.codes.pop(0) if self.codes else 200
        if code == "URLError":
            raise urllib.error.URLError("connection refused")
        if code == "ConnectionResetError":
            raise ConnectionResetError(104, "Connection reset by peer")
        if code != 200:
            raise urllib.error.HTTPError(request.full_url, code, "refused", {}, io.BytesIO(b'{"reason":"refused"}'))
        return _Reply({})


class _Reply(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run(api, clock=None):
    clock = clock or iter(range(0, 10**6, config_connector.CRD_RETRY_SECONDS))
    with patch.dict(os.environ, ENV), patch.object(config_connector, "access_token", return_value="t0ken"), patch.object(
        config_connector.urllib.request, "urlopen", api
    ), patch.object(config_connector.ssl, "create_default_context", return_value=None), patch.object(
        config_connector.time, "sleep"
    ) as slept, patch.object(config_connector.time, "monotonic", side_effect=lambda: next(clock)), patch(
        "sys.stdout", io.StringIO()
    ), patch("sys.stderr", io.StringIO()) as err:
        return config_connector.main(), slept, err.getvalue()


class ConfigConnectorApplyTest(unittest.TestCase):
    def test_one_server_side_apply_of_the_namespaced_object(self):
        api = FakeApi()
        code, _, _ = _run(api)
        self.assertEqual(code, 0)
        self.assertEqual(len(api.requests), 1)
        method, path, query, content_type, body = api.requests[0]
        self.assertEqual((method, path), ("PATCH", OBJECT_PATH))
        self.assertEqual(query, {"fieldManager": ["seeded-fleet"], "force": ["true"]})
        self.assertEqual(content_type, "application/apply-patch+yaml")
        self.assertEqual(body, {
            "apiVersion": "core.cnrm.cloud.google.com/v1beta1",
            "kind": "ConfigConnector",
            "metadata": {"name": NAME, "labels": {"managed-by": "kube-agents-seeded-fleet"}},
            "spec": {"mode": "namespaced"},
        })

    def test_a_missing_crd_is_tried_again_until_it_is_there(self):
        api = FakeApi(404, 404)
        code, slept, err = _run(api)
        self.assertEqual(code, 0)
        self.assertEqual(len(api.requests), 3)
        self.assertEqual(slept.call_count, 2)
        self.assertIn("not on the cluster yet", err)

    def test_a_crd_still_missing_at_the_deadline_fails_the_apply(self):
        api = FakeApi(*([404] * 100))
        code, _, err = _run(api)
        self.assertEqual(code, 1)
        self.assertIn("did not install it", err)
        self.assertLessEqual(len(api.requests), config_connector.CRD_WAIT_SECONDS // config_connector.CRD_RETRY_SECONDS + 2)

    def test_a_server_error_or_no_answer_is_tried_again_in_the_window(self):
        # A failed run taints the resource, and the next reconcile would plan
        # a replace, so a transient answer must not fail the apply.
        api = FakeApi(503, "URLError", "ConnectionResetError", 500)
        code, slept, err = _run(api)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(api.requests), 5)
        self.assertEqual(slept.call_count, 4)
        self.assertIn("trying again", err)

    def test_a_rate_limit_is_tried_again_in_the_window(self):
        api = FakeApi(429, 429)
        code, slept, err = _run(api)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(api.requests), 3)
        self.assertEqual(slept.call_count, 2)
        self.assertIn("429", err)

    def test_a_server_error_still_there_at_the_deadline_fails_the_apply(self):
        api = FakeApi(*([502] * 100))
        code, _, err = _run(api)
        self.assertEqual(code, 1)
        self.assertIn("502", err)

    def test_any_other_refusal_fails_the_apply_at_once(self):
        api = FakeApi(403)
        with self.assertRaises(SystemExit) as raised:
            _run(api)
        self.assertIn("403", str(raised.exception.code))
        self.assertEqual(len(api.requests), 1)

    def test_a_missing_variable_fails_before_any_request(self):
        api = FakeApi()
        with patch.dict(os.environ, {**ENV, "CONFIG_CONNECTOR_MODE": ""}), patch.object(config_connector.urllib.request, "urlopen", api), patch("sys.stderr", io.StringIO()):
            self.assertEqual(config_connector.main(), 1)
        self.assertEqual(api.requests, [])

    def test_labels_that_are_not_json_fail_before_any_request(self):
        api = FakeApi()
        with patch.dict(os.environ, {**ENV, "CONFIG_CONNECTOR_LABELS": "managed-by=x"}), patch.object(config_connector.urllib.request, "urlopen", api), patch("sys.stderr", io.StringIO()):
            self.assertEqual(config_connector.main(), 1)
        self.assertEqual(api.requests, [])


class ConfigConnectorStackTest(unittest.TestCase):
    def _stack(self):
        return (_FLEET / "config-connector.tf").read_text()

    def _block(self, header):
        stack = self._stack()
        block = stack[stack.index(header):]
        return block[: block.index("\n}\n")]

    def test_a_changed_script_re_runs_the_apply(self):
        # A provisioner's command is not part of the resource's state, so
        # without the script's hash in triggers_replace an edited
        # config_connector.py would plan nothing on a planted project.
        block = self._block('resource "terraform_data" "config_connector"')
        self.assertIn('filesha256("${path.module}/config_connector.py")', block)
        self.assertIn('command     = "${path.module}/config_connector.py"', block)
        self.assertIn("seeded_a.endpoint", block)

    def test_a_recreated_cluster_gets_the_object_again(self):
        # The cluster's CA certificate is new on every re-creation of
        # seeded-a; in triggers_replace, it re-runs the apply there.
        block = self._block('resource "terraform_data" "config_connector"')
        triggers = block[block.index("triggers_replace"):block.index("\n  ]")]
        self.assertIn("google_container_cluster.seeded_a.master_auth[0].cluster_ca_certificate", triggers)

    def test_the_writer_is_a_standing_reconcile_allow_entry(self):
        allow = json.loads((_FLEET / "reconcile-allow.json").read_text())
        entry = [e for e in allow if e["address"] == "terraform_data.config_connector"]
        self.assertEqual(len(entry), 1)
        self.assertTrue(entry[0]["standing"])
        self.assertIn("taint", entry[0]["why"])

    def test_the_mode_is_namespaced_and_the_account_holds_no_project_role(self):
        stack = self._stack()
        self.assertIn('config_connector_mode = "namespaced"', stack)
        self.assertNotIn("google_project_iam_member", stack)
        binding = self._block('resource "google_service_account_iam_member" "kcc_eval_context_workload_identity"')
        self.assertIn('role               = "roles/iam.workloadIdentityUser"', binding)
        self.assertIn(".svc.id.goog[${local.kcc_controller_ksa}]", binding)
        self.assertIn('kcc_controller_ksa = "cnrm-system/cnrm-controller-manager-${local.kcc_eval_namespace}"', stack)

    def test_the_add_on_is_on_seeded_a_only(self):
        main = (_FLEET / "main.tf").read_text()
        self.assertEqual(main.count("config_connector_config {"), 1)
        seeded_a = main[main.index('resource "google_container_cluster" "seeded_a"'):]
        seeded_a = seeded_a[: seeded_a.index("\n}\n")]
        self.assertIn("config_connector_config {\n      enabled = true", seeded_a)

    def test_the_script_is_a_stack_input(self):
        # The reconcile's fleet tree and the postsubmit's file list must
        # include it, or a change to it alone would reach no project.
        reconcile = (_REPO_ROOT / "hack" / "fleet_reconcile.py").read_text()
        self.assertIn('"config_connector.py"', reconcile)


if __name__ == "__main__":
    unittest.main()
