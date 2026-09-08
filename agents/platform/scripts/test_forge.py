#!/usr/bin/env python3
"""Tests for forge.py.

Driven by a fake `gh` runner rather than by patching `subprocess`, because what
is worth pinning here is the argv the provider builds and the shape it hands
back — not that Python can spawn a process. `FakeGh` asserts on the former and
scripts the latter.

Four properties carry most of the weight:

* **All three comment endpoints are read.** GitHub splits one human-visible
  conversation across the conversation tab, inline review comments, and review
  summaries. Reading two of three makes the agent ignore requests at random,
  and the bug is invisible until someone types in the wrong box.
* **`--paginate` is present on every list.** A truncated page looks exactly
  like a complete one, so nothing downstream can notice its absence.
* **Ownership needs all three of author, branch and head repository.** Any one
  of them alone is something a stranger can arrange — a fork PR carries the bare
  branch name, so `platform-agent/anything` is a name anybody can choose.
* **An unknown permission is not a "no".** A 404 from the collaborator endpoint
  means no write access; a proxy fault means nothing at all, and the sweep turns
  a "no" into a public refusal that is never retried.
* **The repository parser agrees with `resolver.py`'s.** They are two copies of
  one hardened parser, and `ParserAgreementTest` is what stops them drifting
  until `resolver.py` migrates onto this module.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import forge  # noqa: E402
import github_token_refresh  # noqa: E402


def _completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        ["gh", *argv], returncode, stdout=stdout, stderr=stderr
    )


class FakeGh:
    """A scripted `gh`, keyed on a distinguishing fragment of the argv.

    Keys are matched as subsequences of the joined argv so a test can pin the
    endpoint it cares about without restating every flag the provider passes.
    """

    def __init__(self, responses=None, default=None):
        self.responses = responses or {}
        self.default = default if default is not None else (0, "[]", "")
        self.calls: list[list[str]] = []
        # What each call put on fd 0, positionally aligned with `calls`. The
        # body of a comment travels this way now, so a test that only inspects
        # argv can no longer see what was posted.
        self.stdins: list[str | None] = []

    def __call__(self, argv, *, stdin=None, **_kwargs):
        argv = list(argv)
        self.calls.append(argv)
        self.stdins.append(stdin)
        joined = " ".join(argv)
        for key, value in self.responses.items():
            if key in joined:
                rc, stdout, stderr = value
                return _completed(argv, rc, stdout, stderr)
        rc, stdout, stderr = self.default
        return _completed(argv, rc, stdout, stderr)

    def argv_containing(self, fragment: str) -> list[str]:
        for argv in self.calls:
            if fragment in " ".join(argv):
                return argv
        raise AssertionError(f"no gh call matched {fragment!r}; saw {self.calls}")

    def stdin_of(self, fragment: str):
        for argv, stdin in zip(self.calls, self.stdins):
            if fragment in " ".join(argv):
                return stdin
        raise AssertionError(f"no gh call matched {fragment!r}; saw {self.calls}")


def write_settings(tmpdir: str, value: str) -> str:
    path = os.path.join(tmpdir, "SETTINGS.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"# Settings\n\n- **Git Repo:** {value}\n")
    return path


class ParseRepoTest(unittest.TestCase):
    def _resolve(self, value):
        return forge._parse_repo(value)

    def test_bare_shorthand(self):
        self.assertEqual(self._resolve("acme/toolkit"), "acme/toolkit")

    def test_https_url(self):
        self.assertEqual(
            self._resolve("https://github.com/acme/toolkit"), "acme/toolkit"
        )

    def test_scp_form_ssh_remote(self):
        self.assertEqual(
            self._resolve("git@github.com:acme/toolkit.git"), "acme/toolkit"
        )

    def test_www_prefix(self):
        self.assertEqual(
            self._resolve("https://www.github.com/acme/toolkit"), "acme/toolkit"
        )

    def test_git_suffix_is_stripped(self):
        self.assertEqual(self._resolve("acme/toolkit.git"), "acme/toolkit")

    def test_github_com_as_a_path_segment_on_another_host_is_rejected(self):
        """The confused-deputy shape the anchored regex exists for."""
        with self.assertRaises(forge.RepoUnparseable):
            self._resolve("https://evil.com/github.com/attacker/repo")

    def test_userinfo_cannot_smuggle_the_host(self):
        with self.assertRaises(forge.RepoUnparseable):
            self._resolve("https://user@evil.com/github.com/attacker/repo")

    def test_lookalike_host_is_rejected(self):
        with self.assertRaises(forge.RepoUnparseable):
            self._resolve("https://evilgithub.com/attacker/repo")

    def test_traversal_satisfies_the_shorthand_pattern_and_is_still_rejected(self):
        """`BARE_REPO_RE` admits "../.." — the component check is what stops it."""
        self.assertTrue(forge.BARE_REPO_RE.match("../.."))
        with self.assertRaises(forge.RepoUnparseable):
            self._resolve("../..")

    def test_leading_dash_would_be_parsed_as_a_flag(self):
        with self.assertRaises(forge.RepoUnparseable):
            self._resolve("-oops/repo")




class NormaliseLoginTest(unittest.TestCase):
    def test_bot_suffix_is_stripped(self):
        self.assertEqual(forge.normalise_login("kube-agents-bot[bot]"), "kube-agents-bot")

    def test_case_is_folded(self):
        self.assertEqual(forge.normalise_login("Kube-Agents-Bot"), "kube-agents-bot")

    def test_app_prefix_is_stripped(self):
        """`gh pr list --json author` spells an App `app/<name>`."""
        self.assertEqual(forge.normalise_login("app/kube-agents-bot"), "kube-agents-bot")

    def test_rest_and_graphql_spellings_converge(self):
        """The whole point: the two APIs disagree, the comparison must not."""
        self.assertEqual(
            forge.normalise_login("kube-agents-bot[bot]"),
            forge.normalise_login("kube-agents-bot"),
        )

    def test_all_three_spellings_of_one_app_converge(self):
        """Regression for an observed infinite re-answer loop.

        The sweep sees the PR author as `app/x` and its own past comments as
        `x[bot]`. When those did not normalise to the same key, no marker the
        agent had written was recognised as its own, so every tick re-answered
        the same comment.
        """
        spellings = ["app/kube-agents-bot", "kube-agents-bot[bot]", "kube-agents-bot"]
        keys = {forge.normalise_login(s) for s in spellings}
        self.assertEqual(keys, {"kube-agents-bot"})

    def test_empty_is_tolerated(self):
        self.assertEqual(forge.normalise_login(""), "")
        self.assertEqual(forge.normalise_login(None), "")


REPO = "acme/toolkit"
VIEWER = "kube-agents-bot"


class IsAgentPullRequestTest(unittest.TestCase):
    """Three conditions, and a test for each one failing on its own."""

    def _pr(
        self,
        head_ref="platform-agent/fix-1",
        author="kube-agents-bot[bot]",
        head_repo=REPO,
        labels=(),
    ):
        return forge.PullRequest(
            number=7,
            head_ref=head_ref,
            author=author,
            labels=labels,
            head_repo=head_repo,
        )

    def _ours(self, pr, viewer=VIEWER):
        return forge.is_agent_pull_request(pr, REPO, viewer)

    def test_our_own_pull_request_qualifies(self):
        self.assertTrue(self._ours(self._pr()))

    def test_a_human_branch_does_not(self):
        self.assertFalse(self._ours(self._pr(head_ref="feat/whatever")))

    def test_a_branch_merely_containing_the_prefix_does_not(self):
        self.assertFalse(self._ours(self._pr(head_ref="wip/platform-agent/x")))

    def test_a_fork_branch_with_our_prefix_is_not_ours(self):
        """The branch name is the attacker's to choose on a fork.

        `head.ref` carries the bare branch name for a cross-repository pull
        request, so anyone who can fork this repository can open one that reads
        `platform-agent/anything`. Accepting it would hand a stranger's pull
        request to `submit-suggestion`, which amends by pushing `head_ref` to
        *this* repository.
        """
        self.assertFalse(
            self._ours(self._pr(author="stranger", head_repo="stranger/toolkit"))
        )

    def test_our_prefix_on_a_fork_is_still_not_ours_even_authored_by_us(self):
        self.assertFalse(self._ours(self._pr(head_repo="somebody/toolkit")))

    def test_a_deleted_fork_reads_as_not_ours(self):
        """`head.repo` is null once the fork is gone; unknown is not local."""
        self.assertFalse(self._ours(self._pr(head_repo="")))

    def test_someone_elses_pull_request_on_our_branch_name_is_not_ours(self):
        """Where the re-answer loop came from.

        A maintainer can push `platform-agent/x` here and open a pull request
        on it. Keying identity off `pr.author` would then make a human the
        agent's "self", so no marker it wrote would be recognised as its own and
        every tick would re-answer the same comment.
        """
        self.assertFalse(self._ours(self._pr(author="maintainer")))

    def test_the_author_comparison_is_normalised(self):
        self.assertTrue(self._ours(self._pr(author="App/Kube-Agents-Bot")))

    def test_repository_comparison_is_case_insensitive(self):
        self.assertTrue(self._ours(self._pr(head_repo="Acme/Toolkit")))

    def test_no_viewer_means_nothing_is_ours(self):
        self.assertFalse(self._ours(self._pr(), viewer=""))

    def test_ignore_label_opts_out(self):
        self.assertTrue(self._pr(labels=("agent:ignore",)).is_ignored)
        self.assertFalse(self._pr(labels=("bug",)).is_ignored)


class RunGhTest(unittest.TestCase):
    def test_missing_binary_reports_the_shell_convention(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            result = forge.run_gh(["auth", "status"])
        self.assertEqual(result.returncode, forge.GH_MISSING_RC)

    def test_timeout_is_a_failure_not_an_exception(self):
        """A hung proxy must not hold the cron tick's per-job lock open."""
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=forge.GH_TIMEOUT_S),
        ):
            result = forge.run_gh(["api", "repos/a/b"])
        self.assertEqual(result.returncode, github_token_refresh.GH_TIMEOUT_RC)
        self.assertNotEqual(result.returncode, forge.GH_MISSING_RC)
        self.assertIn("timed out", result.stderr)

    def test_a_failing_command_returns_rather_than_raises(self):
        with mock.patch(
            "subprocess.run",
            return_value=_completed(["api"], 1, "", "HTTP 404"),
        ):
            result = forge.run_gh(["api", "repos/a/b"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("404", result.stderr)

    def test_the_call_goes_to_the_sandbox(self):
        """The agent image holds no gh, and this module's cron consumer runs there.

        The three tests above pass either way — `sandbox_exec.run` falls back to
        `subprocess.run` when no sandbox is configured, which is what a test
        machine looks like. So this is the one that would notice the routing
        being reverted to a direct `subprocess.run(["gh", ...])`.
        """
        with mock.patch.object(
            forge.sandbox_exec, "run",
            return_value=_completed(["gh", "auth", "status"], 0, "ok", ""),
        ) as ran:
            result = forge.run_gh(["auth", "status"])
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(ran.call_args.args[0], ["gh", "auth", "status"])

    def test_stdin_reaches_the_sandbox(self):
        """`--body-file -` is only half of it; the bytes have to be forwarded."""
        with mock.patch.object(
            forge.sandbox_exec, "run",
            return_value=_completed(["gh", "pr", "comment"], 0, "", ""),
        ) as ran:
            forge.run_gh(["pr", "comment", "1", "--body-file", "-"], stdin="hello")
        self.assertEqual(ran.call_args.kwargs["stdin"], "hello")

    def test_an_unreachable_sandbox_is_not_a_quiet_repository(self):
        """A transport failure has to reach the caller, not become an empty result."""
        with mock.patch.object(
            forge.sandbox_exec, "run",
            side_effect=forge.sandbox_exec.SandboxUnavailable("no route"),
        ):
            with self.assertRaises(forge.sandbox_exec.SandboxUnavailable):
                forge.run_gh(["issue", "list"])

    def test_auth_failure_retries_after_credential_refresh(self):
        github_token_refresh.reset_refresh_state()
        with mock.patch(
            "forge.run_gh_once",
            side_effect=[
                _completed(["auth", "status"], 1, "", "HTTP 401: Bad credentials"),
                _completed(["auth", "status"], 0, "Logged in", ""),
            ],
        ) as mock_once, mock.patch("forge.refresh_credentials_once", return_value=True) as mock_refresh:
            result = forge.run_gh(["auth", "status"])
            self.assertEqual(result.returncode, 0)
            self.assertEqual(mock_once.call_count, 2)
            mock_refresh.assert_called_once()

    def test_auth_failure_passes_target_repo_to_refresh_credentials(self):
        github_token_refresh.reset_refresh_state()
        with mock.patch(
            "forge.run_gh_once",
            side_effect=[
                _completed(["api", "repos/orgB/y/pulls"], 1, "", "HTTP 401: Bad credentials"),
                _completed(["api", "repos/orgB/y/pulls"], 0, "[]", ""),
            ],
        ) as mock_once, mock.patch("forge.refresh_credentials_once", return_value=True) as mock_refresh:
            result = forge.run_gh(["api", "repos/orgB/y/pulls"], repo="orgB/y")
            self.assertEqual(result.returncode, 0)
            mock_refresh.assert_called_once_with(["api", "repos/orgB/y/pulls"], repo="orgB/y")

    def test_provider_passes_repo_to_call(self):
        calls = []

        def fake_run(argv, repo=None, *, stdin=None):
            calls.append((argv, repo))
            return _completed(argv, 0, "[]", "")

        provider = forge.GitHubProvider(run=fake_run)
        provider.list_open_prs("orgB/y")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "orgB/y")

    def test_non_auth_failure_does_not_retry(self):
        github_token_refresh.reset_refresh_state()
        with mock.patch(
            "forge.run_gh_once",
            return_value=_completed(["api"], 1, "", "HTTP 404: Not Found"),
        ) as mock_once, mock.patch("forge.refresh_credentials_once") as mock_refresh:
            result = forge.run_gh(["api", "repos/a/b"])
            self.assertEqual(result.returncode, 1)
            self.assertEqual(mock_once.call_count, 1)
            mock_refresh.assert_not_called()


class PreflightTest(unittest.TestCase):
    def test_authenticated_passes(self):
        forge.gh_preflight(FakeGh(default=(0, "", "")))

    def test_missing_binary_is_distinguished_from_missing_auth(self):
        with self.assertRaises(forge.ForgeError) as ctx:
            forge.gh_preflight(FakeGh(default=(forge.GH_MISSING_RC, "", "")))
        self.assertEqual(ctx.exception.reason, "GH_CLI_NOT_FOUND")

    def test_unauthenticated_reports_its_own_reason(self):
        github_token_refresh.reset_refresh_state()
        with self.assertRaises(forge.ForgeError) as ctx:
            forge.gh_preflight(FakeGh(default=(1, "", "not logged in")))
        self.assertEqual(ctx.exception.reason, "GITHUB_AUTH_NOT_CONFIGURED")

    def test_refused_refresh_reports_token_refresh_failed(self):
        github_token_refresh.reset_refresh_state()
        github_token_refresh._refresh_failed = True
        try:
            with self.assertRaises(forge.ForgeError) as ctx:
                forge.gh_preflight(FakeGh(default=(1, "", "not logged in")))
            self.assertEqual(ctx.exception.reason, "GITHUB_TOKEN_REFRESH_FAILED")
        finally:
            github_token_refresh.reset_refresh_state()

    def test_preflight_default_runner_refused_refresh(self):
        github_token_refresh.reset_refresh_state()

        def fake_refresh(*_args, **_kwargs):
            github_token_refresh._refresh_failed = True
            return False

        with mock.patch(
            "forge.run_gh_once",
            return_value=_completed(["auth", "status"], 1, "", "HTTP 401: Bad credentials"),
        ), mock.patch("forge.refresh_credentials_once", side_effect=fake_refresh):
            with self.assertRaises(forge.ForgeError) as ctx:
                forge.gh_preflight()
            self.assertEqual(ctx.exception.reason, "GITHUB_TOKEN_REFRESH_FAILED")
        github_token_refresh.reset_refresh_state()


class CallSeamTest(unittest.TestCase):
    def test_non_zero_exit_becomes_repo_unreachable(self):
        provider = forge.GitHubProvider(run=FakeGh(default=(1, "", "HTTP 404")))
        with self.assertRaises(forge.ForgeError) as ctx:
            provider._call(["api", "repos/a/b"])
        self.assertEqual(ctx.exception.reason, "REPO_UNREACHABLE")
        self.assertIn("404", ctx.exception.value)

    def test_unparseable_json_is_its_own_reason(self):
        provider = forge.GitHubProvider(run=FakeGh(default=(0, "not json", "")))
        with self.assertRaises(forge.ForgeError) as ctx:
            provider._call(["api", "repos/a/b"])
        self.assertEqual(ctx.exception.reason, "FORGE_RESPONSE_UNREADABLE")

    def test_empty_stdout_is_an_empty_list_not_a_fault(self):
        provider = forge.GitHubProvider(run=FakeGh(default=(0, "  \n", "")))
        self.assertEqual(provider._call(["api", "repos/a/b"]), [])

    def test_stderr_is_truncated_so_a_reason_code_stays_readable(self):
        provider = forge.GitHubProvider(run=FakeGh(default=(1, "", "x" * 5000)))
        with self.assertRaises(forge.ForgeError) as ctx:
            provider._call(["api", "repos/a/b"])
        self.assertLessEqual(len(ctx.exception.value), 200)

    def test_retry_transient_recovers_on_second_attempt(self):
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if len(calls) == 1:
                return subprocess.CompletedProcess(argv, 1, "", "transient error")
            return subprocess.CompletedProcess(argv, 0, '[{"id": 1}]', "")

        provider = forge.GitHubProvider(run=run)
        result = provider._call(["api", "repos/a/b"], retry_transient=True)
        self.assertEqual(result, [{"id": 1}])
        self.assertEqual(len(calls), 2)

    def test_retry_transient_disabled_by_default(self):
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "error")

        provider = forge.GitHubProvider(run=run)
        with self.assertRaises(forge.ForgeError):
            provider._call(["api", "repos/a/b"], retry_transient=False)
        self.assertEqual(len(calls), 1)

    def test_retry_transient_skips_definitive_http_errors(self):
        for status_err in ("HTTP 404", "HTTP 403", "HTTP 401: Bad credentials", "gh: Not Found (HTTP 404)"):
            with self.subTest(status_err=status_err):
                calls = []

                def run(argv, **_kwargs):
                    calls.append(argv)
                    return subprocess.CompletedProcess(argv, 1, "", status_err)

                provider = forge.GitHubProvider(run=run)
                with self.assertRaises(forge.ForgeError):
                    provider._call(["api", "repos/a/b"], retry_transient=True)
                self.assertEqual(len(calls), 1)

    def test_retry_transient_skips_sidecar_timeout(self):
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, github_token_refresh.GH_TIMEOUT_RC, "", "timeout")

        provider = forge.GitHubProvider(run=run)
        with self.assertRaises(forge.ForgeError):
            provider._call(["api", "repos/a/b"], retry_transient=True)
        self.assertEqual(len(calls), 1)

    def test_retry_transient_skips_missing_binary(self):
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, github_token_refresh.GH_MISSING_RC, "", "missing")

        provider = forge.GitHubProvider(run=run)
        with self.assertRaises(forge.ForgeError):
            provider._call(["api", "repos/a/b"], retry_transient=True)
        self.assertEqual(len(calls), 1)

    def test_type_error_in_runner_propagates(self):
        def bad_runner(argv, repo=None, *, stdin=None):
            raise TypeError("something inside runner broke")

        provider = forge.GitHubProvider(run=bad_runner)
        with self.assertRaises(TypeError) as ctx:
            provider._call(["api", "repos/a/b"])
        self.assertEqual(str(ctx.exception), "something inside runner broke")


