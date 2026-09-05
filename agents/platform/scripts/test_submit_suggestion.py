"""Unit tests for the submit-suggestion skill's PR submitter.

The subject lives in `agents/platform/skills/submit-suggestion/scripts/`, but
the test lives here: CI discovers tests in exactly two directories
(.github/workflows/python-tests.yml), and that is not one of them. Loading it by
path keeps the coverage without a third discovery step.

Run:
  python3 -m unittest discover -s agents/platform/scripts -p 'test_submit_suggestion.py' -v

Real git against a local bare repository throughout. A recorded runner would
make most of this vacuous — `--force-with-lease` either refuses a diverged
remote or it does not, and only a real push can tell you which.
"""

import dataclasses
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import content_workspace  # noqa: E402
import credential_proxy  # noqa: E402
import credential_proxy_client  # noqa: E402


@dataclasses.dataclass
class GitResult:
    """What the broker's `_git` reads off a run: an exit code, not a returncode."""

    exit_code: int
    stdout: str
    stderr: str
import gitops_workspace  # noqa: E402

SUBJECT = (
    HERE.parent
    / "skills"
    / "submit-suggestion"
    / "scripts"
    / "submit_suggestion.py"
)


def _load_subject():
    spec = importlib.util.spec_from_file_location("submit_suggestion", SUBJECT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


submit_suggestion = _load_subject()


def git(argv, cwd):
    return subprocess.run(
        ["git", *argv], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@unittest.skipIf(shutil.which("git") is None, "git is not on PATH")
class SubmitSuggestionTestCase(unittest.TestCase):
    """A real origin, a real clone, and `gh` stubbed at the module boundary."""

    def patch_attr(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.root = self.tmp_path / "gitops"
        self.origin = self.seed_origin()

        # The token broker is a live sidecar in the pod and irrelevant here.
        self.patch_attr(
            submit_suggestion, "refresh_git_credentials", lambda repo=None: "t"
        )
        # Progress chatter on stderr, useful in the pod, noise in a test run.
        self.patch_attr(submit_suggestion, "log", lambda msg: None)

        # Everything resolves to the local bare repo instead of github.com.
        real_resolve = gitops_workspace.resolve_repo

        def local_resolve(workspace=None):
            if workspace is not None:
                return real_resolve(workspace=workspace)
            return "acme/fleet"

        self.patch_attr(gitops_workspace, "resolve_repo", local_resolve)
        self.patch_attr(
            gitops_workspace,
            "get_managed_github_repos",
            lambda: ["acme/fleet", "acme/secondary-repo"],
        )
        # The broker's write gate caches this list in a module global with a
        # five-minute TTL, so without the reset a value another test warmed
        # would decide the gate rather than the stub above.
        credential_proxy._managed_repository_cache = None
        self.addCleanup(
            setattr, credential_proxy, "_managed_repository_cache", None
        )
        real_ensure = gitops_workspace.ensure_workspace

        def local_ensure(repo, runner, **kwargs):
            kwargs.setdefault("root", self.root)
            kwargs["remote_url"] = str(self.origin)
            return real_ensure(repo, runner, **kwargs)

        self.patch_attr(gitops_workspace, "ensure_workspace", local_ensure)

        env = patch.dict(
            os.environ,
            {
                "HERMES_KANBAN_TASK": "t_card",
                "HERMES_SESSION_ID": "",
                "GITOPS_BASE_BRANCH": "",
                # Directory mode, explicitly. `prepare` asks the broker whether
                # it has content workspaces armed, and an inherited
                # CREDENTIAL_PROXY_URL from the developer's shell would send
                # that question somewhere real.
                "CREDENTIAL_PROXY_URL": "",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        # Memoised per clone, and these clones live at per-test temp paths.
        gitops_workspace.forget_base_branch()
        self.addCleanup(gitops_workspace.forget_base_branch)

        # `gh pr create` needs a GitHub. Record the call instead.
        self.gh_calls = []
        self.patch_attr(submit_suggestion, "subprocess", _GhStub(self))

    def seed_origin(self) -> Path:
        origin = self.tmp_path / "origin.git"
        seed = self.tmp_path / "seed"
        seed.mkdir()
        for cmd in (
            ["git", "init", "--quiet", "--bare", "--initial-branch=main", str(origin)],
            ["git", "init", "--quiet", "--initial-branch=main", str(seed)],
        ):
            subprocess.run(cmd, check=True, capture_output=True)
        (seed / "README.md").write_text("seed\n", encoding="utf-8")
        for argv in (
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "T"],
            ["add", "README.md"],
            ["commit", "--quiet", "-m", "seed"],
            ["remote", "add", "origin", str(origin)],
            ["push", "--quiet", "origin", "main"],
        ):
            git(argv, seed)
        return origin

    # -- helpers ---------------------------------------------------------- #

    def prepare(self, branch="platform-agent/fix-netpol", lease=None, repo=None):
        args = ["prepare", "--branch", branch]
        if lease:
            args += ["--lease", lease]
        if repo:
            args += ["--repo", repo]
        out = io.StringIO()
        with redirect_stdout(out):
            submit_suggestion.dispatch(args)
        return json.loads(out.getvalue())

    def submit(self, branch, workspace, lease=None, title="t", body="b"):
        args = [
            "submit",
            "--branch", branch,
            "--title", title,
            "--body", body,
            "--workspace", str(workspace),
            "--repo", "acme/fleet",
        ]
        if lease:
            args += ["--lease", lease]
        out = io.StringIO()
        with redirect_stdout(out):
            submit_suggestion.dispatch(args)
        return out.getvalue().strip()

    def commit(self, workspace, name="clusters/prod/netpol.yaml"):
        path = Path(workspace) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("kind: NetworkPolicy\n", encoding="utf-8")
        git(["add", name], workspace)
        git(["commit", "--quiet", "-m", "add netpol"], workspace)

    def origin_git(self, argv):
        """Inspect the bare origin.

        `--git-dir` rather than `cwd`: a developer with
        `safe.bareRepository = explicit` in their global config — the hardening
        git ships as a recommendation — gets exit 128 for the cwd form.
        """
        out = subprocess.run(
            ["git", f"--git-dir={self.origin}", *argv],
            check=True, capture_output=True, text=True,
        )
        return out.stdout

    def origin_branches(self):
        return self.origin_git(["branch", "--format=%(refname:short)"]).split()


# --------------------------------------------------------------------------- #
# Branch names the skill must never touch
# --------------------------------------------------------------------------- #


class TestCheckBranch(unittest.TestCase):
    def test_a_protected_branch_is_refused(self):
        for branch in ("main", "master", "production", "MAIN", " main "):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError) as caught:
                    submit_suggestion.check_branch(branch)
                self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))

    def test_a_protected_branch_is_refused_through_a_ref_prefix(self):
        # "refs/heads/main".lower() is not in PROTECTED_BRANCHES, but pushing
        # it moves main all the same. The check must compare the short name,
        # whatever form the caller used.
        for branch in ("refs/heads/main", "refs/heads/MASTER", "REFS/HEADS/production"):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError) as caught:
                    submit_suggestion.check_branch(branch)
                self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))

    def test_an_empty_branch_is_refused_before_the_protected_list(self):
        # `"".lower() in PROTECTED_BRANCHES` is False, so an empty name would
        # otherwise sail through and push whatever HEAD happens to be.
        for branch in ("", "   ", None):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError) as caught:
                    submit_suggestion.check_branch(branch)
                self.assertIn("must not be empty", str(caught.exception))

    def test_a_suggestion_branch_passes_and_is_trimmed(self):
        self.assertEqual(
            submit_suggestion.check_branch("  platform-agent/fix  "),
            "platform-agent/fix",
        )


