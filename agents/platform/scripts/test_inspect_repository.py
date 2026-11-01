"""Unit tests for the inspect-repository skill's reader.

The subject lives in `agents/platform/skills/inspect-repository/scripts/`, but
the test lives here for the reason `test_submit_suggestion.py` gives: CI
discovers tests in two directories and that is not one of them.

A real tar from `git archive` over a real local repository throughout, with
only the broker hop (`vcs_client.call`) stubbed, so `vcs_client.snapshot`
checks and unpacks it as it would the broker's. The properties worth asserting
-- that a tree arrives whole, that nothing arrives with a `.git` in it, that a
bound refuses rather than cuts -- are properties of that unpack, and a stubbed
`snapshot` would assert only that this file agrees with itself.

Run:
  python3 -m unittest discover -s agents/platform/scripts -p 'test_inspect_repository.py' -v
"""

import base64
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import vcs_client  # noqa: E402

SUBJECT = (
    HERE.parent / "skills" / "inspect-repository" / "scripts" / "inspect_repository.py"
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _load_subject():
    spec = importlib.util.spec_from_file_location("inspect_repository", SUBJECT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inspect_repository = _load_subject()


class InspectRepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

        self.seed = self.tmp / "seed"
        self.seed.mkdir()
        self._git(self.seed, "init", "--quiet", "--initial-branch=main")
        (self.seed / "README.md").write_text("a public repository\n")
        (self.seed / "cmd").mkdir()
        (self.seed / "cmd" / "main.go").write_text("func main() { serve() }\n")
        (self.seed / "cmd" / "serve.go").write_text("func serve() {}\n")
        self._git(self.seed, "add", "-A")
        self._git(self.seed, "commit", "--quiet", "-m", "seed")
        self.revision = self._git(self.seed, "rev-parse", "HEAD").stdout.strip()
        self.calls: list[tuple[str, dict]] = []

        patched = patch.object(vcs_client, "call", self.broker)
        patched.start()
        self.addCleanup(patched.stop)
        endpoint = patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"})
        endpoint.start()
        self.addCleanup(endpoint.stop)

    def broker(self, verb, payload):
        """`POST /v1/vcs/snapshot`, answered from the seed as the broker would."""
        self.calls.append((verb, payload))
        argv = ["--literal-pathspecs", "archive", "--format=tar", self.revision]
        if payload.get("paths"):
            argv += ["--", *payload["paths"]]
        env = dict(os.environ)
        env.update(GIT_ENV)
        made = subprocess.run(["git", *argv], cwd=str(self.seed), env=env, capture_output=True)
        if made.returncode != 0:
            raise vcs_client.VcsError(
                "a path in `paths` names nothing", code="PATH_NOT_FOUND", revision=self.revision
            )
        return {
            "forge": "github", "repo": payload["repository"], "role": "unregistered",
            "ref": payload.get("ref") or "main", "revision": self.revision,
            "size": len(made.stdout), "archiveBase64": base64.b64encode(made.stdout).decode(),
            "history": "none",
        }

    def _git(self, cwd, *args):
        env = dict(os.environ)
        env.update(GIT_ENV)
        return subprocess.run(
            ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, check=True
        )

    def run_command(self, argv) -> dict:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(inspect_repository.dispatch(argv), 0)
        return json.loads(out.getvalue())


class TestClone(InspectRepositoryTestCase):
    def test_a_public_repository_lands_as_readable_source(self):
        into = self.tmp / "copy"
        result = self.run_command(["clone", "--repo", "acme/public", "--into", str(into)])
        self.assertEqual(result["mode"], "content")
        self.assertTrue(result["complete"])
        self.assertEqual(result["written"], 3)
        self.assertEqual(result["sha"], self.revision)
        self.assertEqual(result["history"], "none")
        self.assertEqual((into / "cmd" / "main.go").read_text(), "func main() { serve() }\n")
        self.assertEqual(self.calls, [("snapshot", {"repository": "acme/public"})])

    def test_nothing_that_lands_is_a_git_repository(self):
        """The difference from the path this replaces, as an assertion."""
        into = self.tmp / "copy"
        self.run_command(["clone", "--repo", "acme/public", "--into", str(into)])
        self.assertEqual(list(into.rglob(".git")), [])

    def test_a_prefix_copies_one_subtree(self):
        into = self.tmp / "copy"
        result = self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into), "--prefix", "cmd"]
        )
        self.assertEqual(result["written"], 2)
        self.assertFalse((into / "README.md").exists())
        self.assertEqual(self.calls[0][1]["paths"], ["cmd"])

    def test_a_ref_is_passed_to_the_broker(self):
        into = self.tmp / "copy"
        result = self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into), "--ref", "release-1"]
        )
        self.assertEqual(self.calls[0][1]["ref"], "release-1")
        self.assertEqual(result["base"], "release-1")

    def test_a_prefix_that_names_nothing_is_an_empty_copy(self):
        """As a listing of the prefix was: nothing there, and nothing missed."""
        into = self.tmp / "copy"
        result = self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into), "--prefix", ".kube-agents"]
        )
        self.assertEqual(result["written"], 0)
        self.assertTrue(result["complete"])
        self.assertEqual(result["missing"], ".kube-agents")
        # The fleet audit searches the repository only when it has the commit.
        self.assertEqual(result["sha"], self.revision)
        self.assertEqual(list(into.iterdir()), [])

    def test_force_adds_a_second_prefix_to_the_tree(self):
        """The fleet audit builds one tree from several prefixes this way."""
        into = self.tmp / "copy"
        self.run_command(["clone", "--repo", "acme/public", "--into", str(into), "--prefix", "cmd"])
        self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into), "--prefix", "README.md", "--force"]
        )
        self.assertEqual(
            sorted(str(p.relative_to(into)) for p in into.rglob("*") if p.is_file()),
            ["README.md", "cmd/main.go", "cmd/serve.go"],
        )

    def test_force_replaces_a_symbolic_link_that_is_already_there(self):
        """The fleet audit takes `.kube-agents`, then the whole tree, into one directory."""
        (self.seed / ".kube-agents").mkdir()
        (self.seed / ".kube-agents" / "real.yaml").write_text("intent: one\n")
        os.symlink("real.yaml", self.seed / ".kube-agents" / "intent.yaml")
        self._git(self.seed, "add", "-A")
        self._git(self.seed, "commit", "--quiet", "-m", "intent")
        self.revision = self._git(self.seed, "rev-parse", "HEAD").stdout.strip()
        into = self.tmp / "copy"
        self.run_command(["clone", "--repo", "acme/public", "--into", str(into), "--prefix", ".kube-agents"])
        result = self.run_command(["clone", "--repo", "acme/public", "--into", str(into), "--force"])
        self.assertEqual(result["sha"], self.revision)
        link = into / ".kube-agents" / "intent.yaml"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "real.yaml")
        self.assertEqual((into / "cmd" / "main.go").read_text(), "func main() { serve() }\n")

    def test_force_does_not_write_through_a_link_in_the_destination(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        into = self.tmp / "copy"
        into.mkdir()
        os.symlink(str(outside), into / "cmd")
        self.run_command(["clone", "--repo", "acme/public", "--into", str(into), "--force"])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((into / "cmd").is_symlink())
        self.assertTrue((into / "cmd" / "main.go").is_file())

    def test_a_non_empty_destination_is_refused_without_force(self):
        into = self.tmp / "copy"
        into.mkdir()
        (into / "leftover.txt").write_text("from another analysis\n")
        with self.assertRaises(SystemExit):
            inspect_repository.dispatch(["clone", "--repo", "acme/public", "--into", str(into)])
        self.assertEqual(self.calls, [])

    def test_a_file_bound_refuses_the_whole_copy(self):
        into = self.tmp / "copy"
        with self.assertRaises(SystemExit) as caught:
            inspect_repository.dispatch(
                ["clone", "--repo", "acme/public", "--into", str(into), "--max-files", "2"]
            )
        self.assertIn("--prefix", str(caught.exception))
        self.assertEqual(list(into.iterdir()), [])
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith(".copy")], [])

    def test_a_byte_bound_refuses_the_whole_copy(self):
        into = self.tmp / "copy"
        with self.assertRaises(vcs_client.VcsError) as caught:
            inspect_repository.dispatch(
                ["clone", "--repo", "acme/public", "--into", str(into), "--max-bytes", "20"]
            )
        self.assertEqual(caught.exception.code, vcs_client.SNAPSHOT_TOO_LARGE)
        self.assertEqual(list(into.iterdir()), [])

    def test_depth_is_accepted_and_changes_nothing(self):
        into = self.tmp / "copy"
        result = self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into), "--depth", "1"]
        )
        self.assertEqual(result["written"], 3)
        self.assertNotIn("depth", self.calls[0][1])