PRS_JSON = json.dumps(
    [
        {
            "number": 12,
            "head": {
                "ref": "platform-agent/bump-replicas",
                "repo": {"full_name": "acme/toolkit"},
                "sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
            },
            "user": {"login": "kube-agents-bot[bot]"},
            "labels": [{"name": "automated"}],
            "html_url": "https://github.com/acme/toolkit/pull/12",
        },
        {
            "number": 13,
            "head": {
                "ref": "platform-agent/looks-like-ours",
                # Same branch name, somebody else's repository.
                "repo": {"full_name": "stranger/toolkit"},
            },
            "user": {"login": "stranger"},
            "labels": [],
            "html_url": "https://github.com/acme/toolkit/pull/13",
        },
    ]
)

PULLS_ENDPOINT = "repos/acme/toolkit/pulls"


class ListOpenPrsTest(unittest.TestCase):
    def test_rows_are_normalised(self):
        provider = forge.GitHubProvider(run=FakeGh({PULLS_ENDPOINT: (0, PRS_JSON, "")}))
        prs = provider.list_open_prs(REPO)
        self.assertEqual([p.number for p in prs], [12, 13])
        self.assertEqual(prs[0].head_ref, "platform-agent/bump-replicas")
        self.assertEqual(prs[0].labels, ("automated",))
        self.assertEqual(prs[0].url, "https://github.com/acme/toolkit/pull/12")
        self.assertTrue(forge.is_agent_pull_request(prs[0], REPO, VIEWER))
        self.assertFalse(forge.is_agent_pull_request(prs[1], REPO, VIEWER))

    def test_the_head_sha_is_carried_through(self):
        """What a reply's claim to have amended the branch is checked against."""
        provider = forge.GitHubProvider(run=FakeGh({PULLS_ENDPOINT: (0, PRS_JSON, "")}))
        prs = provider.list_open_prs(REPO)
        self.assertEqual(prs[0].head_sha, "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678")
        self.assertEqual(prs[1].head_sha, "")

    def test_the_head_repository_is_carried_through(self):
        """The field `gh pr list` does not have, and the fork check needs."""
        provider = forge.GitHubProvider(run=FakeGh({PULLS_ENDPOINT: (0, PRS_JSON, "")}))
        prs = provider.list_open_prs(REPO)
        self.assertEqual(prs[0].head_repo, "acme/toolkit")
        self.assertEqual(prs[1].head_repo, "stranger/toolkit")

    def test_argv_scopes_the_repo_and_asks_only_for_open_prs(self):
        fake = FakeGh({PULLS_ENDPOINT: (0, PRS_JSON, "")})
        forge.GitHubProvider(run=fake).list_open_prs(REPO)
        argv = fake.argv_containing(PULLS_ENDPOINT)
        self.assertIn("state=open", " ".join(argv))

    def test_the_listing_paginates_rather_than_truncating(self):
        """`gh pr list --limit` drops the overflow and says nothing.

        Human pull requests share that budget, so on a busy repository the
        agent's own would fall out of the window. The previous code raised on a
        full page instead, which on a `deliver: "all"` job at `*/10` is 144
        identical warnings a day with the feature switched off.
        """
        fake = FakeGh({PULLS_ENDPOINT: (0, PRS_JSON, "")})
        forge.GitHubProvider(run=fake).list_open_prs(REPO)
        self.assertIn("--paginate", fake.argv_containing(PULLS_ENDPOINT))

    def test_a_full_page_is_returned_rather_than_refused(self):
        rows = json.dumps(
            [
                {
                    "number": n,
                    "head": {"ref": f"platform-agent/x{n}", "repo": {"full_name": REPO}},
                    "user": {"login": "bot"},
                    "labels": [],
                }
                for n in range(forge.PR_PAGE_SIZE)
            ]
        )
        provider = forge.GitHubProvider(run=FakeGh({PULLS_ENDPOINT: (0, rows, "")}))
        self.assertEqual(len(provider.list_open_prs(REPO)), forge.PR_PAGE_SIZE)

    def test_missing_fields_do_not_crash_the_sweep(self):
        provider = forge.GitHubProvider(run=FakeGh({PULLS_ENDPOINT: (0, "[{}]", "")}))
        prs = provider.list_open_prs(REPO)
        self.assertEqual(prs[0].number, 0)
        self.assertEqual(prs[0].author, "")
        self.assertEqual(prs[0].head_repo, "")
        self.assertFalse(forge.is_agent_pull_request(prs[0], REPO, VIEWER))

    def test_a_deleted_fork_leaves_a_null_head_repo(self):
        rows = json.dumps(
            [{"number": 4, "head": {"ref": "platform-agent/x", "repo": None}}]
        )
        provider = forge.GitHubProvider(run=FakeGh({PULLS_ENDPOINT: (0, rows, "")}))
        self.assertEqual(provider.list_open_prs(REPO)[0].head_repo, "")


