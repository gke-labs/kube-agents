"""A stalled RootSync is created and given its status by one script.

`bench/tf/fleet/config_sync.py` is written for a terraform_data local-exec,
because a kubernetes_manifest cannot plan before the CRD exists and no
operator writes the status. These tests pin what it sends (one server-side
apply of the object with its labels and spec, then one merge patch of its
status subresource), that a 404 while the CRD is not served is tried again
until a deadline, that any other refusal fails the apply, and that the
compliance audit's own check names the object the script writes.
"""

import base64
import importlib.util
import io
import json
import os
import pathlib
import sys
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from unittest.mock import patch

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FLEET = _REPO_ROOT / "bench" / "tf" / "fleet"
_MODULE_PATH = _FLEET / "config_sync.py"
_COLLECTOR_DIRS = (
    _REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts",
    _REPO_ROOT / "agents" / "platform" / "scripts",
)

_spec = importlib.util.spec_from_file_location("config_sync", _MODULE_PATH)
config_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config_sync)

NAMESPACE = "config-management-system"
NAME = "root-sync"
SPEC = {
    "sourceFormat": "unstructured",
    "sourceType": "git",
    "git": {
        "repo": "https://config-sync-fixture.invalid/fleet-config.git",
        "branch": "main",
        "dir": "fixture-no-such-directory",
        "auth": "none",
    },
}
CONDITIONS = [
    {"type": "Reconciling", "status": "True", "reason": "Setup", "message": "Setup stalled"},
    {"type": "Stalled", "status": "True", "reason": "Deployment", "message": "Progress deadline exceeded"},
]
LABELS = {"managed-by": "kube-agents-seeded-fleet"}
ENV = {
    "K8S_HOST": "https://10.0.0.1",
    "K8S_CA_BASE64": base64.b64encode(b"not a real certificate").decode(),
    "ROOT_SYNC_NAMESPACE": NAMESPACE,
    "ROOT_SYNC_NAME": NAME,
    "ROOT_SYNC_LABELS": json.dumps(LABELS),
    "ROOT_SYNC_SPEC": json.dumps(SPEC),
    "ROOT_SYNC_CONDITIONS": json.dumps(CONDITIONS),
}
OBJECT_PATH = f"/apis/configsync.gke.io/v1beta1/namespaces/{NAMESPACE}/rootsyncs/{NAME}"
STATUS_PATH = OBJECT_PATH + "/status"
GENERATION = 3


