"""Unit tests for the inspect-repository skill's reader.

The subject lives in `agents/platform/skills/inspect-repository/scripts/`, but
the test lives here for the reason `test_submit_suggestion.py` gives: CI
discovers tests in two directories and that is not one of them.

A real `ContentWorkspaceStore` over a real local bare repository throughout,
with only the HTTP hop stubbed. The properties worth asserting -- that a tree
arrives whole, that nothing arrives with a `.git` in it, that a bound which bit
is reported -- are properties of what the broker does with git, and a stubbed
store would assert only that this file agrees with itself.

Run:
  python3 -m unittest discover -s agents/platform/scripts -p 'test_inspect_repository.py' -v
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import content_workspace  # noqa: E402
import credential_proxy  # noqa: E402
import credential_proxy_client  # noqa: E402
import workspace_paths  # noqa: E402

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

URL = "https://github.com/acme/public.git"


@dataclass
class GitResult:
    """What the broker's `_git` reads off a run: an exit code, not a returncode."""

    exit_code: int
    stdout: str
    stderr: str


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

        seed = self.tmp / "seed"
        seed.mkdir()
        self._git(seed, "init", "--quiet", "--initial-branch=main")
        (seed / "README.md").write_text("a public repository\n")
        (seed / "cmd").mkdir()
        (seed / "cmd" / "main.go").write_text("func main() { serve() }\n")
        (seed / "cmd" / "serve.go").write_text("func serve() {}\n")
        self._git(seed, "add", "-A")
        self._git(seed, "commit", "--quiet", "-m", "seed")
        self.origin = self.tmp / "origin.git"
        self._git(self.tmp, "clone", "--quiet", "--bare", str(seed), str(self.origin))

        origin = self.origin

        def runner(argv, cwd):
            argv = [str(origin) if token == URL else token for token in argv]
            env = dict(os.environ)
            env.update(GIT_ENV)
            completed = subprocess.run(
                argv, cwd=str(cwd), env=env, capture_output=True, text=True
            )
            return GitResult(completed.returncode, completed.stdout, completed.stderr)

        self.store = content_workspace.ContentWorkspaceStore(
            self.tmp / "broker" / "trees", self.tmp / "agent", runner
        )
        self.available = True

        # Through the broker's own route rather than straight at the store. The
        # translation from a JSON payload to the store's arguments is where a
        # verb goes missing, so a harness that reimplemented it would assert
        # that this file agrees with itself.
        route = credential_proxy.CredentialProxyHandler._workspace_route
        store = self.store

        class Router:
            workspaces = store

        def call(endpoint, verb, payload):
            if not self.available:
                raise credential_proxy_client.WorkspaceUnavailable("not armed")
            try:
                body = route(Router(), verb, payload)
            except content_workspace.ContentWorkspaceError as exc:
                raise credential_proxy_client.WorkspaceRequestError(
                    exc.status,
                    {"status": "blocked", "code": exc.code, "message": str(exc)},
                ) from exc
            if body is None:
                raise credential_proxy_client.WorkspaceRequestError(
                    404, {"status": "not_found"}
                )
            return body

        patched = patch.object(credential_proxy_client, "_workspace_call", call)
        patched.start()
        self.addCleanup(patched.stop)
        endpoint = patch.dict(
            os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"}
        )
        endpoint.start()
        self.addCleanup(endpoint.stop)

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
        result = self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into)]
        )
        self.assertEqual(result["mode"], "content")
        self.assertTrue(result["complete"])
        self.assertEqual(result["written"], 3)
        self.assertEqual(
            (into / "cmd" / "main.go").read_text(), "func main() { serve() }\n"
        )

    def test_nothing_that_lands_is_a_git_repository(self):
        """The difference from the path this replaces, as an assertion."""
        into = self.tmp / "copy"
        self.run_command(["clone", "--repo", "acme/public", "--into", str(into)])
        self.assertEqual(list(into.rglob(".git")), [])
        self.assertEqual(list(into.rglob(".git/**/*")), [])

    def test_a_prefix_copies_one_subtree(self):
        into = self.tmp / "copy"
        result = self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into), "--prefix", "cmd"]
        )
        self.assertEqual(result["written"], 2)
        self.assertFalse((into / "README.md").exists())

    def test_paging_gets_the_whole_tree_when_the_listing_is_capped(self):
        """The listing ceiling must not silently become the copy's ceiling."""
        self.enterContext(patch.object(content_workspace, "max_entries", lambda: 1))
        into = self.tmp / "copy"
        result = self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into)]
        )
        self.assertEqual(result["written"], 3)
        self.assertTrue(result["complete"])

    def test_a_file_bound_stops_and_says_which_bound(self):
        into = self.tmp / "copy"
        result = self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into), "--max-files", "2"]
        )
        self.assertEqual(result["stopped"], "maxFiles")
        self.assertFalse(result["complete"])
        self.assertEqual(result["written"], 2)

    def test_a_byte_bound_stops_and_says_which_bound(self):
        into = self.tmp / "copy"
        result = self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into), "--max-bytes", "20"]
        )
        self.assertEqual(result["stopped"], "maxBytes")
        self.assertFalse(result["complete"])

    def test_a_non_empty_destination_is_refused_without_force(self):
        into = self.tmp / "copy"
        into.mkdir()
        (into / "leftover.txt").write_text("from another analysis\n")
        with self.assertRaises(SystemExit):
            inspect_repository.dispatch(
                ["clone", "--repo", "acme/public", "--into", str(into)]
            )
        result = self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into), "--force"]
        )
        self.assertEqual(result["written"], 3)

    def test_the_broker_side_tree_does_not_outlive_the_clone(self):
        into = self.tmp / "copy"
        self.run_command(["clone", "--repo", "acme/public", "--into", str(into)])
        self.assertEqual(self.store._workspaces, {})

    def test_a_shallow_clone_reads_the_same_files(self):
        into = self.tmp / "copy"
        result = self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into), "--depth", "1"]
        )
        self.assertEqual(result["written"], 3)