COMMITS_ENDPOINT = "repos/acme/toolkit/pulls/12/commits"


class ListCommitsTest(unittest.TestCase):
    """The read behind `pr_conversation reply --verify-commit`."""

    def _pr(self):
        return forge.PullRequest(number=12, head_ref="platform-agent/x", author="bot")

    def test_shas_are_returned_tip_last(self):
        rows = json.dumps([{"sha": "aaa1"}, {"sha": "bbb2"}])
        provider = forge.GitHubProvider(run=FakeGh({COMMITS_ENDPOINT: (0, rows, "")}))
        self.assertEqual(
            [c.sha for c in provider.list_commits(REPO, self._pr())], ["aaa1", "bbb2"]
        )

    def test_the_committer_date_travels_with_the_sha(self):
        """Membership alone cannot tell a commit made in answer to the request
        from one that opened the pull request; the date is what does."""
        rows = json.dumps(
            [{"sha": "aaa1", "commit": {"committer": {"date": "2026-01-02T03:04:05Z"}}}]
        )
        provider = forge.GitHubProvider(run=FakeGh({COMMITS_ENDPOINT: (0, rows, "")}))
        self.assertEqual(
            provider.list_commits(REPO, self._pr())[0].committed_at, "2026-01-02T03:04:05Z"
        )

    def test_the_author_date_is_not_used(self):
        """A rebase or cherry-pick keeps an author date from weeks ago, which
        would clear the recency bound for work that predates the request."""
        rows = json.dumps(
            [
                {
                    "sha": "aaa1",
                    "commit": {
                        "author": {"date": "2020-01-01T00:00:00Z"},
                        "committer": {"date": "2026-01-02T03:04:05Z"},
                    },
                }
            ]
        )
        provider = forge.GitHubProvider(run=FakeGh({COMMITS_ENDPOINT: (0, rows, "")}))
        self.assertEqual(
            provider.list_commits(REPO, self._pr())[0].committed_at, "2026-01-02T03:04:05Z"
        )

    def test_a_missing_date_is_empty_not_absent(self):
        """The caller distinguishes "no date" from "old date" and refuses on
        the first, so the field must survive the row lacking it."""
        rows = json.dumps([{"sha": "aaa1"}, {"sha": "bbb2", "commit": {}}])
        provider = forge.GitHubProvider(run=FakeGh({COMMITS_ENDPOINT: (0, rows, "")}))
        self.assertEqual(
            [c.committed_at for c in provider.list_commits(REPO, self._pr())], ["", ""]
        )

    def test_the_listing_paginates(self):
        """A long-lived pull request outruns one page, and a missed commit reads
        as a false claim."""
        fake = FakeGh({COMMITS_ENDPOINT: (0, "[]", "")})
        forge.GitHubProvider(run=fake).list_commits(REPO, self._pr())
        self.assertIn("--paginate", fake.argv_containing(COMMITS_ENDPOINT))

    def test_a_row_without_a_sha_is_dropped_not_returned_empty(self):
        """An empty string would prefix-match nothing, but it would also make
        `any(...)` on a short claim behave oddly. Drop it."""
        rows = json.dumps([{"sha": ""}, {}, {"sha": "ccc3"}])
        provider = forge.GitHubProvider(run=FakeGh({COMMITS_ENDPOINT: (0, rows, "")}))
        self.assertEqual([c.sha for c in provider.list_commits(REPO, self._pr())], ["ccc3"])

    def test_a_failure_raises_rather_than_reporting_no_commits(self):
        """No commits and cannot tell are opposite answers to "did it land?"."""
        provider = forge.GitHubProvider(
            run=FakeGh({COMMITS_ENDPOINT: (1, "", "gh: Server Error (HTTP 500)")})
        )
        with self.assertRaises(forge.ForgeError):
            provider.list_commits(REPO, self._pr())


