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
import signal
import subprocess
import sys
import tempfile
import threading
import time
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
        endpoint = patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765", "PLATFORM_AGENT_HOME": str(self.tmp / "home")},
        )
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

    def test_force_refuses_a_file_over_a_directory_and_changes_nothing(self):
        """The repository names the file; the directory is the caller's."""
        into = self.tmp / "copy"
        (into / "cmd" / "serve.go").mkdir(parents=True)
        (into / "cmd" / "serve.go" / "keep.txt").write_text("the caller's\n")
        # Walked before the clash: removed at once, it would be gone after the refusal.
        (into / "README.md").write_text("older\n")
        with self.assertRaises(SystemExit) as refused:
            inspect_repository.dispatch(["clone", "--repo", "acme/public", "--into", str(into), "--force"])
        self.assertIn("is a directory", str(refused.exception))
        self.assertEqual((into / "cmd" / "serve.go" / "keep.txt").read_text(), "the caller's\n")
        self.assertEqual((into / "README.md").read_text(), "older\n")
        self.assertFalse((into / "cmd" / "main.go").exists())

    def test_force_removes_nothing_through_a_link_in_the_destination(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "main.go").write_text("not the repository's\n")
        into = self.tmp / "copy"
        into.mkdir()
        os.symlink(str(outside), into / "cmd")
        self.run_command(["clone", "--repo", "acme/public", "--into", str(into), "--force"])
        self.assertEqual((outside / "main.go").read_text(), "not the repository's\n")
        self.assertEqual((into / "cmd" / "main.go").read_text(), "func main() { serve() }\n")

    def test_force_refuses_a_directory_over_a_file(self):
        into = self.tmp / "copy"
        into.mkdir()
        (into / "cmd").write_text("the caller's\n")
        with self.assertRaises(SystemExit) as refused:
            inspect_repository.dispatch(["clone", "--repo", "acme/public", "--into", str(into), "--force"])
        self.assertIn("is a file", str(refused.exception))
        self.assertEqual((into / "cmd").read_text(), "the caller's\n")
        self.assertFalse((into / "README.md").exists())

    def test_skipped_paths_carry_the_reasons_the_fleet_audit_reads(self):
        """audit_report counts a skipped note as missed unless its reason is `symlink` or `notAFile`."""
        os.symlink("/etc/hostname", self.seed / "outside.md")
        for name in ("HEAD", "objects/pack/info", "refs/heads/main"):
            (self.seed / "fixture" / name).parent.mkdir(parents=True, exist_ok=True)
            (self.seed / "fixture" / name).write_text("x\n")
        self._git(self.seed, "add", "-A")
        self._git(self.seed, "commit", "--quiet", "-m", "a link and a bare layout")
        self.revision = self._git(self.seed, "rev-parse", "HEAD").stdout.strip()
        result = self.run_command(["clone", "--repo", "acme/public", "--into", str(self.tmp / "copy")])
        self.assertEqual(
            sorted((entry["path"], entry["reason"]) for entry in result["skipped"]),
            [("fixture", "notAFile"), ("outside.md", "symlink")],
        )
        self.assertFalse(result["complete"])

    def test_a_non_empty_destination_is_refused_without_force(self):
        into = self.tmp / "copy"
        into.mkdir()
        (into / "leftover.txt").write_text("from another analysis\n")
        with self.assertRaises(SystemExit):
            inspect_repository.dispatch(["clone", "--repo", "acme/public", "--into", str(into)])
        self.assertEqual(self.calls, [])

    def test_a_file_bound_refuses_the_whole_copy_before_anything_is_unpacked(self):
        into = self.tmp / "copy"
        unpacked = patch.object(
            vcs_client.tarfile.TarFile, "extractall", side_effect=AssertionError("unpacked")
        )
        for force, there in ((False, []), (True, ["keep.txt"])):
            if force:
                (into / "keep.txt").write_text("the caller's\n")
            with self.subTest(force=force), unpacked, self.assertRaises(SystemExit) as caught:
                inspect_repository.dispatch(
                    ["clone", "--repo", "acme/public", "--into", str(into), "--max-files", "2"]
                    + (["--force"] if force else [])
                )
            self.assertIn("3 files", str(caught.exception))
            self.assertIn("--max-files", str(caught.exception))
            self.assertIn("--prefix", str(caught.exception))
            self.assertEqual([p.name for p in into.iterdir()], there)
            self.assertEqual(self.staging_left(), [])
        # Paired: at the bound the copy is written.
        result = self.run_command(
            ["clone", "--repo", "acme/public", "--into", str(into), "--max-files", "3", "--force"]
        )
        self.assertEqual(result["written"], 3)

    def test_every_clone_stages_in_the_script_s_own_folder(self):
        """The script's staging and the snapshot's are both in staging_root(), with or without --force."""
        into = self.tmp / "copy"
        root = inspect_repository.staging_root()
        for force in (False, True):
            with self.subTest(force=force), patch.object(
                vcs_client, "snapshot", wraps=vcs_client.snapshot
            ) as snapshot, patch.object(tempfile, "mkdtemp", wraps=tempfile.mkdtemp) as mkdtemp:
                self.run_command(
                    ["clone", "--repo", "acme/public", "--into", str(into)] + (["--force"] if force else [])
                )
            self.assertEqual(snapshot.call_args.kwargs["into"].parent, root)
            # The script's staging, then the snapshot's beside it.
            self.assertEqual([c.kwargs["dir"] for c in mkdtemp.call_args_list], [str(root)] * 2)
            self.assertEqual(self.staging_left(), [])
        self.assertEqual((into / "cmd" / "main.go").read_text(), "func main() { serve() }\n")

    def test_the_current_directory_by_its_full_path_stays_valid(self):
        """The snapshot must not remove an empty `into` that existed and put a new one there."""
        work = self.tmp / "work"
        work.mkdir()
        cwd = os.getcwd()
        os.chdir(work)
        self.addCleanup(os.chdir, cwd)
        self.run_command(["clone", "--repo", "acme/public", "--into", str(work)])
        self.assertEqual(sorted(os.listdir(".")), ["README.md", "cmd"])
        self.assertEqual(Path("cmd/main.go").read_text(), "func main() { serve() }\n")

    def test_a_link_to_an_empty_directory_is_written_through(self):
        target = self.tmp / "target"
        target.mkdir()
        link = self.tmp / "copy"
        link.symlink_to(target)
        result = self.run_command(["clone", "--repo", "acme/public", "--into", str(link)])
        self.assertEqual(result["written"], 3)
        self.assertTrue(link.is_symlink())
        self.assertEqual((target / "cmd" / "main.go").read_text(), "func main() { serve() }\n")
        self.assertEqual(self.staging_left(), [])

    def test_the_current_directory_can_be_the_destination(self):
        work = self.tmp / "work"
        work.mkdir()
        cwd = os.getcwd()
        os.chdir(work)
        self.addCleanup(os.chdir, cwd)
        self.run_command(["clone", "--repo", "acme/public", "--into", "."])
        self.assertEqual((work / "cmd" / "main.go").read_text(), "func main() { serve() }\n")
        self.run_command(["clone", "--repo", "acme/public", "--into", ".", "--force"])
        self.assertEqual(sorted(os.listdir(".")), ["README.md", "cmd"])

    def test_two_clones_into_one_fresh_destination_both_complete(self):
        """Two cards clone one repository to its default destination at the same time."""
        arrivals = []
        lock = threading.Lock()
        second_done = threading.Event()

        def broker(verb, payload):
            with lock:
                arrivals.append(threading.current_thread())
                first = len(arrivals) == 1
            if first:
                # Both found the destination empty. The first answer comes
                # after the second clone wrote into the destination.
                if not second_done.wait(30):
                    raise AssertionError("the second clone did not end")
            return self.broker(verb, payload)

        codes, errors = [], []

        def clone():
            try:
                codes.append(inspect_repository.dispatch(["clone", "--repo", "acme/public"]))
            except BaseException as exc:  # noqa: BLE001 - SystemExit too
                errors.append(exc)
            finally:
                if arrivals and arrivals[0] is not threading.current_thread():
                    second_done.set()

        out = io.StringIO()
        with patch.object(vcs_client, "call", broker), redirect_stdout(out):
            threads = [threading.Thread(target=clone) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(codes, [0, 0])
        self.assertEqual(len(arrivals), 2)
        into = inspect_repository.default_into("acme/public")
        self.assertEqual([json.loads(line)["into"] for line in out.getvalue().splitlines()], [str(into)] * 2)
        self.assertEqual(
            sorted(str(p.relative_to(into)) for p in into.rglob("*") if p.is_file()),
            ["README.md", "cmd/main.go", "cmd/serve.go"],
        )
        self.assertEqual(self.staging_left(), [])

    def age(self, path):
        old = time.time() - 2 * inspect_repository.STALE_STAGING_AGE_SECONDS
        os.utime(path, (old, old), follow_symlinks=False)

    def staging_left(self):
        root = inspect_repository.staging_root()
        return sorted(p.name for p in root.iterdir()) if root.exists() else []

    def test_a_clone_killed_before_the_copy_leaves_staging_the_next_clone_removes(self):
        """A SIGKILL does not run the `finally` that removes the staging directory."""
        into = self.tmp / "copy"
        pid = os.fork()
        if pid == 0:
            try:
                with patch.object(
                    inspect_repository, "copy_over", lambda staging, into: os.kill(os.getpid(), signal.SIGKILL)
                ):
                    inspect_repository.dispatch(["clone", "--repo", "acme/public", "--into", str(into), "--force"])
            finally:
                os._exit(1)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
        left = self.staging_left()
        self.assertEqual(len(left), 1, left)
        root = inspect_repository.staging_root()
        self.assertTrue((root / left[0] / "cmd" / "main.go").is_file())

        # Young: it can belong to a clone that runs now, so it stays.
        self.run_command(["clone", "--repo", "acme/public", "--into", str(into), "--force"])
        self.assertEqual(self.staging_left(), left)

        self.age(root / left[0])
        self.run_command(["clone", "--repo", "acme/public", "--into", str(into), "--force"])
        self.assertEqual(self.staging_left(), [])

    def test_a_clone_removes_only_old_entries_of_the_staging_folder(self):
        root = inspect_repository.staging_root()
        root.mkdir(parents=True)
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("not staging\n")
        old = root / "any-name"
        fresh = root / "clone.fresh123"
        for path in (old, fresh):
            path.mkdir()
            (path / "file").write_text("x\n")
        self.age(old)
        link = root / "link"
        link.symlink_to(outside)
        self.age(link)
        into = self.tmp / "copy"
        self.run_command(["clone", "--repo", "acme/public", "--into", str(into)])
        self.assertFalse(old.exists())
        self.assertTrue((fresh / "file").is_file())
        self.assertTrue(link.is_symlink())
        self.assertEqual((outside / "keep.txt").read_text(), "not staging\n")
        self.assertTrue((into / "README.md").is_file())

    def test_a_clone_removes_nothing_beside_the_destination(self):
        """A caller's old copy beside --into stays, whatever its name."""
        names = (".src.original", ".src.20251103", ".src.abc12345", "..src.abc12345.def67890")
        for name in names:
            (self.tmp / name).mkdir()
            (self.tmp / name / "file").write_text("the caller's\n")
            self.age(self.tmp / name)
        into = self.tmp / "src"
        self.run_command(["clone", "--repo", "acme/public", "--into", str(into)])
        self.run_command(["clone", "--repo", "acme/public", "--into", str(into), "--force"])
        for name in names:
            self.assertEqual((self.tmp / name / "file").read_text(), "the caller's\n", name)

    def test_a_leftover_that_cannot_be_removed_does_not_stop_the_clone(self):
        locked = inspect_repository.staging_root() / "clone.abc12345" / "locked"
        locked.mkdir(parents=True)
        (locked / "file").write_text("x\n")
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        self.age(locked.parent)
        result = self.run_command(["clone", "--repo", "acme/public", "--into", str(self.tmp / "copy")])
        self.assertEqual(result["written"], 3)

    def test_a_leftover_another_clone_removes_meanwhile_does_not_stop_the_clone(self):
        """Another clone's `finally` can remove its staging between the listing and the age check."""
        gone = inspect_repository.staging_root() / "clone.gone1234"
        gone.mkdir(parents=True)
        real_lstat = Path.lstat

        def lstat(path, *args, **kwargs):
            if path == gone:
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return real_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", lstat):
            result = self.run_command(["clone", "--repo", "acme/public", "--into", str(self.tmp / "copy")])
        self.assertEqual(result["written"], 3)

    def test_a_destination_in_the_staging_folder_is_refused(self):
        root = inspect_repository.staging_root()
        for into in (root, root / "mine", root / "a" / "b"):
            with self.subTest(into=str(into)):
                with self.assertRaises(SystemExit) as refused:
                    inspect_repository.dispatch(["clone", "--repo", "acme/public", "--into", str(into)])
                self.assertIn("holds only clone staging", str(refused.exception))
                self.assertFalse((into / "README.md").exists())
        self.assertEqual(self.calls, [])

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
