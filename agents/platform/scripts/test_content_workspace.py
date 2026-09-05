"""Tests for broker-owned, content-passed git workspaces.

Every hardening test here is paired with an ordinary-use assertion, usually in
the same method and always adjacent. That pairing is a requirement rather than a
courtesy: a refusal test passes just as well against a control that refuses
*everything*, so mutation coverage on its own proves the check is load-bearing
without proving the product still works. Both halves have to fail for different
reasons before the control is believable.
"""

from __future__ import annotations

import base64
import faulthandler
import os
import subprocess
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import content_workspace
from content_workspace import (
    Change,
    Conflict,
    ContentWorkspaceError,
    ContentWorkspaceStore,
    NoSuchHandle,
    PathRefused,
    TooLarge,
    Workspace,
    assert_disjoint_roots,
    check_branch,
    check_expected_sha,
    parse_changes,
    repo_relative,
)


@dataclass
class FakeResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class RecordingRunner:
    """Stands in for the executor, recording the argv it was handed."""

    def __init__(self, responses: dict[str, FakeResult] | None = None) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.responses = responses or {}

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), Path(cwd)))
        for key, response in self.responses.items():
            if key in " ".join(argv):
                return response
        return FakeResult()

    @property
    def subcommands(self) -> list[str]:
        return [argv[1] for argv, _ in self.calls]


def git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def real_git_runner(argv, cwd):
    """A plain runner, for the tests that need git's real answers."""
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    completed = subprocess.run(
        argv, cwd=str(cwd), env=environment, capture_output=True, text=True
    )
    return FakeResult(completed.returncode, completed.stdout, completed.stderr)


class RepoRelativeTest(unittest.TestCase):
    """What a path may name. One validator, used by reads and writes alike."""

    def test_paths_into_the_git_directory_are_refused(self):
        # The whole reason content-passing exists: `.git/config` and
        # `.git/hooks/pre-commit` are code execution in the credential holder,
        # and neither is content.
        for path in (
            ".git/config",
            ".git/hooks/pre-commit",
            "manifests/.git/config",
            ".git",
        ):
            with self.subTest(path=path):
                with self.assertRaises(PathRefused):
                    repo_relative(path)

        # Paired ordinary use: the paths a GitOps repository is actually made
        # of, including the ones that merely start with the same four letters.
        for path in (
            "manifests/prod/deployment.yaml",
            ".gitignore",
            ".gitkeep",
            ".gitattributes",
            "gitops/cluster.yaml",
            "charts/kube-agents/values.yaml",
        ):
            with self.subTest(path=path):
                self.assertEqual(path, str(repo_relative(path)))

    def test_every_spelling_of_dot_git_that_a_filesystem_accepts(self):
        """Match git's own equivalences, not just the ASCII one.

        git refuses these in a tree because NTFS strips trailing dots and offers
        an 8.3 shortname, and HFS+ ignores zero-width codepoints inside a name.
        A checker that knows only `.git` is a checker that disagrees with the
        thing it is protecting, which is the defect class this codebase keeps
        producing.
        """
        for spelling in (
            ".GIT/config",
            ".Git/config",
            ".git./config",
            ".git /config",
            "git~1/config",
            "GIT~1/config",
            ".g‌it/config",  # zero-width non-joiner, ignored by HFS+
        ):
            with self.subTest(spelling=spelling):
                with self.assertRaises(PathRefused):
                    repo_relative(spelling)

        # Paired: names that only resemble one. Over-refusal here would break
        # ordinary repositories, so the boundary has to be in the right place.
        for ordinary in ("gitops/x.yaml", "git/x.yaml", "digit/x.yaml", ".gitmodules"):
            with self.subTest(ordinary=ordinary):
                self.assertEqual(ordinary, str(repo_relative(ordinary)))

    def test_traversal_and_ambiguous_spellings_are_refused_not_normalised(self):
        for path in (
            "../etc/passwd",
            "manifests/../../etc/passwd",
            "/etc/passwd",
            "manifests//deployment.yaml",
            "manifests/./deployment.yaml",
            "manifests\\deployment.yaml",
            "manifests/dep\x00loyment.yaml",
            "",
        ):
            with self.subTest(path=path):
                with self.assertRaises(PathRefused):
                    repo_relative(path)

        # Paired: the unambiguous spelling of the same depth of nesting works.
        self.assertEqual(
            "manifests/prod/deployment.yaml",
            str(repo_relative("manifests/prod/deployment.yaml")),
        )


class SymlinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "manifests").mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def test_a_write_never_follows_a_symbolic_link(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.root / "vendor").symlink_to(outside)
        (self.root / "manifests" / "escape.yaml").symlink_to(outside / "escape.yaml")

        # A repository may legitimately contain symlinks; writing *through* one
        # lands the bytes where the name did not say.
        with self.assertRaises(PathRefused):
            content_workspace._no_symlink_on_the_way(
                self.root, repo_relative("vendor/x.yaml")
            )
        with self.assertRaises(PathRefused):
            content_workspace._no_symlink_on_the_way(
                self.root, repo_relative("manifests/escape.yaml")
            )

        # Paired ordinary use: an ordinary nested path, existing or not,
        # resolves to exactly the file its name describes.
        self.assertEqual(
            self.root / "manifests" / "deployment.yaml",
            content_workspace._no_symlink_on_the_way(
                self.root, repo_relative("manifests/deployment.yaml")
            ),
        )

    def test_a_symbolic_link_that_stays_inside_the_repository_is_refused_too(self):
        """The case a containment check alone does not see.

        Comparing the *resolved* path against the root catches a link pointing
        out of the tree and nothing else. A link whose target is inside the root
        passes that check and still writes somewhere the name did not say — and
        the target that matters is `.git`, which `repo_relative` refuses by name
        and a symlink reintroduces by reference. Found by mutation: deleting the
        symlink check left every other assertion in this file green.
        """
        (self.root / ".git").mkdir()
        (self.root / "config-link").symlink_to(self.root / ".git")
        (self.root / "manifests" / "alias.yaml").symlink_to(
            self.root / "manifests" / "real.yaml"
        )

        for path in ("config-link/config", "manifests/alias.yaml"):
            with self.subTest(path=path):
                with self.assertRaises(PathRefused):
                    content_workspace._no_symlink_on_the_way(
                        self.root, repo_relative(path)
                    )

        # Paired: the file the link pointed at is writable under its own name.
        self.assertEqual(
            self.root / "manifests" / "real.yaml",
            content_workspace._no_symlink_on_the_way(
                self.root, repo_relative("manifests/real.yaml")
            ),
        )