ISSUE_COMMENTS = json.dumps(
    [
        {
            "id": 100,
            "node_id": "IC_a",
            "user": {"login": "reviewer"},
            "body": "/agent why this value?",
            "author_association": "COLLABORATOR",
            "created_at": "2026-08-12T10:00:00Z",
        },
        {
            "id": 101,
            "node_id": "IC_b",
            "user": {"login": "drive-by"},
            "body": "/agent do something",
            "author_association": "NONE",
            "created_at": "2026-08-12T09:00:00Z",
        },
    ]
)

REVIEW_COMMENTS = json.dumps(
    [
        {
            "id": 100,  # same numeric id as IC_a, different endpoint
            "node_id": "PRRC_a",
            "user": {"login": "reviewer"},
            "body": "inline nit",
            "author_association": "MEMBER",
            "created_at": "2026-08-12T11:00:00Z",
            "path": "charts/values.yaml",
            "line": 42,
        }
    ]
)

REVIEWS = json.dumps(
    [
        {
            "id": 200,
            "node_id": "PRR_a",
            "user": {"login": "owner"},
            "body": "please address the above",
            "author_association": "OWNER",
            "submitted_at": "2026-08-12T12:00:00Z",
        },
        {
            "id": 201,
            "node_id": "PRR_empty",
            "user": {"login": "owner"},
            "body": "",
            "author_association": "OWNER",
            "submitted_at": "2026-08-12T12:30:00Z",
        },
    ]
)