class FakeApi:
    """Answers each PATCH with the next status in `codes` (200 when they run out)."""

    def __init__(self, *codes):
        self.codes = list(codes)
        self.requests = []

    def __call__(self, request, context=None, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        assert request.get_header("Authorization") == "Bearer t0ken"
        self.requests.append(
            (request.get_method(), url.path, urllib.parse.parse_qs(url.query), request.get_header("Content-type"), json.loads(request.data))
        )
        code = self.codes.pop(0) if self.codes else 200
        if code == "URLError":
            raise urllib.error.URLError("connection refused")
        if code == "ConnectionResetError":
            raise ConnectionResetError(104, "Connection reset by peer")
        if code != 200:
            raise urllib.error.HTTPError(request.full_url, code, "refused", {}, io.BytesIO(b'{"reason":"refused"}'))
        return _Reply({"metadata": {"generation": GENERATION}})


class _Reply(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run(api, clock=None, env=None):
    clock = clock or iter(range(0, 10**6, config_sync.RETRY_SECONDS))
    with patch.dict(os.environ, ENV if env is None else env, clear=env is not None), patch.object(
        config_sync, "access_token", return_value="t0ken"
    ), patch.object(config_sync.urllib.request, "urlopen", api), patch.object(
        config_sync.ssl, "create_default_context", return_value=None
    ), patch.object(config_sync.time, "sleep") as slept, patch.object(
        config_sync.time, "monotonic", side_effect=lambda: next(clock)
    ), patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()) as err:
        return config_sync.main(), slept, err.getvalue()


class ConfigSyncApplyTest(unittest.TestCase):
    def test_one_apply_of_the_object_then_one_patch_of_its_status(self):
        api = FakeApi()
        rc, slept, _ = _run(api)
        self.assertEqual(rc, 0)
        slept.assert_not_called()
        self.assertEqual([(m, p) for m, p, *_ in api.requests], [("PATCH", OBJECT_PATH), ("PATCH", STATUS_PATH)])
        _, _, query, content_type, body = api.requests[0]
        self.assertEqual(query, {"fieldManager": ["seeded-fleet"], "force": ["true"]})
        self.assertEqual(content_type, "application/apply-patch+yaml")
        self.assertEqual(
            body,
            {
                "apiVersion": "configsync.gke.io/v1beta1",
                "kind": "RootSync",
                "metadata": {"namespace": NAMESPACE, "name": NAME, "labels": LABELS},
                "spec": SPEC,
            },
        )

    def test_the_status_holds_the_conditions_at_the_objects_generation(self):
        api = FakeApi()
        _run(api)
        _, _, query, content_type, body = api.requests[1]
        self.assertEqual(query, {})
        self.assertEqual(content_type, "application/merge-patch+json")
        status = body["status"]
        self.assertEqual(status["observedGeneration"], GENERATION)
        self.assertEqual(
            [{k: c[k] for k in ("type", "status", "reason", "message")} for c in status["conditions"]], CONDITIONS
        )
        for condition in status["conditions"]:
            for field in config_sync.CONDITION_TIME_FIELDS:
                self.assertRegex(condition[field], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_the_status_patch_stamps_one_time(self):
        when = datetime(2026, 11, 18, 12, 0, 5, tzinfo=timezone.utc)
        patch_body = config_sync.status_patch(CONDITIONS, 1, when)
        stamps = {c[f] for c in patch_body["status"]["conditions"] for f in config_sync.CONDITION_TIME_FIELDS}
        self.assertEqual(stamps, {"2026-11-18T12:00:05Z"})

    def test_a_crd_not_served_yet_is_tried_again_until_it_is(self):
        api = FakeApi(404, 404)
        rc, slept, err = _run(api)
        self.assertEqual(rc, 0)
        self.assertEqual(slept.call_count, 2)
        self.assertEqual(len(api.requests), 4)
        self.assertIn("does not serve the RootSync CRD yet", err)

    def test_a_crd_still_missing_at_the_deadline_fails_the_apply(self):
        api = FakeApi(*[404] * 100)
        rc, _, err = _run(api)
        self.assertEqual(rc, 1)
        self.assertIn(f"after {config_sync.WAIT_SECONDS}s", err)
        self.assertEqual({p for _, p, *_ in api.requests}, {OBJECT_PATH})

    def test_a_server_error_a_rate_limit_or_no_answer_is_tried_again(self):
        for code in (500, 503, 429, "URLError", "ConnectionResetError"):
            with self.subTest(code=code):
                api = FakeApi(code)
                rc, slept, _ = _run(api)
                self.assertEqual(rc, 0)
                self.assertEqual(slept.call_count, 1)

    def test_a_failed_status_write_is_tried_again_and_fails_at_the_deadline(self):
        api = FakeApi(200, 503)
        rc, slept, _ = _run(api)
        self.assertEqual(rc, 0)
        self.assertEqual([p for _, p, *_ in api.requests], [OBJECT_PATH, STATUS_PATH, STATUS_PATH])
        api = FakeApi(200, *[503] * 100)
        rc, _, err = _run(api)
        self.assertEqual(rc, 1)
        self.assertIn(STATUS_PATH, err)

    def test_any_other_refusal_fails_the_apply_at_once(self):
        for code in (400, 401, 403, 409, 422):
            with self.subTest(code=code):
                api = FakeApi(code)
                with self.assertRaises(SystemExit) as exit_:
                    _run(api)
                self.assertIn(f"answered {code}", str(exit_.exception))
                self.assertEqual(len(api.requests), 1)

    def test_a_missing_variable_fails_before_any_request(self):
        api = FakeApi()
        rc, _, err = _run(api, env={k: v for k, v in ENV.items() if k != "ROOT_SYNC_SPEC"})
        self.assertEqual(rc, 1)
        self.assertIn("missing ROOT_SYNC_SPEC", err)
        self.assertEqual(api.requests, [])

    def test_a_value_that_is_not_json_fails_before_any_request(self):
        for name in config_sync.JSON_ENVS:
            with self.subTest(name=name):
                api = FakeApi()
                rc, _, err = _run(api, env={**ENV, name: "{not json"})
                self.assertEqual(rc, 1)
                self.assertIn(f"{name} is not JSON", err)
                self.assertEqual(api.requests, [])


class ConfigSyncProductCheckTest(unittest.TestCase):
    def test_the_products_check_names_the_object_the_script_writes(self):
        """The collector's own predicate, on the object as the script writes it."""
        for directory in _COLLECTOR_DIRS:
            if str(directory) not in sys.path:
                sys.path.insert(0, str(directory))
        import collect  # noqa: PLC0415 - the collector lives outside this test's package

        planted = config_sync.manifest(NAMESPACE, NAME, LABELS, SPEC)
        planted.update(config_sync.status_patch(CONDITIONS, 1, datetime(2026, 11, 18, tzinfo=timezone.utc)))
        hits = collect.check_configsync_reconciler_stalled({"configsync_objects": [planted]})
        self.assertEqual([(h["namespace"], h["object"]) for h in hits], [(NAMESPACE, "RootSync/root-sync")])
        self.assertIn("Deployment: Progress deadline exceeded", hits[0]["excerpt"])


if __name__ == "__main__":
    unittest.main()