class TestTheErrorContract(unittest.TestCase):
    """main() turns every broker failure into JSON on stderr, never a traceback."""

    def run_main(self, raised: Exception) -> tuple[int, dict]:
        err = io.StringIO()
        with patch.object(inspect_repository.sys, "argv", ["inspect_repository.py", "clone"]):
            with patch.object(inspect_repository, "dispatch", side_effect=raised), redirect_stderr(err):
                code = inspect_repository.main()
        return code, json.loads(err.getvalue())

    def test_a_refusal_is_a_sentence_with_its_code(self):
        code, answer = self.run_main(
            vcs_client.VcsError("the broker at x could not be reached: refused")
        )
        self.assertEqual(code, 1)
        self.assertIn("could not be reached", answer["error"])

    def test_a_snapshot_over_the_cap_names_the_way_round(self):
        code, answer = self.run_main(
            vcs_client.VcsError("too large", code=vcs_client.SNAPSHOT_TOO_LARGE)
        )
        self.assertEqual(code, 1)
        self.assertEqual(answer["code"], vcs_client.SNAPSHOT_TOO_LARGE)
        self.assertIn("--prefix", answer["hint"])


class TestDirectoryFallback(InspectRepositoryTestCase):
    def ensure(self, cloned):
        def ensure(repo, runner, **kwargs):
            cloned.update({"repo": repo, **kwargs})
            target = self.tmp / "leased"
            target.mkdir(exist_ok=True)
            return target

        return patch.object(inspect_repository.gitops_workspace, "ensure_workspace", ensure)

    def test_clone_with_no_broker_falls_back_to_a_leased_checkout(self):
        cloned = {}
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}), self.ensure(cloned):
            result = self.run_command(
                ["clone", "--repo", "acme/public", "--lease", "l1", "--depth", "1"]
            )
        self.assertEqual(result["mode"], "directory")
        self.assertEqual(cloned["repo"], "acme/public")
        self.assertEqual(cloned["owner"], "inspect-repository")
        # A shallow read was asked for and this path cannot give one. Reported
        # rather than silently ignored.
        self.assertTrue(result["depthIgnored"])
        self.assertEqual(self.calls, [])

    def test_a_broker_older_than_the_snapshot_verb_is_an_error_not_a_fallback(self):
        """The sandbox's git has no transport, so the leased checkout cannot work there."""
        cloned = {}

        def old_broker(verb, payload):
            raise vcs_client.VcsError(
                "this broker does not serve `snapshot`", code=vcs_client.BROKER_ROUTE_UNSUPPORTED
            )

        with patch.object(vcs_client, "call", old_broker), self.ensure(cloned), self.assertRaises(
            vcs_client.VcsError
        ) as caught:
            inspect_repository.dispatch(
                ["clone", "--repo", "acme/public", "--into", str(self.tmp / "copy"), "--lease", "l1"]
            )
        self.assertEqual(caught.exception.code, vcs_client.BROKER_ROUTE_UNSUPPORTED)
        self.assertEqual(cloned, {})

    def test_another_refusal_does_not_fall_back(self):
        def refusing(verb, payload):
            raise vcs_client.VcsError("not allowed", code="FORGE_UNSUPPORTED")

        with patch.object(vcs_client, "call", refusing), self.assertRaises(vcs_client.VcsError):
            inspect_repository.dispatch(
                ["clone", "--repo", "acme/public", "--into", str(self.tmp / "copy")]
            )


if __name__ == "__main__":
    unittest.main()
