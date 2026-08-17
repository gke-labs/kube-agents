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
import os
import subprocess
import tempfile
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
        thing it is protecting -- the defect class in D15.
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
        tell two sessions in the agent container apart, because `Principal` is
        per-ServiceAccount. What it does buy is that one session cannot *name*
        another's tree, and that only holds while the handle is unpredictable.
        A sequential id would look identical to every other test here.
        """
        store = self.store()
        handles = {store.open("acme/fleet").handle for _ in range(16)}
        self.assertEqual(16, len(handles), "handles must not repeat")
        for handle in handles:
            self.assertRegex(handle, r"\A[0-9a-f]{32}\Z")
        # 128 bits of entropy, so no two differ in only their last characters
        # the way a counter would.
        prefixes = {handle[:8] for handle in handles}
        self.assertEqual(16, len(prefixes), "handles must not share a prefix")

    def test_an_unknown_or_malformed_handle_is_refused(self):
        store = self.store()
        for handle in ("", "../../etc", "z" * 32, None, 42, "0" * 32):
            with self.subTest(handle=handle):
                with self.assertRaises(NoSuchHandle):
                    store.get(handle)

        # Paired: the handle the store minted resolves to the workspace.
        workspace = store.open("acme/fleet")
        self.assertIs(workspace, store.get(workspace.handle))


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

    def test_a_commit_cannot_write_the_repository_configuration(self):
        """The vector D16 exists to close, at the layer that has to refuse it."""
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