def permission(value: str) -> tuple[int, str, str]:
    return (0, json.dumps({"permission": value}), "")


def comments_fake():
    return FakeGh(
        {
            "issues/12/comments": (0, ISSUE_COMMENTS, ""),
            "pulls/12/comments": (0, REVIEW_COMMENTS, ""),
            "pulls/12/reviews": (0, REVIEWS, ""),
            # Write access is a lookup, not a field on the comment. The
            # associations above are deliberately left inconsistent with these
            # answers: an App token misreports them, so nothing may read them.
            "collaborators/reviewer/permission": permission("write"),
            "collaborators/owner/permission": permission("admin"),
            # A non-collaborator 404s rather than answering "none".
            "collaborators/drive-by/permission": (1, "", "gh: Not Found (HTTP 404)"),
        }
    )


class ListCommentsTest(unittest.TestCase):
    def setUp(self):
        self.pr = forge.PullRequest(
            number=12, head_ref="platform-agent/x", author="kube-agents-bot"
        )

    def test_all_three_endpoints_are_read(self):
        """Reading two of three makes the agent ignore requests at random."""
        fake = comments_fake()
        forge.GitHubProvider(run=fake).list_comments("acme/toolkit", self.pr)
        joined = [" ".join(argv) for argv in fake.calls]
        self.assertTrue(any("issues/12/comments" in c for c in joined))
        self.assertTrue(any("pulls/12/comments" in c for c in joined))
        self.assertTrue(any("pulls/12/reviews" in c for c in joined))

    def test_every_list_paginates(self):
        """The default page is 30 and a truncated list looks complete.

        Only the *list* calls: the permission lookup returns a single object,
        for which `--paginate` means nothing.
        """
        fake = comments_fake()
        forge.GitHubProvider(run=fake).list_comments("acme/toolkit", self.pr)
        listings = [
            argv
            for argv in fake.calls
            if any(part.endswith(("/comments", "/reviews")) for part in argv)
        ]
        self.assertEqual(len(listings), 3, f"expected three listings, saw {fake.calls}")
        for argv in listings:
            self.assertIn("--paginate", argv, f"missing --paginate in {argv}")

    def test_results_are_ordered_oldest_first(self):
        """The per-tick cap takes the oldest, so newer requests cannot starve older ones."""
        comments = forge.GitHubProvider(run=comments_fake()).list_comments(
            "acme/toolkit", self.pr
        )
        self.assertEqual(
            [c.node_id for c in comments], ["IC_b", "IC_a", "PRRC_a", "PRR_a"]
        )

    def test_write_permission_becomes_a_boolean(self):
        by_id = {
            c.node_id: c
            for c in forge.GitHubProvider(run=comments_fake()).list_comments(
                "acme/toolkit", self.pr
            )
        }
        self.assertTrue(by_id["IC_a"].can_write)  # reviewer -> write
        self.assertTrue(by_id["PRRC_a"].can_write)  # reviewer -> write
        self.assertTrue(by_id["PRR_a"].can_write)  # owner -> admin
        self.assertFalse(by_id["IC_b"].can_write)  # drive-by -> 404

    def test_author_association_is_never_read(self):
        """An App token reports a repo admin as CONTRIBUTOR.

        Regression for a live failure: the trust gate refused every legitimate
        reviewer because it believed the field. `can_write` must follow the
        permission lookup even when the association flatly contradicts it.
        """
        rows = json.dumps(
            [
                {
                    "id": 1,
                    "node_id": "IC_x",
                    "user": {"login": "admin-person"},
                    "body": "/agent go",
                    # What an installation token actually sees for an admin.
                    "author_association": "CONTRIBUTOR",
                    "created_at": "2026-08-12T10:00:00Z",
                }
            ]
        )
        fake = FakeGh(
            {
                "issues/12/comments": (0, rows, ""),
                "collaborators/admin-person/permission": permission("admin"),
            }
        )
        comments = forge.GitHubProvider(run=fake).list_comments("acme/toolkit", self.pr)
        self.assertTrue(comments[0].can_write)

    def test_a_read_only_collaborator_cannot_direct_the_agent(self):
        fake = FakeGh(
            {
                "issues/12/comments": (
                    0,
                    json.dumps(
                        [
                            {
                                "id": 1,
                                "node_id": "IC_r",
                                "user": {"login": "watcher"},
                                "body": "/agent go",
                                # Would have passed the old association gate.
                                "author_association": "MEMBER",
                                "created_at": "2026-08-12T10:00:00Z",
                            }
                        ]
                    ),
                    "",
                ),
                "collaborators/watcher/permission": permission("read"),
            }
        )
        comments = forge.GitHubProvider(run=fake).list_comments("acme/toolkit", self.pr)
        self.assertFalse(comments[0].can_write)

    def test_permission_is_looked_up_once_per_account(self):
        """Three comments from one person must not cost three API calls."""
        fake = comments_fake()
        forge.GitHubProvider(run=fake).list_comments("acme/toolkit", self.pr)
        lookups = [
            argv
            for argv in fake.calls
            if any("collaborators/reviewer/permission" in part for part in argv)
        ]
        self.assertEqual(len(lookups), 1, f"expected one lookup, saw {lookups}")

    def test_a_bot_and_a_user_of_the_same_name_are_two_accounts(self):
        """The cache key must name the account it asked about.

        `normalise_login` strips a trailing `[bot]` and folds case so a mention
        matches a handle. Keying the permission cache on it collapsed the App
        `helper[bot]` and the ordinary user `helper` into one slot, and
        whichever `_collect` reached first decided trust for both — either
        handing a non-collaborator the `can_write=True` that clears the sweep's
        only trust gate, or refusing a maintainer and writing a permanent
        `agent-refused` marker. Both accounts comment here, so the answer is
        re-derived on every tick rather than depending on ordering.
        """
        fake = FakeGh(
            {
                "issues/12/comments": (
                    0,
                    json.dumps(
                        [
                            {
                                "id": 200,
                                "node_id": "IC_bot",
                                "user": {"login": "helper[bot]"},
                                "body": "/agent do it",
                                "created_at": "2026-08-12T10:00:00Z",
                            },
                            {
                                "id": 201,
                                "node_id": "IC_human",
                                "user": {"login": "helper"},
                                "body": "/agent do it",
                                "created_at": "2026-08-12T11:00:00Z",
                            },
                        ]
                    ),
                    "",
                ),
                "pulls/12/comments": (0, "[]", ""),
                "pulls/12/reviews": (0, "[]", ""),
                # The App is a collaborator; the same-named user is not.
                "collaborators/helper%5Bbot%5D/permission": permission("write"),
                "collaborators/helper/permission": (
                    1,
                    "",
                    "gh: Not Found (HTTP 404)",
                ),
            }
        )
        by_id = {
            c.node_id: c
            for c in forge.GitHubProvider(run=fake).list_comments(
                "acme/toolkit", self.pr
            )
        }
        self.assertIs(by_id["IC_bot"].can_write, True)
        self.assertIs(by_id["IC_human"].can_write, False)

    def test_an_empty_review_body_is_not_an_utterance(self):
        ids = [
            c.node_id
            for c in forge.GitHubProvider(run=comments_fake()).list_comments(
                "acme/toolkit", self.pr
            )
        ]
        self.assertNotIn("PRR_empty", ids)

    def test_a_review_uses_submitted_at_for_its_timestamp(self):
        by_id = {
            c.node_id: c
            for c in forge.GitHubProvider(run=comments_fake()).list_comments(
                "acme/toolkit", self.pr
            )
        }
        self.assertEqual(by_id["PRR_a"].created_at, "2026-08-12T12:00:00Z")

    def test_node_id_distinguishes_comments_that_share_a_numeric_id(self):
        """IC_a and PRRC_a are both id 100 on different endpoints."""
        comments = forge.GitHubProvider(run=comments_fake()).list_comments(
            "acme/toolkit", self.pr
        )
        collide = [c for c in comments if c.numeric_id == 100]
        self.assertEqual(len(collide), 2)
        self.assertEqual(len({c.node_id for c in collide}), 2)

    def test_inline_location_is_carried_through(self):
        by_id = {
            c.node_id: c
            for c in forge.GitHubProvider(run=comments_fake()).list_comments(
                "acme/toolkit", self.pr
            )
        }
        self.assertEqual(by_id["PRRC_a"].path, "charts/values.yaml")
        self.assertEqual(by_id["PRRC_a"].line, 42)
        self.assertEqual(by_id["IC_a"].path, "")
        self.assertIsNone(by_id["IC_a"].line)

    def test_kind_is_recorded_per_endpoint(self):
        by_id = {
            c.node_id: c
            for c in forge.GitHubProvider(run=comments_fake()).list_comments(
                "acme/toolkit", self.pr
            )
        }
        self.assertEqual(by_id["IC_a"].kind, "issue")
        self.assertEqual(by_id["PRRC_a"].kind, "review_comment")
        self.assertEqual(by_id["PRR_a"].kind, "review")

    def test_is_bot_reads_the_unnormalised_suffix(self):
        self.assertTrue(
            forge.Comment(
                node_id="x",
                author="kube-agents-bot[bot]",
                body="",
                can_write=True,
                created_at="",
            ).is_bot
        )