class DisjointRootsTest(unittest.TestCase):
    """The structural check: the agent must not be able to name the tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_overlapping_roots_refuse_to_arm(self):
        agent = self.base / "opt" / "data"
        agent.mkdir(parents=True)

        # The tree inside the agent's volume: every finding content-passing
        # closes would be open again, and the code would claim otherwise.
        with self.assertRaises(RuntimeError):
            assert_disjoint_roots(agent / "content-workspaces", agent)
        # The agent's volume inside the tree: same property, other direction.
        with self.assertRaises(RuntimeError):
            assert_disjoint_roots(self.base / "opt", agent)
        # Identical.
        with self.assertRaises(RuntimeError):
            assert_disjoint_roots(agent, agent)

        # Paired ordinary use: the shipped layout -- the tree on the broker's
        # own state dir, the workspace on the shared volume -- arms cleanly.
        state = self.base / "var" / "lib" / "credential-proxy"
        state.mkdir(parents=True)
        assert_disjoint_roots(state / "content-workspaces", agent)

    def test_the_store_refuses_to_construct_on_overlapping_roots(self):
        agent = self.base / "data"
        agent.mkdir()
        with self.assertRaises(RuntimeError):
            ContentWorkspaceStore(agent / "trees", agent, RecordingRunner())

        # Paired: the disjoint layout constructs and creates its root.
        state = self.base / "state"
        store = ContentWorkspaceStore(state / "trees", agent, RecordingRunner())
        self.assertTrue(store.tree_root.is_dir())


class ParseChangesTest(unittest.TestCase):
    def test_limits_are_enforced_and_the_whole_payload_is_refused(self):
        big = base64.b64encode(b"x" * (content_workspace.max_file_bytes() + 1)).decode()
        with self.assertRaises(TooLarge):
            parse_changes([{"path": "a.yaml", "contentBase64": big}])

        with mock.patch.object(content_workspace, "max_entries", lambda: 2):
            with self.assertRaises(TooLarge):
                parse_changes(
                    [{"path": f"{n}.yaml", "contentBase64": ""} for n in range(3)]
                )

        with mock.patch.object(content_workspace, "max_total_bytes", lambda: 8):
            with self.assertRaises(TooLarge):
                parse_changes(
                    [
                        {"path": "a.yaml", "contentBase64": base64.b64encode(b"12345").decode()},
                        {"path": "b.yaml", "contentBase64": base64.b64encode(b"12345").decode()},
                    ]
                )

        # Paired ordinary use: a two-file manifest change of ordinary size, and
        # binary content, both parse and survive the encoding intact.
        binary = bytes(range(256))
        changes = parse_changes(
            [
                {
                    "path": "manifests/deployment.yaml",
                    "contentBase64": base64.b64encode(b"kind: Deployment\n").decode(),
                },
                {"path": "assets/logo.png", "contentBase64": base64.b64encode(binary).decode()},
            ]
        )
        self.assertEqual(b"kind: Deployment\n", changes[0].content)
        self.assertEqual(binary, changes[1].content)

    def test_ambiguous_and_duplicated_entries_are_refused(self):
        for payload in (
            [{"path": "a.yaml"}],  # neither content nor a deletion
            [{"path": "a.yaml", "content": "plain"}],  # no plaintext form exists
            [{"path": "a.yaml", "contentBase64": "not base64!"}],
            [{"path": "a.yaml", "contentBase64": "", "delete": True}],
            [
                {"path": "a.yaml", "contentBase64": ""},
                {"path": "a.yaml", "contentBase64": ""},
            ],
            [],
            "not a list",
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ContentWorkspaceError):
                    parse_changes(payload)

        # Paired: the two forms that are defined -- write and delete.
        changes = parse_changes(
            [
                {"path": "a.yaml", "contentBase64": base64.b64encode(b"a").decode()},
                {"path": "b.yaml", "delete": True},
            ]
        )
        self.assertEqual(b"a", changes[0].content)
        self.assertTrue(changes[1].deletes)


class CheckBranchTest(unittest.TestCase):
    def test_a_branch_that_could_be_read_as_an_option_is_refused_first(self):
        # `--upload-pack=<cmd>` names a program git runs. Reaching
        # `check-ref-format` with this string would validate it as a flag.
        for name in ("--upload-pack=/bin/sh", "-x", "--force"):
            with self.subTest(name=name):
                with self.assertRaises(ContentWorkspaceError):
                    check_branch(name)

        for protected in ("main", "master", "production", "MAIN"):
            with self.subTest(protected=protected):
                with self.assertRaises(ContentWorkspaceError):
                    check_branch(protected)

        # Paired ordinary use: the branch names the product actually authors.
        self.assertEqual(
            "platform-agent/provision-mercury-09",
            check_branch("platform-agent/provision-mercury-09"),
        )
        self.assertEqual("fix/cve-2026-1234", check_branch("  fix/cve-2026-1234  "))


class CheckExpectedShaTest(unittest.TestCase):
    """The conflict guard's two caller-supplied revisions.

    Both reach a revision position in `git diff <expected> <current> --
    <paths>`, which the workspace routes get to without passing
    `git_argument_violation`, so this function is the only thing between the
    caller and git's option parser.
    """

    def test_an_option_in_a_revision_position_is_refused(self):
        # --output= is the sharp one: git writes the diff to that path and
        # prints nothing, so the caller's guard reads the empty result as "no
        # overlap" and the conflict it exists to catch never raises.
        for value in ("--output=/opt/data/pwn", "-p", "--exit-code"):
            with self.subTest(value=value):
                with self.assertRaises(ContentWorkspaceError):
                    check_expected_sha(value, "expectedBaseSha")

    def test_a_revision_that_is_not_a_full_object_id_is_refused(self):
        # Each of these resolves to a commit, so git would answer rather than
        # fail — with a comparison against something other than what the caller
        # meant. Wrong quietly is the failure mode this refuses.
        for value in ("HEAD~1", "main", "origin/main", "deadbee", "@"):
            with self.subTest(value=value):
                with self.assertRaises(ContentWorkspaceError):
                    check_expected_sha(value, "expectedBranchSha")

    def test_the_field_name_is_in_the_message(self):
        # Two of these are checked in a row and the payload carries both, so
        # the caller has to be told which one they got wrong.
        with self.assertRaises(ContentWorkspaceError) as raised:
            check_expected_sha("HEAD", "expectedBranchSha")
        self.assertIn("expectedBranchSha", str(raised.exception))

    def test_an_empty_or_non_string_value_is_refused(self):
        for value in ("", "   ", None, 42, ["a" * 40]):
            with self.subTest(value=value):
                with self.assertRaises(ContentWorkspaceError):
                    check_expected_sha(value, "expectedBaseSha")

    def test_both_object_id_lengths_are_accepted(self):
        # A repository is SHA-1 or SHA-256 and the broker clones what it is
        # pointed at, so refusing the longer one would refuse the guard itself
        # on a SHA-256 repository.
        sha1 = "e9ee1c37ab4f5d0c2b8a91f6d3e0c47a5b1d8e92"
        sha256 = "b" * 64
        self.assertEqual(sha1, check_expected_sha(sha1, "expectedBaseSha"))
        self.assertEqual(sha256, check_expected_sha(sha256, "expectedBaseSha"))
        self.assertEqual(
            sha1.upper(), check_expected_sha(f"  {sha1.upper()}  ", "expectedBaseSha")
        )


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.agent = self.base / "data"
        self.agent.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def store(self, runner=None):
        return ContentWorkspaceStore(self.base / "trees", self.agent, runner or RecordingRunner())

    def test_the_remote_url_is_composed_here_and_never_supplied(self):
        runner = RecordingRunner()
        store = self.store(runner)
        # A caller-supplied URL is `url.<host>.insteadOf` by another route: it
        # chooses where the minted GitHub token is sent.
        for repo in (
            "https://attacker.invalid/x/y.git",
            "ext::sh -c id",
            "../../etc",
            "owner",
            "owner/name/extra",
            "owner/na me",
        ):
            with self.subTest(repo=repo):
                with self.assertRaises(ContentWorkspaceError):
                    store.open(repo)
        self.assertEqual([], runner.calls, "nothing should have run")

        # Paired ordinary use: a real repository clones from a URL this module
        # built, not one the caller chose.
        workspace = store.open("acme/fleet")
        clone = runner.calls[0][0]
        self.assertEqual(["git", "clone", "--quiet"], clone[:3])
        self.assertEqual("https://github.com/acme/fleet.git", clone[3])
        self.assertRegex(workspace.handle, r"\A[0-9a-f]{32}\Z")

    def test_a_handle_is_unguessable_and_minted_here(self):
        """What the handle is actually for.

        It is a bearer capability, not an ownership check — the broker cannot
        tell two sessions in the agent container apart, because everything on
        the socket arrives with the same identity. What it does buy is that one
        session cannot *name* another's tree, and that only holds while the
        handle is unpredictable.
        A sequential id would look identical to every other test here.
        """
        store = self.store()
        # Above the workspace ceiling on purpose: the ceiling is a resource
        # control and this test is about entropy, so raise it rather than
        # measure sixteen handles against a limit of eight.
        with mock.patch.object(content_workspace, "max_workspaces", lambda: 64):
            handles = {store.open("acme/fleet").handle for _ in range(16)}
        self.assertEqual(16, len(handles), "handles must not repeat")
        for handle in handles:
            self.assertRegex(handle, r"\A[0-9a-f]{32}\Z")
        # 128 bits of entropy, so no two differ in only their last characters
        # the way a counter would.
        prefixes = {handle[:8] for handle in handles}
        self.assertEqual(16, len(prefixes), "handles must not share a prefix")

    def test_a_clone_that_fails_leaves_no_tree_behind(self):
        """A tree with no handle is a tree `close` can never reach.

        `open` makes the directory before the clone that fills it, so a clone
        that cannot authenticate would otherwise leave one directory per attempt
        on the broker's volume, permanent for the life of the container.
        Measured against a real install before it was fixed: a private
        repository with no credential available left exactly that.
        """
        runner = RecordingRunner({"clone": FakeResult(128, "", "could not read Username")})
        store = self.store(runner)
        with self.assertRaises(content_workspace.GitFailed):
            store.open("acme/fleet")
        self.assertEqual(
            [], list(store.tree_root.iterdir()), "a failed open must leave nothing"
        )
        self.assertEqual({}, store._workspaces)

        # Paired ordinary use: a clone that succeeds keeps its tree, and the
        # handle the store minted reaches it.
        store = self.store()
        workspace = store.open("acme/fleet")
        self.assertEqual([workspace.handle], [p.name for p in store.tree_root.iterdir()])

    def test_an_unknown_or_malformed_handle_is_refused(self):
        store = self.store()
        for handle in ("", "../../etc", "z" * 32, None, 42, "0" * 32):
            with self.subTest(handle=handle):
                with self.assertRaises(NoSuchHandle):
                    store.get(handle)

        # Paired: the handle the store minted resolves to the workspace.
        workspace = store.open("acme/fleet")
        self.assertIs(workspace, store.get(workspace.handle))


class ListAndOpenArgumentTest(unittest.TestCase):
    """Two arguments that looked validated and were not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.agent = self.base / "data"
        self.agent.mkdir()
        self.addCleanup(self.tmp.cleanup)
        self.store = ContentWorkspaceStore(
            self.base / "trees", self.agent, RecordingRunner()
        )
        self.workspace = Workspace(
            handle="f" * 32,
            repo="acme/fleet",
            tree=self.store.tree_root / ("f" * 32) / "repo",
            base="main",
            base_sha="0" * 40,
        )
        for relative in ("a/y.yaml", "ab/x.yaml", "a/deep/z.yaml", "b/w.yaml"):
            target = self.workspace.tree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("kind: ConfigMap\n")
        self.store._workspaces[self.workspace.handle] = self.workspace

    def test_a_list_prefix_names_a_directory_not_a_string(self):
        """`prefix="a"` must not return `ab/x.yaml`.

        A string comparison reads as a filter and behaves as a glob without the
        star, so a caller asking for one directory pages through its siblings.
        """
        paths = [
            entry["path"]
            for entry in self.store.list(self.workspace.handle, "a")["entries"]
        ]
        self.assertEqual(["a/deep/z.yaml", "a/y.yaml"], paths)
        self.assertNotIn("ab/x.yaml", paths)

        # Paired ordinary use: the sibling is reachable under its own name, and
        # no prefix still lists everything.
        self.assertEqual(
            ["ab/x.yaml"],
            [e["path"] for e in self.store.list(self.workspace.handle, "ab")["entries"]],
        )
        self.assertEqual(4, self.store.list(self.workspace.handle)["total"])

    def test_the_base_branch_is_checked_the_way_the_commit_branch_is(self):
        """`base` reached git unvalidated while `branch` went through the check.

        Not reachable as an option today -- every use of it is prefixed with
        `origin/` first -- but the asymmetry is the kind a later caller removes
        without noticing, and a reader comparing the two assumes it is not
        there.
        """
        for name in ("--upload-pack=/bin/sh", "-x", "--force"):
            with self.subTest(name=name):
                with self.assertRaises(ContentWorkspaceError):
                    self.store.open("acme/fleet", name)

        # Paired ordinary use: an ordinary base branch is accepted, and so is a
        # protected one. Reading `main` is not authoring onto it, and basing a
        # workspace on the rollout branch is the normal case -- the write path
        # is where `PROTECTED_BRANCHES` belongs, and this is the read side.
        self.assertEqual(
            "release/2026-08",
            self.store.open("acme/fleet", "release/2026-08").base,
        )
        self.assertEqual("main", self.store.open("acme/fleet", "main").base)
        # And the write path still refuses to author onto it.
        with self.assertRaises(ContentWorkspaceError):
            check_branch("main")