class TestSearchThenFetch(InspectRepositoryTestCase):
    def open_handle(self) -> str:
        return self.run_command(["open", "--repo", "acme/public"])["handle"]

    def test_open_hands_back_a_handle_and_no_path(self):
        result = self.run_command(["open", "--repo", "acme/public"])
        self.assertEqual(result["repo"], "acme/public")
        self.assertNotIn(str(self.tmp), json.dumps(result))

    def test_grep_names_the_file_a_symbol_is_defined_in(self):
        handle = self.open_handle()
        result = self.run_command(
            ["grep", "--handle", handle, "--pattern", "func serve"]
        )
        self.assertEqual(
            [match["path"] for match in result["matches"]], ["cmd/serve.go"]
        )

    def test_fetch_copies_only_the_named_files(self):
        handle = self.open_handle()
        into = self.tmp / "picked"
        result = self.run_command(
            ["fetch", "--handle", handle, "--into", str(into), "cmd/serve.go"]
        )
        self.assertEqual(result["written"], 1)
        self.assertEqual(result["skipped"], [])
        self.assertEqual([p.name for p in into.rglob("*") if p.is_file()], ["serve.go"])

    def test_fetch_adds_to_a_directory_it_is_building(self):
        """Iterative by design: a search, then its hits, then theirs."""
        handle = self.open_handle()
        into = self.tmp / "picked"
        self.run_command(
            ["fetch", "--handle", handle, "--into", str(into), "cmd/serve.go"]
        )
        self.run_command(
            ["fetch", "--handle", handle, "--into", str(into), "cmd/main.go"]
        )
        self.assertEqual(
            sorted(p.name for p in into.rglob("*") if p.is_file()),
            ["main.go", "serve.go"],
        )

    def test_a_path_that_is_not_there_is_reported_rather_than_fatal(self):
        handle = self.open_handle()
        into = self.tmp / "picked"
        result = self.run_command(
            [
                "fetch",
                "--handle", handle,
                "--into", str(into),
                "cmd/serve.go",
                "cmd/absent.go",
            ]
        )
        self.assertEqual(result["written"], 1)
        self.assertEqual(
            result["skipped"], [{"path": "cmd/absent.go", "reason": "notAFile"}]
        )

    def test_the_broker_refuses_a_path_that_would_escape_the_repository(self):
        handle = self.open_handle()
        into = self.tmp / "picked"
        with self.assertRaises(credential_proxy_client.WorkspaceRequestError):
            inspect_repository.dispatch(
                ["fetch", "--handle", handle, "--into", str(into), "../../etc/passwd"]
            )
        self.assertFalse(into.exists() and any(into.iterdir()))

    def test_the_writer_refuses_an_escaping_name_of_its_own_accord(self):
        """The second check, at the boundary where a name becomes a write.

        The broker validated these paths already. This asserts the local writer
        does not depend on that -- it is the one that turns a name into a file,
        and a check next to the effect is the one that holds if the other side
        ever answers something else.
        """
        into = self.tmp / "picked"
        into.mkdir()
        for hostile in ("../escape", "/etc/passwd", ".git/config"):
            with self.subTest(path=hostile), self.assertRaises(
                workspace_paths.WorkspaceError
            ):
                inspect_repository.write_files(into, {hostile: b"x"})
        self.assertEqual(list(into.rglob("*")), [])

    def test_a_listing_page_names_its_own_cursor(self):
        handle = self.open_handle()
        self.enterContext(patch.object(content_workspace, "max_entries", lambda: 1))
        page = self.run_command(["list", "--handle", handle])
        self.assertTrue(page["truncated"])
        self.assertEqual(page["next"], page["entries"][-1]["path"])
        rest = self.run_command(["list", "--handle", handle, "--after", page["next"]])
        self.assertNotEqual(rest["entries"], page["entries"])

    def test_close_drops_the_broker_side_tree(self):
        handle = self.open_handle()
        self.run_command(["close", "--handle", handle])
        self.assertEqual(self.store._workspaces, {})

    def test_close_does_not_ask_whether_the_broker_is_armed(self):
        """The other handle commands probe first; this one must not.

        A handle exists only because an `open` succeeded, so the probe can tell
        the caller nothing it does not already know -- and a broker having a bad
        few seconds answers it "unavailable", which refuses the one command that
        releases the clone. The clone then sits on the broker's volume with
        nothing left holding its handle.
        """
        handle = self.open_handle()
        with patch.object(
            inspect_repository,
            "content_mode_available",
            lambda: self.fail("close probed the broker before releasing the clone"),
        ):
            self.run_command(["close", "--handle", handle])
        self.assertEqual(self.store._workspaces, {})


