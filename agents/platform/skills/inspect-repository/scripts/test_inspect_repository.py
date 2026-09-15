"""Unit tests for inspect_repository — reading a repository through the broker.

Run:
  python3 -m unittest discover -s agents/platform/skills/inspect-repository/scripts \
      -p 'test_inspect_repository.py' -v

Stdlib only. The broker is stubbed at `credential_proxy_client._workspace_call`,
the one HTTP hop, with a fake that answers each verb the way the broker's
`open`, `list`, `read` and `close` routes do; nothing here opens a socket or
runs `git`.
"""

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import credential_proxy_client  # noqa: E402
import inspect_repository  # noqa: E402

REPO = "acme/terraform-live"
BASE_SHA = "9f8e7d6c5b4a39281706958473625140abcdef01"
HANDLE = "ws-1"


class FakeBroker:
    """The broker's workspace routes, as far as `open` and `clone` exercise them."""

    def __init__(self):
        self.verbs: list[str] = []
        self.files = {"README.md": b"# terraform-live\n", "clusters/prod.tf": b"replicas = 3\n"}

    def __call__(self, endpoint, verb, payload):
        self.verbs.append(verb)
        if verb == "open":
            return {
                "handle": HANDLE,
                "repo": payload["repo"],
                "base": payload.get("base") or "main",
                "baseSha": BASE_SHA,
                "shallow": bool(payload.get("depth")),
            }
        if verb == "list":
            entries = [{"path": p, "size": len(b)} for p, b in sorted(self.files.items())]
            return {"entries": entries, "total": len(entries), "truncated": False}
        if verb == "read":
            return {
                "files": [
                    {"path": p, "contentBase64": base64.b64encode(self.files[p]).decode()}
                    for p in payload["paths"]
                ],
                "skipped": [],
            }
        if verb == "close":
            return {"closed": True}
        raise AssertionError(f"unexpected verb {verb}")


class ContentModeTestCase(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker()
        for patcher in (
            patch.object(credential_proxy_client, "_workspace_call", self.broker),
            patch.object(inspect_repository, "content_mode_available", lambda: True),
            patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def run_command(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = inspect_repository.dispatch(argv)
        self.assertEqual(code, 0)
        return json.loads(out.getvalue().strip())

    def test_open_prints_the_sha_the_tree_was_cloned_at(self):
        """`owner/name@sha` needs a sha, and this side has no `.git` to ask.

        The fleet-audit declared-intent record names each repository at the
        commit it was read at; `open` is where a context repository's read
        starts, so the sha comes back with the handle.
        """
        payload = self.run_command(["open", "--repo", REPO, "--depth", "1"])
        self.assertEqual(payload["mode"], "content")
        self.assertEqual(payload["handle"], HANDLE)
        self.assertEqual(payload["repo"], REPO)
        self.assertEqual(payload["sha"], BASE_SHA)
        self.assertTrue(payload["shallow"])

    def test_a_content_clone_prints_the_sha_beside_the_tree(self):
        into = self.tmp_path / "copy"
        payload = self.run_command(["clone", "--repo", REPO, "--into", str(into)])
        self.assertEqual(payload["mode"], "content")
        self.assertEqual(payload["sha"], BASE_SHA)
        self.assertEqual(payload["repo"], REPO)
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["written"], 2)
        self.assertEqual(
            (into / "clusters" / "prod.tf").read_text(encoding="utf-8"), "replicas = 3\n"
        )
        # The copy is closed behind itself, and the sha was read before that.
        self.assertEqual(self.broker.verbs[-1], "close")
        self.assertFalse((into / ".git").exists())


if __name__ == "__main__":
    unittest.main()
