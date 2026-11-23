"""seeded-a's RepoSync for the post-merge fixture is applied once Config Sync has installed its CRD.

`bench/tf/fleet/repo_sync.py` runs from a terraform_data local-exec in
post-merge-sync.tf, as config_connector.py does, and reuses its token and
retry constants. These tests pin the one request it sends, the refusal of a
manifest that is not a namespaced RepoSync, and the 404 wait.
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
from unittest.mock import patch

_FLEET = pathlib.Path(__file__).resolve().parents[1] / "bench" / "tf" / "fleet"
if str(_FLEET) not in sys.path:
    sys.path.insert(0, str(_FLEET))
_spec = importlib.util.spec_from_file_location("repo_sync", _FLEET / "repo_sync.py")
repo_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_sync)

MANIFEST = {
    "apiVersion": "configsync.gke.io/v1beta1",
    "kind": "RepoSync",
    "metadata": {"name": "repo-sync", "namespace": "seeded-post-merge"},
    "spec": {"sourceType": "git", "git": {"repo": "git@github.com:o/r.git", "auth": "ssh"}},
}
PATH = "/apis/configsync.gke.io/v1beta1/namespaces/seeded-post-merge/reposyncs/repo-sync"


class _Api:
    """Answers each PATCH with the next status in `codes` (200 when they run out)."""

    def __init__(self, *codes):
        self.codes, self.requests = list(codes), []

    def __call__(self, request, context=None, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        self.requests.append((request.get_method(), url.path, urllib.parse.parse_qs(url.query), json.loads(request.data)))
        code = self.codes.pop(0) if self.codes else 200
        if code != 200:
            raise urllib.error.HTTPError(request.full_url, code, "refused", {}, io.BytesIO(b"{}"))
        return io.BytesIO(b"{}")


def _run(api, manifest=MANIFEST):
    env = {"K8S_HOST": "https://10.0.0.1", "K8S_CA_BASE64": base64.b64encode(b"ca").decode(), "REPO_SYNC_MANIFEST": json.dumps(manifest)}
    clock = iter(range(0, 10**6, repo_sync.CRD_RETRY_SECONDS))
    with patch.dict(os.environ, env), patch.object(repo_sync, "access_token", return_value="t0ken"), patch.object(
        repo_sync.urllib.request, "urlopen", api
    ), patch.object(repo_sync.ssl, "create_default_context", return_value=None), patch.object(
        repo_sync.time, "sleep"
    ), patch.object(repo_sync.time, "monotonic", side_effect=lambda: next(clock)), patch("sys.stdout", io.StringIO()), patch(
        "sys.stderr", io.StringIO()
    ) as err:
        return repo_sync.main(), err.getvalue()


class RepoSyncApplyTest(unittest.TestCase):
    def test_one_server_side_apply_of_the_namespaced_object(self):
        api = _Api()
        code, err = _run(api)
        self.assertEqual(0, code, err)
        self.assertEqual([("PATCH", PATH, {"fieldManager": ["seeded-fleet"], "force": ["true"]}, MANIFEST)], api.requests)

    def test_a_missing_crd_is_tried_again_until_it_is_there(self):
        api = _Api(404, 404)
        code, err = _run(api)
        self.assertEqual(0, code, err)
        self.assertEqual(3, len(api.requests))
        self.assertIn("not on the cluster yet", err)

    def test_a_crd_still_missing_at_the_deadline_fails_the_apply(self):
        code, err = _run(_Api(*([404] * 100)))
        self.assertEqual(1, code)
        self.assertIn("Config Sync did not install it", err)

    def test_any_other_refusal_fails_the_apply_at_once(self):
        api = _Api(403)
        with self.assertRaises(SystemExit):
            _run(api)
        self.assertEqual(1, len(api.requests))

    def test_a_manifest_that_is_not_a_namespaced_repo_sync_is_refused_before_any_call(self):
        for broken in ({**MANIFEST, "kind": "RootSync"}, {**MANIFEST, "metadata": {"name": "repo-sync"}}):
            with self.subTest(manifest=broken):
                api = _Api()
                code, err = _run(api, broken)
                self.assertEqual(1, code)
                self.assertEqual([], api.requests)
                self.assertIn("REPO_SYNC_MANIFEST", err)

    def test_the_stack_sends_the_object_the_role_catalog_probes(self):
        stack = (_FLEET / "post-merge-sync.tf").read_text()
        catalog = json.loads((_FLEET / "fixtures.json").read_text())["roles"]["post-merge-sync-workload"]
        self.assertIn("reposync/repo-sync", catalog["probes"])
        self.assertIn('kind       = "RepoSync"', stack)
        self.assertIn('name      = "repo-sync"', stack)
        self.assertIn(f'apiVersion = "{repo_sync.API_VERSION}"', stack)


if __name__ == "__main__":
    unittest.main()