class ConcurrencyTest(unittest.TestCase):
    """Two requests naming one handle, which `ThreadingHTTPServer` produces.

    Every verb here is a read-then-act on a working tree another verb is
    entitled to delete or reset underneath it. Both interleavings below were
    reproduced against the unlocked store before the lock went in; the
    injection is single-threaded on purpose, because it pins the exact
    interleaving rather than hoping a race scheduler finds it.
    """

    def setUp(self):
        # A deadlock here does not fail, it hangs -- and a hang in CI is a job
        # timeout with no test named and nothing pointing at this file. Arm a
        # watchdog for the duration of this class only: if any method in it
        # stops making progress, the process dumps every thread's stack, which
        # names the exact line holding the lock, and dies. Scoped rather than
        # module-level because the rest of the suite runs alongside longer
        # ones and must not inherit a deadline.
        faulthandler.dump_traceback_later(60, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.agent = self.base / "data"
        self.agent.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def store_with_workspace(self):
        # `diff --cached --quiet` exits 1 when something is staged, which is
        # what makes these commits reach the `commit` call at all.
        runner = RecordingRunner({"diff --cached": FakeResult(1)})
        store = ContentWorkspaceStore(self.base / "trees", self.agent, runner)
        workspace = Workspace(
            handle="d" * 32,
            repo="acme/fleet",
            tree=store.tree_root / ("d" * 32) / "repo",
            base="main",
            base_sha="0" * 40,
        )
        workspace.tree.mkdir(parents=True, exist_ok=True)
        store._workspaces[workspace.handle] = workspace
        return store, workspace, runner

    def commit_with(self, store, workspace, interleaved):
        """Run `interleaved` from another thread once the commit is mid-flight.

        The victim point is the first write of the payload loop, i.e. after
        `checkout` and `clean` and before anything is staged.
        """
        arrived = threading.Event()
        finished = threading.Event()
        original = content_workspace._no_symlink_on_the_way

        def once(root, relative):
            if not arrived.is_set():
                arrived.set()
                thread = threading.Thread(
                    target=lambda: (interleaved(), finished.set())
                )
                thread.start()
                # If the lock works, the other verb cannot finish while this
                # commit holds it. A generous wait keeps the assertion about
                # the lock rather than about scheduling.
                thread.join(timeout=2)
                # Snapshot it *here*. Read after the commit returns, this is
                # always true -- the other verb runs the moment the lock is
                # released -- and the test would pass against no lock at all.
                self.finished_during_commit = finished.is_set()
                self.other = thread
            return original(root, relative)

        content_workspace._no_symlink_on_the_way = once
        self.addCleanup(
            setattr, content_workspace, "_no_symlink_on_the_way", original
        )
        try:
            outcome = store.commit(
                workspace.handle,
                "platform-agent/change",
                "feat: a change",
                [Change(repo_relative("manifests/mine.yaml"), b"kind: Mine\n")],
            )
        finally:
            content_workspace._no_symlink_on_the_way = original
        self.other.join(timeout=10)
        return outcome, self.finished_during_commit

    def test_a_close_cannot_land_inside_a_commit(self):
        """Unlocked, this left a tree on disk with no handle pointing at it.

        `close` pops the handle and removes the tree; the commit already past
        `get` then re-creates it with `mkdir(parents=True)` and writes into it.
        Nothing can reach the result afterwards, and the trees live on the
        broker's ephemeral storage -- so a `close`/`commit` loop is node disk
        pressure, through the new surface, with the flag on.
        """
        store, workspace, _ = self.store_with_workspace()
        outcome, finished_during_commit = self.commit_with(
            store, workspace, lambda: store.close(workspace.handle)
        )

        self.assertFalse(
            finished_during_commit,
            "close() completed while a commit held the workspace",
        )
        self.assertTrue(outcome["committed"])
        # close() ran after the commit released, so it removed a tree that was
        # whole. Nothing is registered and nothing is left behind.
        self.assertEqual({}, store._workspaces)
        self.assertEqual(
            [], list(store.tree_root.iterdir()), "a tree was orphaned on disk"
        )

    def test_a_second_commit_cannot_land_inside_the_first(self):
        """Unlocked, the second commit's `clean -fdxq` deleted the first's files.

        The first then either failed on a pathspec that no longer matched or
        landed its commit on the other's branch while reporting its own -- and
        the response still said `committed: true`, so a caller could push a
        branch whose content is not what it sent.
        """
        store, workspace, runner = self.store_with_workspace()

        def other_commit():
            store.commit(
                workspace.handle,
                "platform-agent/theirs",
                "feat: theirs",
                [Change(repo_relative("manifests/theirs.yaml"), b"kind: Theirs\n")],
            )

        outcome, finished_during_commit = self.commit_with(
            store, workspace, other_commit
        )

        self.assertFalse(
            finished_during_commit,
            "a second commit completed while the first held the workspace",
        )
        self.assertTrue(outcome["committed"])
        self.assertEqual("platform-agent/change", outcome["branch"])
        # The two commits ran end to end, so each staged its own path and
        # neither saw the other's checkout.
        staged = [
            argv for argv, _ in runner.calls if argv[1:3] == ["--literal-pathspecs", "add"]
        ]
        self.assertEqual(
            [
                ["git", "--literal-pathspecs", "add", "--", "manifests/mine.yaml"],
                ["git", "--literal-pathspecs", "add", "--", "manifests/theirs.yaml"],
            ],
            staged,
        )

    def test_ordinary_sequential_use_is_not_slowed_to_a_stop(self):
        """Paired ordinary use: the lock is reentrant and does not self-deadlock.

        Every verb takes it and every verb calls `get`, which takes it too, so
        a plain `Lock` deadlocks on the first request.

        Run on a worker with a bounded join rather than inline, because the
        failure mode here is a *hang*, not an exception. Inline, swapping
        `RLock` for `Lock` does not turn this red -- it stops the suite dead,
        which in CI is a job timeout with no failing test named and nothing
        pointing at this file. A deadlock the test cannot report is a test that
        only works when someone is watching.
        """
        store, workspace, _ = self.store_with_workspace()
        done = []

        def sequence():
            done.append(store.get(workspace.handle))
            done.append(store.list(workspace.handle))
            done.append(
                store.commit(
                    workspace.handle,
                    "platform-agent/change",
                    "feat: a change",
                    [Change(repo_relative("a.yaml"), b"a\n")],
                )
            )
            store.close(workspace.handle)
            done.append("closed")

        worker = threading.Thread(target=sequence, daemon=True)
        worker.start()
        worker.join(timeout=20)
        self.assertFalse(
            worker.is_alive(),
            "a verb deadlocked: every verb takes the store lock and every verb "
            "calls get(), which takes it too, so it has to be reentrant",
        )
        self.assertIs(workspace, done[0])
        self.assertEqual([], done[1]["entries"])
        self.assertTrue(done[2]["committed"])
        self.assertEqual("closed", done[3])
        self.assertEqual({}, store._workspaces)


class ResourceCeilingTest(unittest.TestCase):
    """Nothing bounded how much disk the trees could take between them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.agent = self.base / "data"
        self.agent.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def store(self, runner=None):
        return ContentWorkspaceStore(
            self.base / "trees", self.agent, runner or RecordingRunner()
        )

    def test_the_number_of_open_workspaces_is_capped(self):
        store = self.store()
        with mock.patch.object(content_workspace, "max_workspaces", lambda: 3):
            for _ in range(3):
                store.open("acme/fleet")
            with self.assertRaises(TooLarge):
                store.open("acme/fleet")

            # Paired ordinary use: closing one makes room for the next, so the
            # ceiling is a ceiling and not a lifetime quota.
            store.close(next(iter(store._workspaces)))
            self.assertIsNotNone(store.open("acme/fleet"))
        self.assertEqual(3, len(store._workspaces))
        self.assertEqual(3, len(list(store.tree_root.iterdir())))

    def test_a_clone_over_the_byte_ceiling_is_removed_not_kept(self):
        """A repository the broker cannot afford is not one to hold half of."""

        class FatCloneRunner(RecordingRunner):
            def __call__(self, argv, cwd):
                result = super().__call__(argv, cwd)
                if argv[1] == "clone":
                    target = Path(argv[-1])
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "blob.bin").write_bytes(b"x" * 4096)
                return result

        store = self.store(FatCloneRunner())
        with mock.patch.object(content_workspace, "max_clone_bytes", lambda: 1024):
            with self.assertRaises(TooLarge):
                store.open("acme/fleet")
        self.assertEqual({}, store._workspaces)
        self.assertEqual(
            [], list(store.tree_root.iterdir()), "the oversized tree was kept"
        )

        # Paired: under the ceiling the same clone is accepted and stays.
        with mock.patch.object(content_workspace, "max_clone_bytes", lambda: 1 << 20):
            workspace = store.open("acme/fleet")
        self.assertEqual([workspace.handle], [p.name for p in store.tree_root.iterdir()])


class ErrorRedactionTest(unittest.TestCase):
    """The invariant three docstrings assert: no response carries a path.

    `GitFailed` puts git's stderr on the wire, and git quotes absolute paths in
    its errors without being asked. Until this was enforced, the claim was a
    sentence with nothing behind it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.agent = self.base / "data"
        self.agent.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def test_git_stderr_reaches_the_caller_without_the_tree_root_in_it(self):
        handle = "e" * 32
        runner = RecordingRunner()
        store = ContentWorkspaceStore(self.base / "trees", self.agent, runner)
        # Built from the store's own root, because git prints the path it
        # actually used and git resolves. A fixture using the unresolved
        # spelling tests a string the broker never emits.
        runner.responses = {
            "clone": FakeResult(
                128,
                "",
                f"fatal: could not create work tree dir "
                f"'{store.tree_root}/{handle}/repo': Permission denied",
            )
        }

        with self.assertRaises(content_workspace.GitFailed) as caught:
            store.open("acme/fleet")
        message = str(caught.exception)

        self.assertNotIn(str(store.tree_root), message)
        self.assertNotIn(str(self.base), message)
        # Root and tail collapse to one marker rather than leaving a shredded
        # path behind.
        self.assertIn("'<workspace-root>'", message)
        # And no handle survives in any spelling. It is a bearer capability,
        # and the handle whose `open` just failed is not registered yet, so it
        # has to be caught by shape.
        self.assertNotRegex(message, r"[0-9a-f]{32}")

        # Paired: a handle quoted on its own, outside any path, still goes.
        runner.responses = {"clone": FakeResult(128, "", f"fatal: bad object {handle}")}
        with self.assertRaises(content_workspace.GitFailed) as caught:
            store.open("acme/fleet")
        self.assertIn("<handle>", str(caught.exception))
        self.assertNotRegex(str(caught.exception), r"[0-9a-f]{32}")

        # Paired ordinary use: the part of the message that helps a caller
        # debug is still there. Redaction that removes everything is just a
        # worse error.
        self.assertIn("could not create work tree dir", message)
        self.assertIn("Permission denied", message)
        self.assertIn("exit code 128", message)

    def test_the_root_is_resolved_so_a_symlinked_prefix_still_matches(self):
        """git prints the path it used, and git resolves. So must the root.

        Constructed with an unresolved, symlinked spelling -- which is what a
        caller hands over -- while git's stderr names the real path underneath.
        Unresolved, the substitution matches only the tail and the message goes
        out as `/private<workspace-root>/...`. That is how it surfaced: in a
        real run on a machine where the temp directory is behind a symlink.

        It is unreachable in production only because `CommandExecutor` happens
        to resolve the root before handing it over, which makes this module's
        invariant depend on a caller somewhere else. Resolving here is what
        makes it this module's property.
        """
        real = self.base / "real"
        real.mkdir()
        linked = self.base / "linked"
        linked.symlink_to(real)

        runner = RecordingRunner()
        store = ContentWorkspaceStore(linked / "trees", self.agent, runner)
        self.assertEqual(
            (real / "trees").resolve(), store.tree_root, "the root was not resolved"
        )

        # The path git would actually print, from under the symlink.
        runner.responses = {
            "clone": FakeResult(
                128,
                "",
                f"fatal: could not create work tree dir "
                f"'{(real / 'trees').resolve()}/{'e' * 32}/repo': Permission denied",
            )
        }
        with self.assertRaises(content_workspace.GitFailed) as caught:
            store.open("acme/fleet")
        message = str(caught.exception)
        self.assertIn("'<workspace-root>'", message)
        self.assertNotIn(str(real), message)
        self.assertNotIn("/private", message)

    def test_an_absolute_path_that_is_not_the_tree_root_goes_too(self):
        """The claim is "no filesystem path", not "not that one path".

        Knowing only the tree root and the handle grammar left every other
        absolute path in git's stderr on the wire. These two the broker really
        can produce: `$HOME` is deliberately pointed at the broker's own state
        dir, so a config it cannot read names that dir, and a lock failure
        names wherever the config lives.
        """
        leaky = (
            "warning: unable to access '/var/run/broker-state/.gitconfig': "
            "Permission denied\n"
            "error: could not lock config file /tmp/xyz/.git/config: Permission denied"
        )
        runner = RecordingRunner({"clone": FakeResult(128, "", leaky)})
        store = ContentWorkspaceStore(self.base / "trees3", self.agent, runner)

        with self.assertRaises(content_workspace.GitFailed) as caught:
            store.open("acme/fleet")
        message = str(caught.exception)

        self.assertNotIn("/var/run/broker-state", message)
        self.assertNotIn("/tmp/xyz", message)
        self.assertNotIn(".gitconfig", message)
        self.assertEqual(2, message.count("<path>"))

        # Paired: the reason survives, and a ref is not a path -- `origin/main`
        # and `refs/heads/x` are most of what a git error is about and must
        # come through intact.
        self.assertIn("unable to access", message)
        self.assertIn("could not lock config file", message)
        self.assertIn("Permission denied", message)

        refs = RecordingRunner(
            {"clone": FakeResult(1, "", "error: failed to push some refs to origin/main")}
        )
        store = ContentWorkspaceStore(self.base / "trees4", self.agent, refs)
        with self.assertRaises(content_workspace.GitFailed) as caught:
            store.open("acme/fleet")
        self.assertIn("origin/main", str(caught.exception))
        self.assertNotIn("<path>", str(caught.exception))


class IndexUnreadableTest(unittest.TestCase):
    """`git diff --cached --quiet` has three answers, not two.

    0 is "nothing staged", 1 is "something staged", and anything else means the
    index could not be read at all — a missing object store, a corrupt index, a
    failed hook. `audit_report` read every non-zero exit as "already fixed on
    main", logged a reassuring line and opened no pull request. The same mistake
    here would report a commit that never happened.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.agent = self.base / "data"
        self.agent.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def store_with(self, diff_exit_code):
        runner = RecordingRunner({"diff --cached": FakeResult(diff_exit_code)})
        store = ContentWorkspaceStore(self.base / "trees", self.agent, runner)
        workspace = Workspace(
            handle="b" * 32,
            repo="acme/fleet",
            tree=self.base / "trees" / "repo",
            base="main",
            base_sha="0" * 40,
        )
        workspace.tree.mkdir(parents=True, exist_ok=True)
        store._workspaces[workspace.handle] = workspace
        return store, workspace, runner

    def commit(self, store, workspace):
        return store.commit(
            workspace.handle,
            "platform-agent/change",
            "feat: a change",
            [Change(repo_relative("a.yaml"), b"a\n")],
        )

    def test_an_index_that_cannot_be_read_is_an_error_not_an_empty_commit(self):
        for exit_code in (2, 128, 129):
            with self.subTest(exit_code=exit_code):
                store, workspace, runner = self.store_with(exit_code)
                with self.assertRaises(content_workspace.GitFailed):
                    self.commit(store, workspace)
                self.assertNotIn(
                    "commit", runner.subcommands, "nothing should have been committed"
                )

        # Paired ordinary use: the two answers that do mean something.
        store, workspace, runner = self.store_with(1)
        self.assertTrue(self.commit(store, workspace)["committed"])
        self.assertIn("commit", runner.subcommands)

        store, workspace, runner = self.store_with(0)
        self.assertFalse(self.commit(store, workspace)["committed"])
        self.assertNotIn("commit", runner.subcommands)


@unittest.skipUnless(git_available(), "git is not installed")
class RealGitTest(unittest.TestCase):
    """The commit path against real git, in a real tree.

    `open` is bypassed deliberately: it composes an https URL by design, and a
    test-only escape hatch for that would be a hole in the control the test
    above exists to prove. Constructing the `Workspace` directly exercises
    everything after the clone, which is where the interesting behaviour is.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.remote = self.base / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(self.remote)],
            capture_output=True,
            check=True,
        )
        seed = self.base / "seed"
        real_git_runner(["git", "clone", str(self.remote), str(seed)], self.base)
        (seed / "manifests").mkdir()
        (seed / "manifests" / "existing.yaml").write_text("kind: ConfigMap\n")
        real_git_runner(["git", "add", "-A"], seed)
        real_git_runner(["git", "commit", "-m", "seed"], seed)
        real_git_runner(["git", "push", "origin", "main"], seed)

        self.agent = self.base / "data"
        self.agent.mkdir()
        self.store = ContentWorkspaceStore(self.base / "trees", self.agent, real_git_runner)
        self.tree = self.base / "trees" / "work" / "repo"
        self.tree.parent.mkdir(parents=True)
        real_git_runner(["git", "clone", str(self.remote), str(self.tree)], self.base)
        self.workspace = Workspace(
            handle="a" * 32,
            repo="acme/fleet",
            tree=self.tree,
            base="main",
            base_sha=real_git_runner(
                ["git", "rev-parse", "--verify", "origin/main"], self.tree
            ).stdout.strip(),
        )
        self.store._workspaces[self.workspace.handle] = self.workspace

    def commit(self, changes, **kwargs):
        return self.store.commit(
            self.workspace.handle, "platform-agent/change", "feat: a change", changes, **kwargs
        )

    def test_a_commit_lands_the_bytes_and_nothing_else(self):
        result = self.commit(
            [
                Change(repo_relative("manifests/new.yaml"), b"kind: Deployment\n"),
                Change(repo_relative("manifests/existing.yaml"), None),
            ]
        )
        self.assertTrue(result["committed"])
        self.assertEqual("platform-agent/change", result["branch"])

        listed = real_git_runner(
            ["git", "show", "--name-status", "--format=", "HEAD"], self.tree
        ).stdout
        self.assertIn("A\tmanifests/new.yaml", listed)
        self.assertIn("D\tmanifests/existing.yaml", listed)

        content = real_git_runner(
            ["git", "show", "HEAD:manifests/new.yaml"], self.tree
        ).stdout
        self.assertEqual("kind: Deployment\n", content)

    def test_an_empty_change_is_reported_rather_than_committed(self):
        # The bytes already on the base: there is nothing to propose, and that
        # is a fact to report, not a failure to raise.
        existing = (self.tree / "manifests" / "existing.yaml").read_bytes()
        result = self.commit([Change(repo_relative("manifests/existing.yaml"), existing)])
        self.assertFalse(result["committed"])

        # Paired: one byte different and it is a commit.
        result = self.commit(
            [Change(repo_relative("manifests/existing.yaml"), existing + b"# changed\n")]
        )
        self.assertTrue(result["committed"])

    def test_a_base_that_moved_under_the_same_file_is_a_conflict(self):
        opened_at = self.workspace.base_sha
        # Somebody else changes the same file on the base branch.
        seed = self.base / "seed"
        (seed / "manifests" / "existing.yaml").write_text("kind: ConfigMap  # theirs\n")
        real_git_runner(["git", "add", "-A"], seed)
        real_git_runner(["git", "commit", "-m", "theirs"], seed)
        real_git_runner(["git", "push", "origin", "main"], seed)

        with self.assertRaises(Conflict) as caught:
            self.commit(
                [Change(repo_relative("manifests/existing.yaml"), b"kind: ConfigMap  # ours\n")],
                expected_base_sha=opened_at,
            )
        self.assertIn("existing.yaml", str(caught.exception))

        # Paired ordinary use: the base moving under a file this commit does
        # *not* write is not a conflict. Refusing that would fail every commit
        # that raced any unrelated merge, which is most of them.
        result = self.commit(
            [Change(repo_relative("manifests/unrelated.yaml"), b"kind: Service\n")],
            expected_base_sha=opened_at,
        )
        self.assertTrue(result["committed"])

    def test_a_working_branch_that_moved_under_the_same_file_is_a_conflict(self):
        """The base check does not answer this one.

        A second round of review starts from `origin/<branch>`, so a
        maintainer's commit stays in the history -- but their edit to a file
        this payload also writes is overwritten, and `--force-with-lease`
        cannot object because it compares against the tip being overwritten.
        """
        branch = "platform-agent/change"
        seed = self.base / "seed"
        real_git_runner(["git", "checkout", "-b", branch], seed)
        (seed / "manifests" / "proposal.yaml").write_text("kind: Deployment\n")
        real_git_runner(["git", "add", "-A"], seed)
        real_git_runner(["git", "commit", "-m", "ours, round one"], seed)
        real_git_runner(["git", "push", "origin", branch], seed)

        # What `open` records when it checks the branch out.
        real_git_runner(["git", "fetch", "--prune", "origin"], self.tree)
        self.workspace.opened_branch = branch
        self.workspace.branch_sha = real_git_runner(
            ["git", "rev-parse", "--verify", f"origin/{branch}"], self.tree
        ).stdout.strip()

        # A maintainer hand-edits the proposal on the pull request.
        (seed / "manifests" / "proposal.yaml").write_text("kind: Deployment  # theirs\n")
        real_git_runner(["git", "add", "-A"], seed)
        real_git_runner(["git", "commit", "-m", "theirs"], seed)
        real_git_runner(["git", "push", "origin", branch], seed)

        with self.assertRaises(Conflict) as caught:
            self.commit(
                [Change(repo_relative("manifests/proposal.yaml"), b"kind: Deployment  # ours\n")]
            )
        self.assertIn("proposal.yaml", str(caught.exception))
        self.assertIn(branch, str(caught.exception))

        # Paired ordinary use: the branch moving under a file this commit does
        # not write costs the agent nothing, and the maintainer's commit is
        # still there afterwards.
        result = self.commit(
            [Change(repo_relative("manifests/another.yaml"), b"kind: Service\n")]
        )
        self.assertTrue(result["committed"])
        self.assertEqual(
            "kind: Deployment  # theirs\n",
            real_git_runner(
                ["git", "show", "HEAD:manifests/proposal.yaml"], self.tree
            ).stdout,
        )
        # And the workspace now leases what it just saw, so the next round
        # compares against the right sha rather than the sha `open` recorded.
        self.assertEqual(result["branchSha"], self.workspace.branch_sha)

    def test_a_read_returns_content_and_a_list_never_shows_the_git_directory(self):
        self.assertEqual(
            b"kind: ConfigMap\n",
            self.store.read(self.workspace.handle, "manifests/existing.yaml"),
        )
        with self.assertRaises(PathRefused):
            self.store.read(self.workspace.handle, ".git/config")

        paths = [
            entry["path"]
            for entry in self.store.list(self.workspace.handle)["entries"]
        ]
        self.assertIn("manifests/existing.yaml", paths)
        self.assertFalse([path for path in paths if path.startswith(".git/")])

    def test_a_payload_over_the_limit_writes_nothing_at_all(self):
        """Fail closed means before the side effects, not after some of them."""
        before = (self.tree / "manifests" / "existing.yaml").read_bytes()
        with mock.patch.object(content_workspace, "max_file_bytes", lambda: 4):
            with self.assertRaises(TooLarge):
                parse_changes(
                    [
                        {
                            "path": "manifests/existing.yaml",
                            "contentBase64": base64.b64encode(b"ok").decode(),
                        },
                        {
                            "path": "manifests/huge.yaml",
                            "contentBase64": base64.b64encode(b"far too long").decode(),
                        },
                    ]
                )
        self.assertEqual(before, (self.tree / "manifests" / "existing.yaml").read_bytes())
        self.assertFalse((self.tree / "manifests" / "huge.yaml").exists())

        # Paired: under the limit, both files land.
        changes = parse_changes(
            [
                {"path": "manifests/a.yaml", "contentBase64": base64.b64encode(b"a").decode()},
                {"path": "manifests/b.yaml", "contentBase64": base64.b64encode(b"b").decode()},
            ]
        )
        self.assertTrue(self.commit(changes)["committed"])
        self.assertEqual(b"a", (self.tree / "manifests" / "a.yaml").read_bytes())

    def seed_a_symlink_into_the_base(self, name, target):
        """Commit a symlink onto the base branch and refresh the workspace.

        Committed, not merely written. `commit` does `checkout --force` and
        `clean -fdxq` before it applies anything, so a symlink only written
        into the working tree is gone by the time the write loop runs and the
        test would pass for the wrong reason -- the same fixture trap as
        attributes that have to live in the merge base.
        """
        seed = self.base / "seed"
        real_git_runner(["git", "rm", "-r", "--cached", "-q", "--ignore-unmatch", name], seed)
        path = seed / name
        if path.is_dir() and not path.is_symlink():
            for child in sorted(path.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
            path.rmdir()
        path.symlink_to(target)
        real_git_runner(["git", "add", "-A"], seed)
        real_git_runner(["git", "commit", "-m", "symlink"], seed)
        real_git_runner(["git", "push", "origin", "main"], seed)
        real_git_runner(["git", "fetch", "--quiet", "origin"], self.tree)
        real_git_runner(["git", "checkout", "--force", "-B", "main", "origin/main"], self.tree)
        # The fixture is only worth anything if git really restored a symlink.
        self.assertTrue(
            (self.tree / name).is_symlink(), "fixture did not produce a symlink"
        )

    def test_a_commit_refuses_to_write_through_a_symlink_in_the_base(self):
        """The symlink check, reached the way a request reaches it.

        The unit tests above call `_no_symlink_on_the_way` directly, which
        proves the function and not the wiring. Bypassing the call at both of
        its sites left every one of those tests green, so this one goes through
        `store.commit` instead.
        """
        outside = self.base / "outside"
        outside.mkdir()
        self.seed_a_symlink_into_the_base("manifests", outside)

        with self.assertRaises(PathRefused):
            self.commit([Change(repo_relative("manifests/pwned.yaml"), b"kind: Pwned\n")])
        self.assertFalse(
            (outside / "pwned.yaml").exists(), "the write escaped through the link"
        )

        # Paired ordinary use: a path that does not cross the link still commits.
        self.assertTrue(
            self.commit([Change(repo_relative("charts/values.yaml"), b"replicas: 1\n")])[
                "committed"
            ]
        )

    def test_a_read_refuses_to_follow_a_symlink_in_the_base(self):
        """Same wiring question on the read path, which has its own call site."""
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.yaml").write_text("kind: Secret\n")
        self.seed_a_symlink_into_the_base("manifests", outside)

        with self.assertRaises(PathRefused):
            self.store.read(self.workspace.handle, "manifests/secret.yaml")

        # Paired: an ordinary tracked file still reads.
        (self.tree / "plain.yaml").write_bytes(b"kind: ConfigMap\n")
        self.assertEqual(
            b"kind: ConfigMap\n", self.store.read(self.workspace.handle, "plain.yaml")
        )

    def test_a_commit_cannot_write_the_repository_configuration(self):
        """Repo-local config as code execution, refused where it can be."""
        # Distinctive enough that finding it proves the payload landed. A single
        # character would not: the repository path itself is in `.git/config`,
        # so a one-byte needle matches whatever the temporary directory is
        # called and the test passes or fails by luck.
        payload = b"[url]\n\tinsteadOf = PAYLOAD-c0ffee\n"
        for path in (".git/config", ".git/hooks/pre-commit"):
            with self.subTest(path=path):
                with self.assertRaises(PathRefused):
                    parse_changes(
                        [{"path": path, "contentBase64": base64.b64encode(payload).decode()}]
                    )
                self.assertNotIn(
                    b"PAYLOAD-c0ffee", (self.tree / ".git" / "config").read_bytes()
                )
        self.assertFalse((self.tree / ".git" / "hooks" / "pre-commit").exists())

        # Paired ordinary use: a file whose name begins the same way is content
        # like any other, and it commits.
        self.assertTrue(
            self.commit([Change(repo_relative(".gitignore"), b"*.tmp\n")])["committed"]
        )


class ReadVerbCeilingTest(unittest.TestCase):
    """The batched read and the paged listing, which answer partially by design.

    Both report what they left out. A caller that cannot tell a complete answer
    from a truncated one materialises part of a repository, does not find what
    it wanted, and goes on to `read` paths it invented -- which is the failure
    the ceilings are there to make visible rather than the one they cause.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.agent = self.base / "data"
        self.agent.mkdir()
        self.addCleanup(self.tmp.cleanup)
        self.store = ContentWorkspaceStore(
            self.base / "trees", self.agent, RecordingRunner()
        )
        self.workspace = Workspace(
            handle="e" * 32,
            repo="acme/fleet",
            tree=self.store.tree_root / ("e" * 32) / "repo",
            base="main",
            base_sha="0" * 40,
        )
        for index in range(5):
            target = self.workspace.tree / "manifests" / f"{index}.yaml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("kind: ConfigMap\n")
        self.store._workspaces[self.workspace.handle] = self.workspace

    def handle(self) -> str:
        return self.workspace.handle

    def test_a_batch_read_names_what_it_did_not_return(self):
        with mock.patch.object(content_workspace, "max_total_bytes", lambda: 20):
            answer = self.store.read_many(
                self.handle(),
                ["manifests/0.yaml", "manifests/1.yaml", "manifests/2.yaml"],
            )
        # 16 bytes each, so the second exhausts the budget and the third is
        # named rather than dropped -- `requestBudget` means ask again.
        self.assertEqual(["manifests/0.yaml"], [f["path"] for f in answer["files"]])
        self.assertEqual(
            [("manifests/1.yaml", "requestBudget"), ("manifests/2.yaml", "requestBudget")],
            [(s["path"], s["reason"]) for s in answer["skipped"]],
        )

        # A file that is not there is skipped, not fatal, and one over the
        # per-file ceiling reports its size so a caller stops asking for it.
        with mock.patch.object(content_workspace, "max_file_bytes", lambda: 4):
            answer = self.store.read_many(
                self.handle(), ["manifests/0.yaml", "manifests/absent.yaml"]
            )
        self.assertEqual([], answer["files"])
        self.assertEqual(
            [("manifests/0.yaml", "tooLarge"), ("manifests/absent.yaml", "notAFile")],
            [(s["path"], s["reason"]) for s in answer["skipped"]],
        )

        # Paired ordinary use: within the ceilings every file comes back, as
        # bytes, in the order it was asked for.
        answer = self.store.read_many(
            self.handle(), ["manifests/1.yaml", "manifests/0.yaml"]
        )
        self.assertEqual([], answer["skipped"])
        self.assertEqual(
            ["manifests/1.yaml", "manifests/0.yaml"],
            [f["path"] for f in answer["files"]],
        )
        self.assertEqual(
            b"kind: ConfigMap\n",
            base64.b64decode(answer["files"][0]["contentBase64"]),
        )

    def test_a_link_the_broker_will_not_follow_says_so(self):
        """A symlink is not a missing file, and the skip has to say which.

        The caller only ever names what `list` and `grep` returned, so both of
        these are names the repository itself supplied. Told `notAFile` about
        one of them, a reader concludes the name is wrong and goes looking for
        the right one -- there isn't one. `symlink` is the only skip reason it
        can act on: ask for the target under its own name.
        """
        link = self.workspace.tree / "manifests" / "vendored.yaml"
        link.symlink_to(self.workspace.tree / "manifests" / "0.yaml")

        answer = self.store.read_many(
            self.handle(), ["manifests/vendored.yaml", "manifests/absent.yaml"]
        )
        self.assertEqual([], answer["files"])
        self.assertEqual(
            [("manifests/vendored.yaml", "symlink"), ("manifests/absent.yaml", "notAFile")],
            [(s["path"], s["reason"]) for s in answer["skipped"]],
        )

        # A directory on the way is the same refusal, and reports the same way:
        # nothing here is a claim about the leaf alone.
        (self.workspace.tree / "vendor").mkdir()
        (self.workspace.tree / "vendor" / "app.yaml").write_text("kind: Pod\n")
        (self.workspace.tree / "manifests" / "vendor").symlink_to(
            self.workspace.tree / "vendor"
        )
        answer = self.store.read_many(self.handle(), ["manifests/vendor/app.yaml"])
        self.assertEqual(
            [("manifests/vendor/app.yaml", "symlink")],
            [(s["path"], s["reason"]) for s in answer["skipped"]],
        )

        # Paired ordinary use: the target under its own name is served, which is
        # what the reason is telling the caller to do.
        self.assertEqual(
            ["vendor/app.yaml"],
            [f["path"] for f in self.store.read_many(self.handle(), ["vendor/app.yaml"])["files"]],
        )

    def test_a_batch_read_refuses_the_whole_request_for_one_bad_path(self):
        """`.git/config` in the last entry does not get the others answered.

        The same fail-before-side-effects rule `parse_changes` follows. A path
        that is not a path is the caller being wrong about what it may name,
        which is not the same class of thing as a file that is missing.
        """
        with self.assertRaises(PathRefused):
            self.store.read_many(
                self.handle(), ["manifests/0.yaml", ".git/config"]
            )
        with self.assertRaises(PathRefused):
            self.store.read_many(self.handle(), ["../etc/passwd"])
        for bad in ([], "manifests/0.yaml", None):
            with self.subTest(paths=bad):
                with self.assertRaises(ContentWorkspaceError):
                    self.store.read_many(self.handle(), bad)
        with mock.patch.object(content_workspace, "max_entries", lambda: 1):
            with self.assertRaises(TooLarge):
                self.store.read_many(
                    self.handle(), ["manifests/0.yaml", "manifests/1.yaml"]
                )

        # Paired ordinary use: the same call with only nameable paths is served.
        self.assertEqual(
            2,
            len(
                self.store.read_many(
                    self.handle(), ["manifests/0.yaml", "manifests/1.yaml"]
                )["files"]
            ),
        )

    def test_a_truncated_listing_says_so_and_pages_from_its_last_entry(self):
        with mock.patch.object(content_workspace, "max_entries", lambda: 2):
            first = self.store.list(self.handle())
            self.assertTrue(first["truncated"])
            self.assertEqual(5, first["total"])
            self.assertEqual(
                ["manifests/0.yaml", "manifests/1.yaml"],
                [e["path"] for e in first["entries"]],
            )

            # The cursor is the last path of the page before, and the next page
            # starts strictly after it -- no repeat, no gap.
            second = self.store.list(
                self.handle(), after=first["entries"][-1]["path"]
            )
            self.assertEqual(
                ["manifests/2.yaml", "manifests/3.yaml"],
                [e["path"] for e in second["entries"]],
            )
            self.assertEqual(3, second["total"])

            last = self.store.list(
                self.handle(), after=second["entries"][-1]["path"]
            )
            self.assertEqual(["manifests/4.yaml"], [e["path"] for e in last["entries"]])
            self.assertFalse(last["truncated"])

            # A cursor need not name a file that is still there. The page
            # before named it and the tree can have moved on, so `after` is a
            # position in the order rather than a lookup: a name that has gone
            # resumes where it would have been instead of restarting the
            # listing or returning nothing.
            resumed = self.store.list(self.handle(), after="manifests/2a.yaml")
            self.assertEqual(
                ["manifests/3.yaml", "manifests/4.yaml"],
                [e["path"] for e in resumed["entries"]],
            )
            self.assertEqual(2, resumed["total"])

        # Paired ordinary use: under the ceiling one page is the whole answer
        # and it does not claim to be truncated.
        whole = self.store.list(self.handle())
        self.assertFalse(whole["truncated"])
        self.assertEqual(5, len(whole["entries"]))

        # And the cursor is a path like any other, so it is validated like one.
        with self.assertRaises(PathRefused):
            self.store.list(self.handle(), after="../etc/passwd")


class ShallowAndBranchOpenTest(unittest.TestCase):
    """`open`'s two read-side arguments, and what each one commits the caller to."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.agent = self.base / "data"
        self.agent.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def store(self, responses=None):
        self.runner = RecordingRunner(responses)
        return ContentWorkspaceStore(self.base / "trees", self.agent, self.runner)

    def test_a_depth_is_a_commit_count_and_not_a_yes(self):
        store = self.store()
        for depth in (True, False, 0, -1, "1", 1.0):
            with self.subTest(depth=depth):
                with self.assertRaises(ContentWorkspaceError):
                    store.open("acme/fleet", None, None, depth)
        # Refused together with `branch`: a single-branch clone cannot see
        # whether the working branch exists, so the check would answer no and
        # the caller would be told it had looked.
        with self.assertRaises(ContentWorkspaceError):
            store.open("acme/fleet", None, "platform-agent/fix", 1)

        # Paired ordinary use: a count reaches git as a shallow single-branch
        # clone, and the workspace says so.
        workspace = store.open("acme/fleet", "main", None, 5)
        self.assertTrue(workspace.shallow)
        clone = next(argv for argv, _ in self.runner.calls if argv[1] == "clone")
        self.assertEqual(
            ["git", "clone", "--quiet", "--depth", "5", "--single-branch", "--branch", "main"],
            clone[:8],
        )
        # And a full clone still is one.
        before = len(self.runner.calls)
        self.assertFalse(store.open("acme/fleet").shallow)
        full = next(
            argv for argv, _ in self.runner.calls[before:] if argv[1] == "clone"
        )
        self.assertNotIn("--depth", full)

    def test_a_shallow_workspace_refuses_to_author(self):
        """Refused at `commit` rather than discovered at `push`.

        A shallow clone shares no merge base with the remote branch, so the push
        is either rejected as unrelated or, on a remote configured to take it,
        lands a history that discards everything before the depth.
        """
        store = self.store()
        shallow = store.open("acme/fleet", "main", None, 1)
        with self.assertRaises(ContentWorkspaceError):
            store.commit(
                shallow.handle,
                "platform-agent/change",
                "feat: a change",
                [Change(repo_relative("a.yaml"), b"a\n")],
            )

        # Paired ordinary use: the same commit on a full clone gets as far as
        # git, which is all this fake runner can show.
        full = store.open("acme/fleet", "main")
        result = store.commit(
            full.handle,
            "platform-agent/change",
            "feat: a change",
            [Change(repo_relative("a.yaml"), b"a\n")],
        )
        self.assertEqual("platform-agent/change", result["branch"])
        self.assertIn("checkout", self.runner.subcommands)

    def test_a_branch_that_exists_is_what_reads_answer_from(self):
        """Second-round feedback is written against the pull request, not the base.

        Left on the base, a re-read would return the file as `main` has it, the
        caller would patch that, and the first round's reviewed work would be
        silently rewritten out of the commit.
        """
        store = self.store()
        workspace = store.open("acme/fleet", "main", "platform-agent/fix")
        self.assertEqual("origin/platform-agent/fix", workspace.started_from)
        checkout = next(argv for argv, _ in self.runner.calls if argv[1] == "checkout")
        self.assertEqual(
            ["checkout", "--force", "-B", "platform-agent/fix", "origin/platform-agent/fix"],
            checkout[1:],
        )

        # Paired ordinary use: the first round names the same branch, the remote
        # does not have it yet, and that is not an error -- the workspace opens
        # on the base and says so.
        absent = self.store({"refs/remotes/origin/": FakeResult(exit_code=1)})
        first = absent.open("acme/fleet", "main", "platform-agent/fix")
        self.assertEqual("origin/main", first.started_from)
        self.assertNotIn("checkout", self.runner.subcommands)

        # And the branch is a name in an argv like any other.
        with self.assertRaises(ContentWorkspaceError):
            store.open("acme/fleet", "main", "--upload-pack=/bin/sh")


class GrepTest(unittest.TestCase):
    """`git grep` over a real tree, because the argv is the whole control."""

    def setUp(self):
        if not git_available():
            self.skipTest("git is not available")
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.agent = self.base / "data"
        self.agent.mkdir()
        self.store = ContentWorkspaceStore(
            self.base / "trees", self.agent, real_git_runner
        )
        self.tree = self.base / "trees" / "work" / "repo"
        (self.tree / "manifests").mkdir(parents=True)
        real_git_runner(["git", "init", "--quiet", "--initial-branch=main", "."], self.tree)
        (self.tree / "manifests" / "app.yaml").write_text(
            "kind: Service\nreplicas: 2\n"
        )
        (self.tree / "other.yaml").write_text("kind: Service\n")
        real_git_runner(["git", "add", "-A"], self.tree)
        real_git_runner(["git", "commit", "-m", "seed"], self.tree)
        self.workspace = Workspace(
            handle="d" * 32,
            repo="acme/fleet",
            tree=self.tree,
            base="main",
            base_sha="0" * 40,
        )
        self.store._workspaces[self.workspace.handle] = self.workspace

    def test_a_pattern_is_never_read_as_an_option_or_reaches_the_git_directory(self):
        handle = self.workspace.handle
        # `.git` is not in scope however the pattern is written: git grep
        # searches tracked files, and git does not track its own directory.
        # `core.repositoryformatversion` is in every `.git/config` there is.
        self.assertEqual(
            0, self.store.grep(handle, "repositoryformatversion")["total"]
        )
        # A pattern beginning with a dash is a pattern. `-O<file>` is the pager
        # vector the executor's own allowlist tests cover from the other side.
        self.assertEqual(0, self.store.grep(handle, "-O/tmp/payload.sh")["total"])
        # Fixed-string unless asked: a regular expression given by accident
        # matches itself rather than more than the caller meant.
        self.assertEqual(0, self.store.grep(handle, "kind: S.rvice")["total"])
        for bad in ("", "   ", None, 7, "two\nlines"):
            with self.subTest(pattern=bad):
                with self.assertRaises(ContentWorkspaceError):
                    self.store.grep(handle, bad)
        # An expression git will not parse is a 400 about the expression, and
        # git's stderr -- which quotes the tree's path -- is not in it.
        with self.assertRaises(ContentWorkspaceError) as refused:
            self.store.grep(handle, "a[", regex=True)
        self.assertNotIn(str(self.base), str(refused.exception))

        # Paired ordinary use: the search a reader actually runs.
        answer = self.store.grep(handle, "kind: Service")
        self.assertEqual(
            [("manifests/app.yaml", 1), ("other.yaml", 1)],
            sorted((m["path"], m["line"]) for m in answer["matches"]),
        )
        self.assertFalse(answer["truncated"])
        # The prefix narrows it, the regex flag turns the expression on, and
        # ignoreCase does what it says.
        self.assertEqual(
            1, self.store.grep(handle, "kind: Service", "manifests")["total"]
        )
        self.assertEqual(2, self.store.grep(handle, "kind: S.rvice", regex=True)["total"])
        self.assertEqual(2, self.store.grep(handle, "KIND: SERVICE", ignore_case=True)["total"])

    def test_a_search_that_hit_the_ceiling_does_not_look_complete(self):
        handle = self.workspace.handle
        with mock.patch.object(content_workspace, "max_matches", lambda: 1):
            answer = self.store.grep(handle, "kind: Service")
        self.assertEqual(1, len(answer["matches"]))
        self.assertEqual(2, answer["total"])
        self.assertTrue(answer["truncated"])

        # A long line is cut at the width and the match says it was.
        with mock.patch.object(content_workspace, "max_match_chars", lambda: 4):
            match = self.store.grep(handle, "replicas")["matches"][0]
        self.assertEqual("repl", match["text"])
        self.assertTrue(match["truncated"])

        # Paired ordinary use: under both ceilings nothing claims to be cut.
        answer = self.store.grep(handle, "replicas")
        self.assertFalse(answer["truncated"])
        self.assertNotIn("truncated", answer["matches"][0])
        self.assertEqual("replicas: 2", answer["matches"][0]["text"])


if __name__ == "__main__":
    unittest.main()