class TestValidateRepo(unittest.TestCase):
    def test_invalid_format_raises(self):
        for bad in ("", "foo", "foo/bar/baz", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as caught:
                    submit_suggestion.validate_repo(bad)
                self.assertIn("Invalid repository format", str(caught.exception))

    def test_unmanaged_repo_raises_when_managed_repos_configured(self):
        with patch.object(gitops_workspace, "get_managed_github_repos", return_value=["acme/managed"]):
            with self.assertRaises(ValueError) as caught:
                submit_suggestion.validate_repo("acme/unmanaged")
            self.assertIn("not in the managed repositories list", str(caught.exception))

    def test_managed_repo_passes(self):
        with patch.object(gitops_workspace, "get_managed_github_repos", return_value=["acme/managed"]):
            self.assertEqual(submit_suggestion.validate_repo("acme/managed"), "acme/managed")

    def test_passes_when_no_managed_repos_configured(self):
        with patch.object(gitops_workspace, "get_managed_github_repos", return_value=[]):
            self.assertEqual(submit_suggestion.validate_repo("acme/any"), "acme/any")

    def test_raises_when_get_managed_github_repos_fails(self):
        with patch.object(
            gitops_workspace,
            "get_managed_github_repos",
            side_effect=RuntimeError("kubectl failed: Forbidden"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                submit_suggestion.validate_repo("acme/any")
            self.assertIn("kubectl failed: Forbidden", str(caught.exception))


# --------------------------------------------------------------------------- #
# prepare — leasing a private clone
# --------------------------------------------------------------------------- #


class TestPrepare(SubmitSuggestionTestCase):
    def test_it_hands_back_a_leased_workspace_on_the_new_branch(self):
        payload = self.prepare()
        workspace = Path(payload["workspace"])
        self.assertEqual(payload["lease"], "t_card")
        self.assertEqual(payload["repo"], "acme/fleet")
        self.assertEqual(workspace, self.root / "t_card" / "acme__fleet")
        self.assertTrue((workspace / ".git").is_dir())
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(workspace), check=True, capture_output=True, text=True,
        )
        self.assertEqual(head.stdout.strip(), "platform-agent/fix-netpol")

    def test_the_lease_marker_names_this_skill(self):
        # So an operator staring at /opt/data/gitops can tell which tree belongs
        # to an audit and which to a suggestion.
        payload = self.prepare()
        record = gitops_workspace.read_lease(Path(payload["workspace"]).parent)
        self.assertEqual(record["owner"], submit_suggestion.OWNER)
        self.assertEqual(record["lease"], "t_card")

    def test_two_cards_get_two_trees(self):
        # The incident in one assertion: before leases both of these resolved to
        # the same directory and the second `checkout -B` moved the first card's
        # HEAD out from under it.
        first = self.prepare(branch="platform-agent/one", lease="t_one")
        second = self.prepare(branch="platform-agent/two", lease="t_two")
        self.assertNotEqual(first["workspace"], second["workspace"])
        for payload, branch in (
            (first, "platform-agent/one"),
            (second, "platform-agent/two"),
        ):
            head = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=payload["workspace"], check=True, capture_output=True, text=True,
            )
            self.assertEqual(head.stdout.strip(), branch)

    def test_a_protected_branch_never_reaches_the_clone(self):
        with self.assertRaises(ValueError):
            self.prepare(branch="main")
        self.assertFalse((self.root / "t_card").exists())

    def test_a_retried_card_reuses_its_branch_rather_than_failing(self):
        # `-B`, not `-b`. A card that comes back from review runs prepare again.
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(first["workspace"], second["workspace"])
        self.assertEqual(second["branch"], "platform-agent/fix-netpol")

    def test_a_retry_starts_from_a_clean_tree(self):
        payload = self.prepare()
        stray = Path(payload["workspace"]) / "half-written.yaml"
        stray.write_text("...\n", encoding="utf-8")
        self.prepare()
        self.assertFalse(stray.exists())

    def test_prepare_refused_when_managed_repos_read_fails(self):
        with patch.object(
            gitops_workspace,
            "get_managed_github_repos",
            side_effect=RuntimeError("ConfigMap missing"),
        ):
            with self.assertRaises(RuntimeError):
                self.prepare()


# --------------------------------------------------------------------------- #
# submit — pushing only what we own
# --------------------------------------------------------------------------- #


class TestSubmit(SubmitSuggestionTestCase):
    def test_prepare_then_submit_lands_the_branch_and_opens_the_pr(self):
        payload = self.prepare()
        self.commit(payload["workspace"])
        url = self.submit(payload["branch"], payload["workspace"])

        self.assertEqual(url, "https://github.com/acme/fleet/pull/1")
        self.assertIn("platform-agent/fix-netpol", self.origin_branches())
        self.assertEqual(len(self.gh_calls), 1)
        argv, cwd = self.gh_calls[0]
        self.assertEqual(argv[:3], ["gh", "pr", "create"])
        self.assertEqual(cwd, payload["workspace"])
        self.assertIn("--repo", argv)
        self.assertEqual(argv[argv.index("--repo") + 1], "acme/fleet")
        self.assertEqual(argv[argv.index("--head") + 1], "platform-agent/fix-netpol")
        self.assertEqual(argv[argv.index("--base") + 1], "main")

    def test_submit_inherits_target_repo_from_prepare_lease(self):
        """When prepare leases a non-default repo, submit without --repo uses it."""
        payload = self.prepare(
            branch="platform-agent/secondary-fix", repo="acme/secondary-repo"
        )
        self.commit(payload["workspace"])

        args = [
            "submit",
            "--branch", "platform-agent/secondary-fix",
            "--title", "fix",
            "--body", "details",
            "--workspace", payload["workspace"],
            "--lease", payload["lease"],
        ]
        out = io.StringIO()
        with redirect_stdout(out):
            submit_suggestion.dispatch(args)

        argv, _ = self.gh_calls[0]
        self.assertEqual(argv[argv.index("--repo") + 1], "acme/secondary-repo")

    def test_another_agents_workspace_is_refused(self):
        # The check the credential proxy cannot make. The audit's tree holds a
        # perfectly valid `.lease` — just not ours.
        theirs = self.prepare(branch="platform-agent/theirs", lease="compliance-audit")
        with self.assertRaises(PermissionError) as caught:
            self.submit("platform-agent/theirs", theirs["workspace"], lease="t_card")
        message = str(caught.exception)
        self.assertIn("compliance-audit", message)
        self.assertIn("t_card", message)
        self.assertEqual(self.gh_calls, [])
        self.assertNotIn("platform-agent/theirs", self.origin_branches())

    def test_an_unleased_directory_is_refused(self):
        # An agent that skipped `prepare` and ran the skill from its profile.
        loose = self.tmp_path / "profile"
        loose.mkdir()
        with self.assertRaises(PermissionError):
            self.submit("platform-agent/fix-netpol", loose)
        self.assertEqual(self.gh_calls, [])

    def test_submitting_the_wrong_branch_is_refused(self):
        payload = self.prepare()
        self.commit(payload["workspace"])
        with self.assertRaises(ValueError) as caught:
            self.submit("platform-agent/some-other", payload["workspace"])
        self.assertIn("platform-agent/fix-netpol", str(caught.exception))
        self.assertEqual(self.gh_calls, [])

    def test_a_protected_branch_is_refused_before_anything_runs(self):
        payload = self.prepare()
        with self.assertRaises(ValueError) as caught:
            self.submit("main", payload["workspace"])
        self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))
        self.assertEqual(self.gh_calls, [])

    def test_a_second_round_of_review_feedback_keeps_the_first_round(self):
        """Step 5, and the data loss it used to cause.

        `prepare --branch <headRefName>` against an open pull request's branch
        used to reset that branch onto `origin/main` and force-push, so round
        two did not amend the pull request — it replaced every reviewed commit
        with one that no longer contained them. `--force-with-lease` could not
        object either: the clone had just fetched the very ref it would have
        compared against.
        """
        payload = self.prepare()
        self.commit(payload["workspace"])
        self.submit(payload["branch"], payload["workspace"])

        again = self.prepare()
        self.assertEqual(again["started_from"], "origin/platform-agent/fix-netpol")
        self.commit(again["workspace"], name="clusters/prod/netpol-v2.yaml")
        self.submit(again["branch"], again["workspace"])

        files = self.origin_git(
            ["ls-tree", "-r", "--name-only", "platform-agent/fix-netpol"]
        ).split()
        self.assertIn("clusters/prod/netpol-v2.yaml", files)
        self.assertIn(
            "clusters/prod/netpol.yaml", files, "round one's reviewed work was erased"
        )

    def test_a_brand_new_branch_still_starts_from_the_base(self):
        # The other half of the same rule: only an existing remote branch is
        # continued. A new one must not inherit whatever a same-named branch
        # once held, and must carry no commits but the base's.
        payload = self.prepare(branch="platform-agent/brand-new")
        self.assertEqual(payload["started_from"], "origin/main")
        self.assertEqual(payload["base"], "main")

    def test_resubmitting_returns_the_open_pr_instead_of_failing(self):
        """`gh pr create` refuses after the push has already landed.

        Reported as a failure, that is the worst possible shape: the branch is
        updated and the reviewer sees the new commits, but the skill says the
        submission failed — so the agent retries, pushes again, and fails
        again, for as many rounds of feedback as the pull request gets.
        """
        payload = self.prepare()
        self.commit(payload["workspace"])
        first = self.submit(payload["branch"], payload["workspace"], title="round one")

        again = self.prepare()
        self.commit(again["workspace"], name="clusters/prod/netpol-v2.yaml")
        second = self.submit(again["branch"], again["workspace"], title="round two")

        self.assertEqual(second, first)
        verbs = [argv[1:3] for argv, _ in self.gh_calls]
        self.assertEqual(
            verbs,
            [["pr", "create"], ["pr", "create"], ["pr", "edit"], ["pr", "view"]],
        )
        # Refreshed, not merely located: the title and body describe the
        # commits just pushed, and the old ones are no longer on the branch.
        stub = submit_suggestion.subprocess
        self.assertEqual(stub.titles["platform-agent/fix-netpol"], "round two")

    def test_a_gh_failure_that_is_not_an_existing_pr_still_raises(self):
        # The fallback must not swallow "not authenticated" or "base branch is
        # protected" — those are real failures and the run has to stop.
        payload = self.prepare()
        self.commit(payload["workspace"])

        stub = submit_suggestion.subprocess
        original = stub._create
        stub._create = lambda argv: subprocess.CompletedProcess(
            argv, 1, "", "HTTP 403: Resource not accessible by integration\n"
        )
        self.addCleanup(setattr, stub, "_create", original)

        with self.assertRaises(subprocess.CalledProcessError):
            self.submit(payload["branch"], payload["workspace"])

    def test_submit_without_a_lease_or_a_session_says_what_to_pass(self):
        """The unrecoverable loop, turned into one line of instruction.

        Outside a kanban card neither HERMES_KANBAN_TASK nor HERMES_SESSION_ID
        is set, and `lease_id` would mint `adhoc-<random>` — a different random
        string in `submit`'s process than in `prepare`'s. The ownership check
        would then refuse the workspace `prepare` had just handed over, and
        would refuse it identically however many times the agent retried.
        """
        payload = self.prepare(lease="t_explicit")
        self.commit(payload["workspace"])
        with patch.dict(
            os.environ, {"HERMES_KANBAN_TASK": "", "HERMES_SESSION_ID": ""}
        ):
            with self.assertRaises(ValueError) as caught:
                self.submit(payload["branch"], payload["workspace"])
        self.assertIn("--lease", str(caught.exception))
        # And the documented round-trip works.
        with patch.dict(
            os.environ, {"HERMES_KANBAN_TASK": "", "HERMES_SESSION_ID": ""}
        ):
            url = self.submit(
                payload["branch"], payload["workspace"], lease=payload["lease"]
            )
        self.assertTrue(url.startswith("https://github.com/acme/fleet/pull/"))

    def test_someone_elses_branch_of_the_same_name_is_not_destroyed(self):
        """`--force-with-lease`, and the reason it replaced a blind `-f`.

        Two cards, two leases, the same branch name. The second one's remote ref
        moved after its clone fetched it, so the push must abort rather than
        throw away work that is not ours.
        """
        mine = self.prepare(branch="platform-agent/shared", lease="t_mine")
        self.commit(mine["workspace"])

        theirs = self.prepare(branch="platform-agent/shared", lease="t_theirs")
        self.commit(theirs["workspace"], name="clusters/prod/theirs.yaml")
        self.submit("platform-agent/shared", theirs["workspace"], lease="t_theirs")

        with self.assertRaises(subprocess.CalledProcessError):
            self.submit("platform-agent/shared", mine["workspace"], lease="t_mine")

        survivor = self.origin_git(
            ["show", "--stat", "--format=", "platform-agent/shared"]
        )
        self.assertIn("theirs.yaml", survivor)

    def test_the_push_asks_for_a_lease_not_a_bare_force(self):
        seen = []
        real = gitops_workspace.run_git

        def record(argv, cwd, *, check=True):
            seen.append(list(argv))
            return real(argv, cwd, check=check)

        payload = self.prepare()
        self.commit(payload["workspace"])
        with patch.object(gitops_workspace, "run_git", record):
            self.submit(payload["branch"], payload["workspace"])

        push = next(a for a in seen if a and a[0] == "push")
        self.assertIn("--force-with-lease", push)
        self.assertNotIn("-f", push)
        self.assertNotIn("--force", push)

    def test_every_git_call_names_a_working_directory(self):
        # An unstated cwd is not "the obvious one": the credential proxy runs
        # the real git in the sidecar's filesystem at whatever this process
        # reports, and the sidecar's default holds no lease.
        seen = []
        real = gitops_workspace.run_git

        def record(argv, cwd, *, check=True):
            seen.append(cwd)
            return real(argv, cwd, check=check)

        payload = self.prepare()
        self.commit(payload["workspace"])
        with patch.object(gitops_workspace, "run_git", record):
            self.submit(payload["branch"], payload["workspace"])

        self.assertTrue(seen)
        for cwd in seen:
            self.assertTrue(str(cwd).strip(), "a git call ran with no cwd")

    def test_submit_refused_when_managed_repos_read_fails(self):
        payload = self.prepare()
        self.commit(payload["workspace"])
        with patch.object(
            gitops_workspace,
            "get_managed_github_repos",
            side_effect=RuntimeError("ConfigMap missing"),
        ):
            with self.assertRaises(RuntimeError):
                self.submit(payload["branch"], payload["workspace"])