class PostCommentTest(unittest.TestCase):
    BODY = "Thanks — that is fixed in a1b2c3d.\n\n> your words back at you\n"

    def _post(self, fake):
        pr = forge.PullRequest(number=12, head_ref="platform-agent/x", author="bot")
        forge.GitHubProvider(run=fake).post_comment("acme/toolkit", pr, self.BODY)

    def test_body_travels_on_stdin_never_on_the_command_line(self):
        """A reviewer's words go back through a proxy and two shells' quoting."""
        fake = FakeGh(default=(0, "", ""))
        self._post(fake)
        argv = fake.argv_containing("pr comment")
        self.assertIn("--body-file", argv)
        self.assertNotIn("--body", argv)
        self.assertEqual(argv[argv.index("--body-file") + 1], "-")
        self.assertEqual(argv[argv.index("-R") + 1], "acme/toolkit")
        self.assertEqual(fake.stdin_of("pr comment"), self.BODY)

    def test_no_argument_names_a_path(self):
        """The three processes in this call have three different filesystems.

        A path would name a file in the agent pod; `gh` runs in the broker pod.
        """
        fake = FakeGh(default=(0, "", ""))
        self._post(fake)
        for arg in fake.argv_containing("pr comment"):
            self.assertFalse(arg.startswith("/"), arg)

    def test_a_failed_post_is_not_swallowed(self):
        fake = FakeGh(default=(1, "", "HTTP 403"))
        with self.assertRaises(forge.ForgeError):
            self._post(fake)


