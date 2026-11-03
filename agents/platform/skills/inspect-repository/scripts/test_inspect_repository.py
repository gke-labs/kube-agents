"""Unit tests for inspect_repository — reading a repository through the broker.

Run:
  python3 -m unittest discover -s agents/platform/skills/inspect-repository/scripts \
      -p 'test_inspect_repository.py' -v

Stdlib only. The broker is stubbed at `vcs_client.snapshot`, which writes the
files a snapshot would have unpacked; nothing here opens a socket or runs
`git`. `agents/platform/scripts/test_inspect_repository.py` runs the real
unpack over a real `git archive`.
"""

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

import inspect_repository  # noqa: E402
import vcs_client  # noqa: E402

REPO = "acme/terraform-live"
REVISION = "9f8e7d6c5b4a39281706958473625140abcdef01"
FILES = {"README.md": "# terraform-live\n", "clusters/prod.tf": "replicas = 3\n"}


def fake_snapshot(repository, ref=None, paths=None, *, into, max_bytes):
    for name, text in FILES.items():
        target = Path(into) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return {
        "forge": "github", "repo": repository, "role": "context", "ref": ref or "main",
        "revision": REVISION, "path": str(into), "files": len(FILES),
        "bytes": sum(len(t) for t in FILES.values()), "skipped": [],
        "archiveBytes": 1024, "history": "none",
    }


class DefaultIntoTest(unittest.TestCase):
    def test_github_keeps_owner__name_and_another_forge_gets_one_injective_directory(self):
        with patch.object(inspect_repository, "scratch_root", return_value=Path("/s")):
            self.assertEqual(Path("/s/acme__fleet"), inspect_repository.default_into("acme/fleet"))
            self.assertEqual(
                Path("/s/gitlab.com%2Facme%2Fplatform%2Finfra"),
                inspect_repository.default_into("gitlab.com/acme/platform/infra"),
            )
            self.assertNotEqual(
                inspect_repository.default_into("gitlab.com/a/b__c"),
                inspect_repository.default_into("gitlab.com/a__b/c"),
            )


class ContentModeTest(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.object(vcs_client, "snapshot", fake_snapshot),
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

    def test_a_content_clone_prints_the_sha_beside_the_tree(self):
        """`owner/name@sha` needs a sha, and this side has no `.git` to ask."""
        into = self.tmp_path / "copy"
        payload = self.run_command(["clone", "--repo", REPO, "--into", str(into)])
        self.assertEqual(payload["mode"], "content")
        self.assertEqual(payload["sha"], REVISION)
        self.assertEqual(payload["repo"], REPO)
        self.assertEqual(payload["into"], str(into))
        self.assertTrue(payload["complete"])
        self.assertIsNone(payload["stopped"])
        self.assertEqual(payload["skipped"], [])
        self.assertEqual(payload["written"], 2)
        self.assertEqual((into / "clusters" / "prod.tf").read_text(encoding="utf-8"), "replicas = 3\n")
        self.assertFalse((into / ".git").exists())

    def test_the_staging_directory_does_not_outlive_the_clone(self):
        into = self.tmp_path / "copy"
        self.run_command(["clone", "--repo", REPO, "--into", str(into)])
        self.assertEqual([p.name for p in self.tmp_path.iterdir()], ["copy"])


if __name__ == "__main__":
    unittest.main()