# --------------------------------------------------------------------------- #
# The pre-`prepare` call shape
# --------------------------------------------------------------------------- #


class TestArgvCompatibility(unittest.TestCase):
    def test_a_bare_flag_form_is_read_as_submit(self):
        # A session already mid-flight when this ships must not die on
        # "invalid choice".
        self.assertEqual(
            submit_suggestion.normalise_argv(["--branch", "b", "--title", "t"]),
            ["submit", "--branch", "b", "--title", "t"],
        )

    def test_an_explicit_command_is_left_alone(self):
        for command in submit_suggestion.COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(
                    submit_suggestion.normalise_argv([command, "--branch", "b"]),
                    [command, "--branch", "b"],
                )

    def test_help_still_describes_the_whole_script(self):
        for argv in ([], ["-h"], ["--help"]):
            with self.subTest(argv=argv):
                self.assertEqual(submit_suggestion.normalise_argv(argv), argv)


# --------------------------------------------------------------------------- #
# Content mode — the broker owns the checkout
# --------------------------------------------------------------------------- #


@unittest.skipIf(shutil.which("git") is None, "git is not on PATH")
class TestContentMode(SubmitSuggestionTestCase):
    """The same two commands, against a real broker-side store.

    The store is the real `ContentWorkspaceStore` rather than a recording of it,
    for the reason the module docstring gives about `--force-with-lease`: the
    properties worth testing here — that the agent never receives a path, that a
    moved base is refused — are properties of what git does, and a stub would
    assert only that this test agrees with itself.

    What is stubbed is the HTTP hop, at `_workspace_call`. It raises the same
    two exception types the real transport raises for the same two conditions,
    which is what the skill branches on.
    """

    def setUp(self):
        super().setUp()
        origin = self.origin
        # `open` composes https://github.com/<owner>/<name>.git itself and takes
        # no caller-supplied URL, by design — so the redirect to the local bare
        # repo goes in at the runner, below the code under test.
        url = "https://github.com/acme/fleet.git"

        # The identity the real executor injects for every `git` it runs, from
        # the product's own defaults rather than a name invented here. Without
        # it this runner inherits whatever ~/.gitconfig the machine happens to
        # have, so `commit` passes on a developer's laptop and exits 128 on a CI
        # runner that has no global identity — the test would be measuring the
        # machine rather than the code.
        identity = {
            "GIT_AUTHOR_NAME": credential_proxy.DEFAULT_GIT_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": credential_proxy.DEFAULT_GIT_AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": credential_proxy.DEFAULT_GIT_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": credential_proxy.DEFAULT_GIT_AUTHOR_EMAIL,
        }

        def runner(argv, cwd):
            argv = [str(origin) if token == url else token for token in argv]
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env={**os.environ, **identity},
            )
            return GitResult(completed.returncode, completed.stdout, completed.stderr)

        self.store = content_workspace.ContentWorkspaceStore(
            self.tmp_path / "broker" / "trees",
            self.tmp_path / "agent-workspace",
            runner,
        )
        self.verbs = []

        # Routed through the broker's own handler rather than straight at the
        # store, so the translation from payload to arguments is under test too.
        route = credential_proxy.CredentialProxyHandler._workspace_route
        store = self.store

        class Router:
            workspaces = store

        def call(endpoint, verb, payload):
            self.verbs.append(verb)
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

        self.patch_attr(credential_proxy_client, "_workspace_call", call)
        endpoint = patch.dict(
            os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"}
        )
        endpoint.start()
        self.addCleanup(endpoint.stop)

    def scratch(self, files: dict) -> Path:
        directory = self.tmp_path / "scratch"
        for name, text in files.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return directory

    def prepare_content(self, branch="platform-agent/fix-netpol", repo=None):
        argv = ["prepare", "--branch", branch]
        if repo:
            argv += ["--repo", repo]
        out = io.StringIO()
        with redirect_stdout(out):
            submit_suggestion.dispatch(argv)
        return json.loads(out.getvalue())

    def submit_content(
        self, prepared, source, title="t", body="b", deletes=(), repo="acme/fleet"
    ):
        argv = [
            "submit",
            "--branch", prepared["branch"],
            "--title", title,
            "--body", body,
            "--handle", prepared["handle"],
            "--from", str(source),
            "--base-sha", prepared["baseSha"],
            "--repo", repo,
        ]
        for path in deletes:
            argv += ["--delete", path]
        out = io.StringIO()
        with redirect_stdout(out):
            submit_suggestion.dispatch(argv)
        return out.getvalue().strip()

    def test_prepare_hands_back_a_handle_and_no_path(self):
        payload = self.prepare_content()
        self.assertEqual(payload["mode"], "content")
        self.assertEqual(payload["repo"], "acme/fleet")
        self.assertEqual(payload["base"], "main")
        self.assertEqual(len(payload["handle"]), 32)
        # The whole point. A path in this JSON is a directory the agent can be
        # told to `cd` into, and `.git` is inside it.
        self.assertNotIn("workspace", payload)
        self.assertNotIn("lease", payload)
        for value in payload.values():
            self.assertNotIn("/tmp", str(value))
            self.assertFalse(str(value).startswith("/"))

    def test_prepare_opens_the_repository_the_flag_names(self):
        """`--repo` decides the repository in both modes or in neither.

        Content mode read the default instead, so a fleet whose cards target
        more than one GitOps repository had every suggestion opened against
        whichever one `resolve_repo` answered with -- under a flag naming
        another.
        """
        self.patch_attr(gitops_workspace, "resolve_repo", lambda workspace=None: "acme/secondary-repo")
        payload = self.prepare_content(repo="acme/fleet")
        self.assertEqual("acme/fleet", payload["repo"])

    def test_submit_refuses_a_repository_outside_the_allowlist(self):
        """The allowlist is checked before the credential is minted, not after.

        Content-mode `submit` resolved `--repo` and went straight to
        `refresh_git_credentials`, so argv the model controls named a repository
        the operator never allowed, an installation token was minted for it, and
        `gh pr create --repo` opened a pull request against it. Directory mode
        and content-mode `prepare` both check; this path did not.
        """
        prepared = self.prepare_content()
        source = self.scratch({"clusters/prod/netpol.yaml": "kind: NetworkPolicy\n"})

        minted = []
        self.patch_attr(
            submit_suggestion,
            "refresh_git_credentials",
            lambda repo=None: minted.append(repo) or "t",
        )

        with self.assertRaises(ValueError) as caught:
            self.submit_content(prepared, source, repo="acme/not-managed")
        self.assertIn("not in the managed repositories list", str(caught.exception))
        # The refusal has to land before the token exists. Minting first and
        # failing afterwards still hands out a credential scoped to a repository
        # nobody allowed.
        self.assertEqual([], minted)

    def test_prepare_then_submit_lands_the_branch_and_opens_the_pr(self):
        prepared = self.prepare_content()
        source = self.scratch({"clusters/prod/netpol.yaml": "kind: NetworkPolicy\n"})
        url = self.submit_content(prepared, source)

        self.assertEqual(url, "https://github.com/acme/fleet/pull/1")
        self.assertIn("platform-agent/fix-netpol", self.origin_branches())
        files = self.origin_git(
            ["ls-tree", "-r", "--name-only", "platform-agent/fix-netpol"]
        ).split()
        self.assertIn("clusters/prod/netpol.yaml", files)
        self.assertEqual(
            self.verbs, ["open", "open", "commit", "push", "close"]
        )  # the first `open` is the availability probe

    def test_the_pull_request_body_travels_on_stdin(self):
        prepared = self.prepare_content()
        source = self.scratch({"a.yaml": "kind: X\n"})
        self.submit_content(prepared, source, body="a body\nover two lines\n")
        argv, cwd = self.gh_calls[0]
        self.assertEqual(argv[argv.index("--body-file") + 1], "-")
        self.assertNotIn("--body", argv)
        # No cwd either: in content mode there is no directory in this container
        # for `gh` to run in, and it does not need one — every call names --repo.
        self.assertIsNone(cwd)

    def test_a_delete_reaches_the_commit(self):
        prepared = self.prepare_content()
        source = self.scratch({"a.yaml": "kind: X\n"})
        self.submit_content(prepared, source, deletes=["README.md"])
        files = self.origin_git(
            ["ls-tree", "-r", "--name-only", "platform-agent/fix-netpol"]
        ).split()
        self.assertNotIn("README.md", files)

    def test_a_symlink_in_the_scratch_directory_is_not_followed(self):
        """A link resolves against the *agent's* filesystem.

        Following one would read whatever it points at into a commit — the
        agent's own token file included — while every string in the request
        still looked like ordinary repository-relative content.
        """
        secret = self.tmp_path / "token"
        secret.write_text("ghs_not_yours\n", encoding="utf-8")
        source = self.scratch({"a.yaml": "kind: X\n"})
        (source / "stolen.txt").symlink_to(secret)

        changes = submit_suggestion.collect_changes(str(source), None)
        self.assertEqual(list(changes), ["a.yaml"])

    def test_submit_refuses_a_handle_with_nothing_to_send(self):
        prepared = self.prepare_content()
        with self.assertRaises(ValueError) as caught:
            submit_suggestion.dispatch([
                "submit",
                "--branch", prepared["branch"],
                "--title", "t", "--body", "b",
                "--handle", prepared["handle"],
            ])
        self.assertIn("--from is required", str(caught.exception))
        self.assertEqual(self.gh_calls, [])

    def test_a_protected_branch_is_refused_before_the_broker_is_touched(self):
        prepared = self.prepare_content()
        self.verbs.clear()
        source = self.scratch({"a.yaml": "kind: X\n"})
        with self.assertRaises(ValueError) as caught:
            self.submit_content({**prepared, "branch": "main"}, source)
        self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))
        self.assertEqual(self.verbs, [])

    def test_a_base_that_moved_under_the_same_file_is_refused(self):
        """`--base-sha` is what makes a ten-minute suggestion safe to land."""
        prepared = self.prepare_content()
        # Someone else lands a change to the same file on main meanwhile.
        seed = self.tmp_path / "meanwhile"
        subprocess.run(
            ["git", "clone", "--quiet", str(self.origin), str(seed)],
            check=True, capture_output=True,
        )
        (seed / "clusters/prod").mkdir(parents=True)
        (seed / "clusters/prod/netpol.yaml").write_text("kind: Other\n", encoding="utf-8")
        for argv in (
            ["config", "user.email", "o@example.com"],
            ["config", "user.name", "O"],
            ["add", "-A"],
            ["commit", "--quiet", "-m", "theirs"],
            ["push", "--quiet", "origin", "main"],
        ):
            git(argv, seed)

        source = self.scratch({"clusters/prod/netpol.yaml": "kind: NetworkPolicy\n"})
        with self.assertRaises(credential_proxy_client.WorkspaceRequestError) as caught:
            self.submit_content(prepared, source)
        self.assertEqual(caught.exception.status, 409)
        # The broker names the colliding files in the refusal itself rather than
        # in a field of its own, so a caller that only prints the message still
        # tells its reader which file to re-read.
        self.assertIn(
            "clusters/prod/netpol.yaml", caught.exception.payload["message"]
        )
        self.assertEqual(self.gh_calls, [])

    def test_a_failed_submit_still_releases_the_brokers_clone(self):
        """The sidecar's disk is not something a retry loop may fill.

        Every failure between `open` and the pull request used to leave a
        checkout and a handle on the broker with nothing left that could name
        them -- and the card that failed is the one an operator retries.
        """
        prepared = self.prepare_content()
        source = self.scratch({"a.yaml": "kind: X\n"})
        self.submit_content(prepared, source)
        self.verbs.clear()

        # The same bytes again: refused as a duplicate, mid-session.
        again = self.prepare_content()
        with self.assertRaises(ValueError):
            self.submit_content(again, source)
        self.assertIn("close", self.verbs)
        self.assertEqual({}, self.store._workspaces)

    def test_a_second_round_of_review_feedback_keeps_the_first_round(self):
        """The data loss directory mode already shipped once, in the new path.

        `commit` used to check out `origin/<base>` unconditionally, so round two
        replaced every reviewed commit with one that no longer contained them —
        and `--force-with-lease` could not object, because `commit` fetches the
        very ref the lease compares against moments earlier.
        """
        first = self.prepare_content()
        self.assertEqual(first["started_from"], "origin/main")
        self.submit_content(first, self.scratch({"a.yaml": "kind: X\n"}))

        second = self.prepare_content()
        self.assertEqual(second["started_from"], "origin/platform-agent/fix-netpol")
        self.submit_content(second, self.scratch({"b.yaml": "kind: Y\n"}))

        files = self.origin_git(
            ["ls-tree", "-r", "--name-only", "platform-agent/fix-netpol"]
        ).split()
        self.assertIn("b.yaml", files)
        self.assertIn("a.yaml", files, "round one's reviewed work was erased")

    def test_a_reopened_workspace_reads_the_branch_not_the_base(self):
        """Otherwise round two edits the file as `main` has it.

        Same failure as above by another route: the content is right on disk but
        the agent was shown the wrong starting text, so its edit writes the
        reviewed change back out of the file.
        """
        first = self.prepare_content()
        self.submit_content(first, self.scratch({"README.md": "seed\nround one\n"}))

        second = self.prepare_content()
        scratch = self.tmp_path / "again"
        with redirect_stdout(io.StringIO()):
            submit_suggestion.dispatch([
                "fetch",
                "--handle", second["handle"],
                "--path", "README.md",
                "--to", str(scratch),
                "--repo", "acme/fleet",
            ])
        self.assertEqual(
            (scratch / "README.md").read_text(encoding="utf-8"), "seed\nround one\n"
        )

    def test_list_and_fetch_are_how_an_existing_file_gets_edited(self):
        """There is no `cat` that reaches the checkout, so the skill carries one.

        Without this the agent's only way to change an existing file would be to
        rewrite it from memory.
        """
        prepared = self.prepare_content()
        out = io.StringIO()
        with redirect_stdout(out):
            submit_suggestion.dispatch(
                ["list", "--handle", prepared["handle"], "--repo", "acme/fleet"]
            )
        self.assertEqual(
            [entry["path"] for entry in json.loads(out.getvalue())["entries"]],
            ["README.md"],
        )

        scratch = self.tmp_path / "scratch"
        out = io.StringIO()
        with redirect_stdout(out):
            submit_suggestion.dispatch([
                "fetch",
                "--handle", prepared["handle"],
                "--path", "README.md",
                "--to", str(scratch),
                "--repo", "acme/fleet",
            ])
        self.assertEqual((scratch / "README.md").read_text(encoding="utf-8"), "seed\n")

        (scratch / "README.md").write_text("seed\nand a line\n", encoding="utf-8")
        self.submit_content(prepared, scratch)
        landed = self.origin_git(
            ["show", "platform-agent/fix-netpol:README.md"]
        )
        self.assertEqual(landed, "seed\nand a line\n")

    def test_fetch_will_not_write_outside_the_scratch_directory(self):
        # `--path` is repository-relative and the broker validates it, so the
        # traversal is refused before anything is read rather than being
        # normalised into something that lands somewhere unexpected here.
        prepared = self.prepare_content()
        scratch = self.tmp_path / "scratch"
        with self.assertRaises(credential_proxy_client.WorkspaceRequestError):
            submit_suggestion.dispatch([
                "fetch",
                "--handle", prepared["handle"],
                "--path", "../../etc/passwd",
                "--to", str(scratch),
                "--repo", "acme/fleet",
            ])
        self.assertFalse(scratch.exists())

    def test_a_missing_broker_falls_back_to_the_directory_path(self):
        """Both mechanisms are live at once, and the agent asks rather than assumes."""

        def unavailable(endpoint, verb, payload):
            raise credential_proxy_client.WorkspaceUnavailable("not enabled")

        with patch.object(credential_proxy_client, "_workspace_call", unavailable):
            payload = self.prepare_content()
        self.assertEqual(payload["mode"], "directory")
        self.assertIn("workspace", payload)