class AcknowledgeTest(unittest.TestCase):
    def _comment(self, kind):
        return forge.Comment(
            node_id="n",
            numeric_id=100,
            author="reviewer",
            body="",
            can_write=True,
            created_at="",
            kind=kind,
        )

    def test_issue_comment_uses_the_issues_reactions_endpoint(self):
        fake = FakeGh(default=(0, "", ""))
        self.assertTrue(
            forge.GitHubProvider(run=fake).acknowledge(
                "acme/toolkit", self._comment("issue")
            )
        )
        argv = fake.argv_containing("reactions")
        self.assertIn("repos/acme/toolkit/issues/comments/100/reactions", argv)
        self.assertIn("content=eyes", argv)

    def test_review_comment_uses_the_pulls_reactions_endpoint(self):
        fake = FakeGh(default=(0, "", ""))
        forge.GitHubProvider(run=fake).acknowledge(
            "acme/toolkit", self._comment("review_comment")
        )
        argv = fake.argv_containing("reactions")
        self.assertIn("repos/acme/toolkit/pulls/comments/100/reactions", argv)

    def test_a_review_summary_has_no_reaction_endpoint(self):
        fake = FakeGh(default=(0, "", ""))
        self.assertFalse(
            forge.GitHubProvider(run=fake).acknowledge(
                "acme/toolkit", self._comment("review")
            )
        )
        self.assertEqual(fake.calls, [])

    def test_a_failed_reaction_never_blocks_the_answer(self):
        """Best-effort by contract: the courtesy must not gate the reply."""
        fake = FakeGh(default=(1, "", "HTTP 403"))
        self.assertFalse(
            forge.GitHubProvider(run=fake).acknowledge(
                "acme/toolkit", self._comment("issue")
            )
        )


AUTH_OK = (
    "github.com\n"
    "  ✓ Logged in to github.com account toshiowang-labs-kube-agents[bot] "
    "(/var/lib/credential-proxy/home/.config/gh/hosts.yml)\n"
    "  - Active account: true\n"
)
AUTH_BROKEN = (
    "github.com\n"
    "  X Failed to log in to github.com account toshiowang-labs-kube-agents[bot] "
    "(/var/lib/credential-proxy/home/.config/gh/hosts.yml)\n"
    "  - The token in hosts.yml is invalid.\n"
)


