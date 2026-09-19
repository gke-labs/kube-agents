#!/usr/bin/env python3
"""vcs_client.py as a library: the shape a consumer script imports, not the CLI.

The command-line suite (`skills/version-control/scripts/test_vcs.py`) drives
every operation through `vcs.py`, which is a thin front over this module; what
that suite cannot show is that the module stands on its own for a caller that
never builds an argparse namespace. These tests are that caller.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vcs_client  # noqa: E402

REAL_GIT = shutil.which("git") or "/usr/bin/git"


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"},
    )


class ForgeCallTest(unittest.TestCase):
    def test_forge_names_the_repository_and_drops_absent_fields(self):
        seen = []

        def fake_call(verb, payload):
            seen.append((verb, payload))
            return {"ok": True}

        with mock.patch.object(vcs_client, "call", fake_call):
            answer = vcs_client.forge(
                "issue-update", {"number": 7, "title": None, "labelsAdd": ["a"]}, "acme/infra"
            )
        self.assertEqual(answer, {"ok": True})
        self.assertEqual(seen, [("issue-update", {"number": 7, "labelsAdd": ["a"], "repository": "acme/infra"})])

    def test_capabilities_takes_a_repository_without_a_working_copy(self):
        with mock.patch.object(vcs_client, "call", lambda verb, payload: {"verb": verb, **payload}):
            self.assertEqual(
                vcs_client.capabilities("acme/infra"),
                {"verb": "capabilities", "repository": "acme/infra"},
            )

    def test_a_broker_refusal_keeps_its_code(self):
        error = vcs_client.credential_proxy_client.WorkspaceRequestError(
            "refused", payload={"error": "no", "code": "PROTECTED_BRANCH", "detail": "d"}
        )
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:1"}), mock.patch.object(
            vcs_client.credential_proxy_client, "vcs_call", side_effect=error
        ):
            with self.assertRaises(vcs_client.VcsError) as caught:
                vcs_client.call("publish", {})
        self.assertEqual(caught.exception.code, "PROTECTED_BRANCH")
        self.assertEqual(caught.exception.as_json(), {"error": "no", "code": "PROTECTED_BRANCH", "detail": "d"})


class WorkingCopyTest(unittest.TestCase):
    """clone -> branch -> commit -> publish, driven as a library against a local repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.origin = base / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "--quiet", "--initial-branch=main")
        (self.origin / "a.txt").write_text("a\n")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "--quiet", "-m", "seed")
        self.head = git(self.origin, "rev-parse", "HEAD").stdout.strip()
        self.published: list[dict] = []
        root = base / "root"
        for attribute, value in (("ROOT", root), ("SESSIONS", root / ".sessions"), ("LOCAL_GIT", REAL_GIT), ("call", self.call)):
            patch = mock.patch.object(vcs_client, attribute, value)
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, verb, payload):
        if verb == "clone":
            import base64
            bundle = Path(self.tmp.name) / "served.bundle"
            git(self.origin, "bundle", "create", str(bundle), "HEAD", "main")
            blob = bundle.read_bytes()
            return {"forge": "local", "repo": "acme/infra", "branch": "main", "revision": self.head,
                    "size": len(blob), "bundleBase64": base64.b64encode(blob).decode("ascii")}
        if verb == "publish":
            self.published.append(payload)
            return {"forge": "local", "repo": "acme/infra", "branch": payload["branch"], "revision": "f" * 40}
        raise AssertionError(verb)

    def test_the_library_round_trip(self):
        cloned = vcs_client.clone("acme/infra")
        self.assertEqual(cloned["revision"], self.head)
        self.assertEqual(cloned["remotes"], [])
        made = vcs_client.branch("acme/infra", "fix/one")
        self.assertTrue(made["created"])
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        committed = vcs_client.commit("change a", spec="acme/infra")
        self.assertEqual(committed["files"], ["a.txt"])
        answer = vcs_client.publish("acme/infra")
        self.assertEqual(answer["revisions"], 1)
        self.assertEqual(self.published[0]["branch"], "fix/one")
        self.assertEqual(self.published[0]["target"], "main")
        self.assertEqual(self.published[0]["clonedFrom"], "main")
        self.assertEqual(self.published[0]["baseRevision"], self.head)
        removed = vcs_client.discard("acme/infra")
        self.assertFalse(Path(removed["removed"]).exists())

    def test_publishing_the_cloned_branch_is_refused_before_any_call(self):
        cloned = vcs_client.clone("acme/infra")
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        vcs_client.commit("straight onto main", spec="acme/infra")
        with self.assertRaises(vcs_client.VcsError) as caught:
            vcs_client.publish("acme/infra", target="release")
        self.assertIn("cloned from", str(caught.exception))
        self.assertEqual(self.published, [])

    def test_advance_publishes_the_cloned_branch_and_says_so_to_the_broker(self):
        # The copy was taken of a proposal branch in order to add to it, which
        # is the one reason to write to the branch it came down on. The flag
        # travels: the broker refuses the same thing on its own, so a client
        # that waived the check quietly would be refused there instead.
        cloned = vcs_client.clone("acme/infra")
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        vcs_client.commit("another round on the proposal", spec="acme/infra")
        answer = vcs_client.publish("acme/infra", target="release", advance=True)
        self.assertEqual(answer["branch"], "main")
        self.assertTrue(self.published[0]["advance"])
        self.assertEqual(self.published[0]["clonedFrom"], "main")

    def test_one_repository_cloned_twice_gets_one_copy_per_branch(self):
        # The scratch root is shared by every card in the container, so the
        # copy is keyed on the branch as well as the repository. Keyed on the
        # repository alone, the second clone here landed on the first one.
        first = vcs_client.clone("acme/infra", key="fix/one")
        second = vcs_client.clone("acme/infra", key="fix/two")
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(first["path"].endswith("local__acme__infra__fix__one"))
        self.assertEqual(
            vcs_client.resolve_session("acme/infra", key="fix/two")["path"],
            second["path"],
        )
        # And discarding one leaves the other, session file included.
        vcs_client.discard("acme/infra", key="fix/two")
        self.assertFalse(Path(second["path"]).exists())
        self.assertEqual(
            vcs_client.resolve_session("acme/infra")["path"], first["path"]
        )

    def test_two_copies_are_ambiguous_until_one_is_named_or_stood_in(self):
        first = vcs_client.clone("acme/infra", key="fix/one")
        vcs_client.clone("acme/infra", key="fix/two")
        with self.assertRaises(vcs_client.VcsError) as caught:
            vcs_client.resolve_session("acme/infra")
        # The refusal hands over paths rather than a choice: the caller is
        # meant to be standing in the copy, and `cd` takes a path.
        self.assertIn(first["path"], str(caught.exception))
        self.assertIn("fix/two", str(caught.exception))

        here = Path.cwd()
        self.addCleanup(os.chdir, here)
        os.chdir(first["path"])
        self.assertEqual(
            vcs_client.resolve_session("acme/infra")["path"], first["path"]
        )

    def test_advance_still_refuses_a_target_that_is_the_branch_itself(self):
        cloned = vcs_client.clone("acme/infra")
        (Path(cloned["path"]) / "a.txt").write_text("b\n")
        vcs_client.commit("no target of its own", spec="acme/infra")
        with self.assertRaises(vcs_client.VcsError) as caught:
            vcs_client.publish("acme/infra", advance=True)
        self.assertIn("branch and target are both", str(caught.exception))
        self.assertEqual(self.published, [])


if __name__ == "__main__":
    unittest.main()