class TestTheErrorContract(unittest.TestCase):
    """main() turns every way a broker call fails into a sentence.

    Not the two workspace exceptions -- those were always caught. These are the
    two that reach main() from before the broker answers: no token to send, and
    no connection to send it over. A traceback for either reads to the agent as
    the script being broken and sends it to fix the wrong thing.
    """

    def run_main(self, raised: Exception) -> tuple[int, str]:
        err = io.StringIO()
        with patch.object(inspect_repository.sys, "argv", ["inspect_repository.py", "close"]):
            with patch.object(
                inspect_repository, "dispatch", side_effect=raised
            ), redirect_stderr(err):
                code = inspect_repository.main()
        return code, err.getvalue()

    def test_a_missing_caller_token_is_a_sentence(self):
        code, message = self.run_main(
            credential_proxy_client.TokenUnavailable("/var/run/token is empty")
        )
        self.assertEqual(code, 1)
        self.assertIn("/var/run/token is empty", message)

    def test_an_unreachable_broker_is_a_sentence(self):
        code, message = self.run_main(
            urllib.error.URLError("Connection refused")
        )
        self.assertEqual(code, 1)
        self.assertIn("Connection refused", message)


class TestDirectoryFallback(InspectRepositoryTestCase):
    def test_the_handle_commands_say_what_is_missing(self):
        self.available = False
        for argv in (
            ["open", "--repo", "acme/public"],
            ["list", "--handle", "x"],
            ["grep", "--handle", "x", "--pattern", "y"],
            ["fetch", "--handle", "x", "--into", str(self.tmp / "d"), "a"],
        ):
            with self.subTest(argv=argv[0]), self.assertRaises(SystemExit) as caught:
                inspect_repository.dispatch(argv)
            self.assertIn("content-passing broker", str(caught.exception))


    def test_clone_falls_back_to_a_leased_checkout(self):
        self.available = False
        cloned = {}

        def ensure(repo, runner, **kwargs):
            cloned.update({"repo": repo, **kwargs})
            target = self.tmp / "leased"
            target.mkdir(exist_ok=True)
            return target

        with patch.object(
            inspect_repository.gitops_workspace, "ensure_workspace", ensure
        ):
            result = self.run_command(
                ["clone", "--repo", "acme/public", "--lease", "l1", "--depth", "1"]
            )
        self.assertEqual(result["mode"], "directory")
        self.assertEqual(cloned["repo"], "acme/public")
        self.assertEqual(cloned["owner"], "inspect-repository")
        # A shallow read was asked for and this path cannot give one. Reported
        # rather than silently ignored.
        self.assertTrue(result["depthIgnored"])


if __name__ == "__main__":
    unittest.main()
