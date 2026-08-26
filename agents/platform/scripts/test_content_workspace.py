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
            entry["path"] for entry in self.store.list(self.workspace.handle, "a")
        ]
        self.assertEqual(["a/deep/z.yaml", "a/y.yaml"], paths)
        self.assertNotIn("ab/x.yaml", paths)

        # Paired ordinary use: the sibling is reachable under its own name, and
        # no prefix still lists everything.
        self.assertEqual(
            ["ab/x.yaml"],
            [e["path"] for e in self.store.list(self.workspace.handle, "ab")],
        )
        self.assertEqual(4, len(self.store.list(self.workspace.handle)))

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
        self.assertEqual([], done[1])
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

    def test_a_read_returns_content_and_a_list_never_shows_the_git_directory(self):
        self.assertEqual(
            b"kind: ConfigMap\n",
            self.store.read(self.workspace.handle, "manifests/existing.yaml"),
        )
        with self.assertRaises(PathRefused):
            self.store.read(self.workspace.handle, ".git/config")

        paths = [entry["path"] for entry in self.store.list(self.workspace.handle)]
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


if __name__ == "__main__":
    unittest.main()