class ViewerLoginTest(unittest.TestCase):
    """The identity that decides what is ours, and whose comments are ours."""

    def test_the_account_is_read_from_auth_status_and_normalised(self):
        provider = forge.GitHubProvider(run=FakeGh({"auth status": (0, AUTH_OK, "")}))
        self.assertEqual(provider.viewer_login(), "toshiowang-labs-kube-agents")

    def test_it_never_asks_the_user_endpoint(self):
        """An installation token gets 401 from `GET /user` — verified live."""
        fake = FakeGh({"auth status": (0, AUTH_OK, "")})
        forge.GitHubProvider(run=fake).viewer_login()
        self.assertNotIn(["api", "user"], fake.calls)

    def test_stderr_is_read_too(self):
        """`gh` has moved this between streams across versions."""
        provider = forge.GitHubProvider(run=FakeGh({"auth status": (0, "", AUTH_OK)}))
        self.assertEqual(provider.viewer_login(), "toshiowang-labs-kube-agents")

    def test_a_broken_credential_names_nobody(self):
        """The failure line names an account whose token no longer works."""
        provider = forge.GitHubProvider(
            run=FakeGh({"auth status": (1, AUTH_BROKEN, "")})
        )
        self.assertEqual(provider.viewer_login(), "")

    def test_unrecognisable_output_is_empty_rather_than_a_guess(self):
        provider = forge.GitHubProvider(run=FakeGh({"auth status": (0, "hello", "")}))
        self.assertEqual(provider.viewer_login(), "")

    def test_it_is_resolved_once_per_provider(self):
        fake = FakeGh({"auth status": (0, AUTH_OK, "")})
        provider = forge.GitHubProvider(run=fake)
        provider.viewer_login()
        provider.viewer_login()
        self.assertEqual(len([c for c in fake.calls if c[:2] == ["auth", "status"]]), 1)

    def test_an_empty_answer_is_cached_too(self):
        fake = FakeGh({"auth status": (0, "hello", "")})
        provider = forge.GitHubProvider(run=fake)
        provider.viewer_login()
        provider.viewer_login()
        self.assertEqual(len([c for c in fake.calls if c[:2] == ["auth", "status"]]), 1)


class PermissionUnknownTest(unittest.TestCase):
    """A 404 is an answer; anything else is not.

    The sweep answers a `False` with a public refusal stamped with a marker that
    stops the request ever being retried. Collapsing a transient fault into that
    `False` permanently refuses a maintainer over a network blip.
    """

    def _comment_with(self, permission_response):
        rows = json.dumps(
            [
                {
                    "id": 1,
                    "node_id": "IC_x",
                    "user": {"login": "maintainer"},
                    "body": "/agent go",
                    "created_at": "2026-08-12T10:00:00Z",
                }
            ]
        )
        fake = FakeGh(
            {
                "issues/12/comments": (0, rows, ""),
                "collaborators/maintainer/permission": permission_response,
            }
        )
        pr = forge.PullRequest(number=12, head_ref="platform-agent/x", author="bot")
        return forge.GitHubProvider(run=fake).list_comments(REPO, pr)[0]

    def test_a_404_is_a_definitive_no(self):
        comment = self._comment_with((1, "", "gh: Not Found (HTTP 404)"))
        self.assertFalse(comment.can_write)
        self.assertTrue(comment.can_write_known)

    def test_a_server_error_is_not_an_answer(self):
        comment = self._comment_with((1, "", "gh: Server Error (HTTP 502)"))
        self.assertFalse(comment.can_write)
        self.assertFalse(comment.can_write_known)

    def test_a_proxy_fault_with_no_status_is_not_an_answer(self):
        comment = self._comment_with((1, "", "connection refused"))
        self.assertFalse(comment.can_write_known)

    def test_a_timeout_is_not_an_answer(self):
        comment = self._comment_with(
            (1, "", f"'gh' timed out after {forge.GH_TIMEOUT_S}s.")
        )
        self.assertFalse(comment.can_write_known)

    def test_a_granted_permission_is_known(self):
        comment = self._comment_with(permission("write"))
        self.assertTrue(comment.can_write)
        self.assertTrue(comment.can_write_known)

    def test_an_unknown_answer_is_cached_rather_than_retried_per_comment(self):
        rows = json.dumps(
            [
                {
                    "id": n,
                    "node_id": f"IC_{n}",
                    "user": {"login": "maintainer"},
                    "body": "/agent go",
                    "created_at": "2026-08-12T10:00:00Z",
                }
                for n in (1, 2, 3)
            ]
        )
        fake = FakeGh(
            {
                "issues/12/comments": (0, rows, ""),
                "collaborators/maintainer/permission": (1, "", "HTTP 502"),
            }
        )
        pr = forge.PullRequest(number=12, head_ref="platform-agent/x", author="bot")
        comments = forge.GitHubProvider(run=fake).list_comments(REPO, pr)
        self.assertEqual(len(comments), 3)
        lookups = [c for c in fake.calls if "collaborators/maintainer" in " ".join(c)]
        self.assertEqual(len(lookups), 1)

    def test_a_bot_login_is_percent_encoded_into_the_path(self):
        """`[` and `]` are not path characters, and an App comments as `x[bot]`.

        Unencoded the request is malformed rather than a 404, so the answer is
        neither yes nor no: every allowlisted bot caches as unknown and is
        asked again on the next tick, forever.
        """
        rows = json.dumps(
            [
                {
                    "id": 1,
                    "node_id": "IC_b",
                    "user": {"login": "helper[bot]"},
                    "body": "/agent go",
                    "created_at": "2026-08-12T10:00:00Z",
                }
            ]
        )
        fake = FakeGh(
            {
                "issues/12/comments": (0, rows, ""),
                "collaborators/helper%5Bbot%5D/permission": permission("write"),
            }
        )
        pr = forge.PullRequest(number=12, head_ref="platform-agent/x", author="bot")
        comments = forge.GitHubProvider(run=fake).list_comments(REPO, pr)
        self.assertTrue(comments[0].can_write)


class ProviderForTest(unittest.TestCase):
    def test_github_host_selects_the_github_provider(self):
        self.assertIsInstance(forge.provider_for(repo="https://github.com/acme/toolkit"), forge.GitHubProvider)

    def test_bare_shorthand_means_github(self):
        """The operator writes `owner/repo` through verbatim; it is `gh -R`'s own form."""
        self.assertIsInstance(forge.provider_for(repo="acme/toolkit"), forge.GitHubProvider)

    def test_omitted_repo_defaults_to_github_provider(self):
        self.assertIsInstance(forge.provider_for(), forge.GitHubProvider)

    def test_the_run_seam_is_forwarded_to_the_provider(self):
        fake = FakeGh()
        provider = forge.provider_for(repo="acme/toolkit", run=fake)
        self.assertIs(provider._run, fake)


class ProtocolConformanceTest(unittest.TestCase):
    def test_github_provider_implements_every_operation(self):
        provider = forge.GitHubProvider(run=FakeGh())
        for name in (
            "viewer_login",
            "list_open_prs",
            "list_comments",
            "post_comment",
            "acknowledge",
        ):
            self.assertTrue(callable(getattr(provider, name)), name)
        self.assertTrue(provider.supports_acknowledge)


if __name__ == "__main__":
    unittest.main()