class _GhStub:
    """Stand in for the `subprocess` module inside submit_suggestion.

    Only `gh` is intercepted — it is the one tool that needs a real GitHub.
    Everything else is handed to the real module so the git in these tests
    stays real.

    It models one behaviour beyond recording, because that behaviour is the
    bug: GitHub refuses a second `pr create` for a branch that already has an
    open pull request, and the refusal arrives *after* the push has landed. A
    stub that always succeeded made the resubmission path untestable, which is
    how the skill shipped exiting 1 on every round of review feedback.
    """

    def __init__(self, test):
        self._test = test
        self.open_prs: dict[str, str] = {}
        self.titles: dict[str, str] = {}

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, cmd, **kwargs):
        argv = list(cmd)
        if argv[:1] != ["gh"]:
            return subprocess.run(cmd, **kwargs)
        self._test.gh_calls.append((argv, kwargs.get("cwd")))
        verb = argv[1:3]
        if verb == ["pr", "create"]:
            return self._create(argv)
        if verb == ["pr", "edit"]:
            return self._edit(argv)
        if verb == ["pr", "view"]:
            return self._view(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _create(self, argv):
        branch = argv[argv.index("--head") + 1]
        if branch in self.open_prs:
            # Verbatim shape of the real refusal, because the code keys on it.
            return subprocess.CompletedProcess(
                argv, 1, "",
                f'a pull request for branch "{branch}" into branch "main" '
                f"already exists:\n{self.open_prs[branch]}\n",
            )
        url = f"https://github.com/acme/fleet/pull/{len(self.open_prs) + 1}"
        self.open_prs[branch] = url
        self.titles[branch] = argv[argv.index("--title") + 1]
        return subprocess.CompletedProcess(argv, 0, url + "\n", "")

    def _edit(self, argv):
        branch = argv[3]
        if branch not in self.open_prs:
            return subprocess.CompletedProcess(argv, 1, "", "no pull requests found\n")
        self.titles[branch] = argv[argv.index("--title") + 1]
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _view(self, argv):
        branch = argv[3]
        if branch not in self.open_prs:
            return subprocess.CompletedProcess(argv, 1, "", "no pull requests found\n")
        return subprocess.CompletedProcess(argv, 0, self.open_prs[branch] + "\n", "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
