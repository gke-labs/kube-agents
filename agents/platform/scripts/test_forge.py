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
* **Truncation is never silent.** `gh pr list --limit` drops the overflow
  without a word; a full page therefore has to raise rather than be trusted.
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

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
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


def write_settings(tmpdir: str, value: str) -> str:
    path = os.path.join(tmpdir, "SETTINGS.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"# Settings\n\n- **Git Repo:** {value}\n")
    return path


class TargetRepoTest(unittest.TestCase):
    def _resolve(self, value):
        with tempfile.TemporaryDirectory() as tmp:
            return forge.target_repo(write_settings(tmp, value))

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

    def test_unset_literal_is_absent_not_a_fault(self):
        self.assertIsNone(self._resolve("none"))

    def test_missing_file_is_absent(self):
        self.assertIsNone(forge.target_repo("/nonexistent/SETTINGS.md"))

    def test_file_without_a_git_repo_line_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "SETTINGS.md")
            Path(path).write_text("# Settings\n\nnothing here\n", encoding="utf-8")
            self.assertIsNone(forge.target_repo(path))

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

    def test_bold_delimiters_around_the_value_are_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "SETTINGS.md")
            Path(path).write_text(
                "- **Git Repo:** **acme/toolkit**\n", encoding="utf-8"
            )
            self.assertEqual(forge.target_repo(path), "acme/toolkit")


class ParserAgreementTest(unittest.TestCase):
    """`forge._parse_repo` and `resolver.get_target_repo` must not drift.

    Delete this test — and `forge._parse_repo` — when `resolver.py` migrates
    onto this module. Until then it is the only thing keeping one hardened
    parser from quietly becoming two different ones.
    """

    CORPUS = (
        "acme/toolkit",
        "acme/toolkit.git",
        "https://github.com/acme/toolkit",
        "https://www.github.com/acme/toolkit",
        "http://github.com/acme/toolkit.git",
        "git@github.com:acme/toolkit.git",
        "ssh://git@github.com/acme/toolkit",
        "https://evil.com/github.com/attacker/repo",
        "https://user@evil.com/github.com/attacker/repo",
        "https://evilgithub.com/attacker/repo",
        "../..",
        "-oops/repo",
        "not a repo at all",
        "acme/toolkit/extra",
    )

    @classmethod
    def setUpClass(cls):
        here = Path(__file__).resolve().parent
        path = (
            here.parent
            / "skills"
            / "github-issue-resolver"
            / "scripts"
            / "resolver.py"
        )
        # Asserted rather than skipped: a moved resolver.py silently disabling
        # the drift guard is the failure this test exists to prevent.
        assert path.exists(), f"resolver.py not found at {path}"
        spec = importlib.util.spec_from_file_location("_resolver_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.resolver = module

    def _forge_result(self, value):
        try:
            return forge._parse_repo(value)
        except forge.RepoUnparseable:
            return "UNPARSEABLE"

    def _resolver_result(self, value):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_settings(tmp, value)
            try:
                return self.resolver.get_target_repo(
                    required=False, settings_path=path
                )
            except self.resolver.RepoUnparseable:
                return "UNPARSEABLE"

    def test_both_parsers_agree_over_the_corpus(self):
        for value in self.CORPUS:
            with self.subTest(value=value):
                self.assertEqual(
                    self._forge_result(value),
                    self._resolver_result(value),
                    f"parsers disagree on {value!r}",
                )


class NormaliseLoginTest(unittest.TestCase):
    def test_bot_suffix_is_stripped(self):
        self.assertEqual(forge.normalise_login("kube-agents-bot[bot]"), "kube-agents-bot")

    def test_case_is_folded(self):
        self.assertEqual(forge.normalise_login("Kube-Agents-Bot"), "kube-agents-bot")

    def test_rest_and_graphql_spellings_converge(self):
        """The whole point: the two APIs disagree, the comparison must not."""
        self.assertEqual(
            forge.normalise_login("kube-agents-bot[bot]"),
            forge.normalise_login("kube-agents-bot"),
        )

    def test_empty_is_tolerated(self):
        self.assertEqual(forge.normalise_login(""), "")
        self.assertEqual(forge.normalise_login(None), "")


class PullRequestTest(unittest.TestCase):
    def _pr(self, head_ref="platform-agent/fix-1", labels=()):
        return forge.PullRequest(
            number=7, head_ref=head_ref, author="kube-agents-bot[bot]", labels=labels
        )

    def test_agent_branch_prefix_identifies_our_own_pr(self):
        self.assertTrue(self._pr().is_agent_authored)

    def test_a_human_branch_is_not_agent_authored(self):
        self.assertFalse(self._pr(head_ref="feat/whatever").is_agent_authored)

    def test_a_branch_merely_containing_the_prefix_does_not_count(self):
        self.assertFalse(self._pr(head_ref="wip/platform-agent/x").is_agent_authored)

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
        self.assertNotEqual(result.returncode, 0)
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


class PreflightTest(unittest.TestCase):
    def test_authenticated_passes(self):
        forge.gh_preflight(FakeGh(default=(0, "", "")))

    def test_missing_binary_is_distinguished_from_missing_auth(self):
        with self.assertRaises(forge.ForgeError) as ctx:
            forge.gh_preflight(FakeGh(default=(forge.GH_MISSING_RC, "", "")))
        self.assertEqual(ctx.exception.reason, "GH_CLI_NOT_FOUND")

    def test_unauthenticated_reports_its_own_reason(self):
        with self.assertRaises(forge.ForgeError) as ctx:
            forge.gh_preflight(FakeGh(default=(1, "", "not logged in")))
        self.assertEqual(ctx.exception.reason, "GITHUB_AUTH_NOT_CONFIGURED")


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


PRS_JSON = json.dumps(
    [
        {
            "number": 12,
            "headRefName": "platform-agent/bump-replicas",
            "author": {"login": "kube-agents-bot"},
            "labels": [{"name": "automated"}],
            "url": "https://github.com/acme/toolkit/pull/12",
        },
        {
            "number": 13,
            "headRefName": "feat/human-work",
            "author": {"login": "someone"},
            "labels": [],
            "url": "https://github.com/acme/toolkit/pull/13",
        },
    ]
)


class ListOpenPrsTest(unittest.TestCase):
    def test_rows_are_normalised(self):
        provider = forge.GitHubProvider(run=FakeGh({"pr list": (0, PRS_JSON, "")}))
        prs = provider.list_open_prs("acme/toolkit")
        self.assertEqual([p.number for p in prs], [12, 13])
        self.assertEqual(prs[0].head_ref, "platform-agent/bump-replicas")
        self.assertEqual(prs[0].labels, ("automated",))
        self.assertTrue(prs[0].is_agent_authored)
        self.assertFalse(prs[1].is_agent_authored)

    def test_argv_scopes_the_repo_and_asks_only_for_open_prs(self):
        fake = FakeGh({"pr list": (0, PRS_JSON, "")})
        forge.GitHubProvider(run=fake).list_open_prs("acme/toolkit")
        argv = fake.argv_containing("pr list")
        self.assertIn("-R", argv)
        self.assertEqual(argv[argv.index("-R") + 1], "acme/toolkit")
        self.assertEqual(argv[argv.index("--state") + 1], "open")

    def test_a_full_page_raises_rather_than_truncating_silently(self):
        rows = json.dumps(
            [
                {
                    "number": n,
                    "headRefName": f"platform-agent/x{n}",
                    "author": {"login": "bot"},
                    "labels": [],
                }
                for n in range(forge.PR_PAGE_LIMIT)
            ]
        )
        provider = forge.GitHubProvider(run=FakeGh({"pr list": (0, rows, "")}))
        with self.assertRaises(forge.ForgeError) as ctx:
            provider.list_open_prs("acme/toolkit")
        self.assertEqual(ctx.exception.reason, "PR_PAGE_TRUNCATED")

    def test_missing_fields_do_not_crash_the_sweep(self):
        provider = forge.GitHubProvider(run=FakeGh({"pr list": (0, "[{}]", "")}))
        prs = provider.list_open_prs("acme/toolkit")
        self.assertEqual(prs[0].number, 0)
        self.assertEqual(prs[0].author, "")
        self.assertFalse(prs[0].is_agent_authored)


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


def comments_fake():
    return FakeGh(
        {
            "issues/12/comments": (0, ISSUE_COMMENTS, ""),
            "pulls/12/comments": (0, REVIEW_COMMENTS, ""),
            "pulls/12/reviews": (0, REVIEWS, ""),
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
        """The default page is 30 and a truncated list looks complete."""
        fake = comments_fake()
        forge.GitHubProvider(run=fake).list_comments("acme/toolkit", self.pr)
        for argv in fake.calls:
            self.assertIn("--paginate", argv, f"missing --paginate in {argv}")

    def test_results_are_ordered_oldest_first(self):
        """The per-tick cap takes the oldest, so newer requests cannot starve older ones."""
        comments = forge.GitHubProvider(run=comments_fake()).list_comments(
            "acme/toolkit", self.pr
        )
        self.assertEqual(
            [c.node_id for c in comments], ["IC_b", "IC_a", "PRRC_a", "PRR_a"]
        )

    def test_write_association_becomes_a_boolean(self):
        by_id = {
            c.node_id: c
            for c in forge.GitHubProvider(run=comments_fake()).list_comments(
                "acme/toolkit", self.pr
            )
        }
        self.assertTrue(by_id["IC_a"].can_write)  # COLLABORATOR
        self.assertTrue(by_id["PRRC_a"].can_write)  # MEMBER
        self.assertTrue(by_id["PRR_a"].can_write)  # OWNER
        self.assertFalse(by_id["IC_b"].can_write)  # NONE

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
    def test_body_is_passed_as_a_file_never_on_the_command_line(self):
        """A reviewer's words go back through a proxy and two shells' quoting."""
        fake = FakeGh(default=(0, "", ""))
        pr = forge.PullRequest(number=12, head_ref="platform-agent/x", author="bot")
        forge.GitHubProvider(run=fake).post_comment(
            "acme/toolkit", pr, "/opt/data/scratch/pr_12.md"
        )
        argv = fake.argv_containing("pr comment")
        self.assertIn("--body-file", argv)
        self.assertNotIn("--body", argv)
        self.assertEqual(argv[argv.index("--body-file") + 1], "/opt/data/scratch/pr_12.md")
        self.assertEqual(argv[argv.index("-R") + 1], "acme/toolkit")

    def test_a_failed_post_is_not_swallowed(self):
        fake = FakeGh(default=(1, "", "HTTP 403"))
        pr = forge.PullRequest(number=12, head_ref="platform-agent/x", author="bot")
        with self.assertRaises(forge.ForgeError):
            forge.GitHubProvider(run=fake).post_comment("acme/toolkit", pr, "/tmp/x.md")


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


class SelfLoginTest(unittest.TestCase):
    def test_taken_from_the_pr_author_and_normalised(self):
        pr = forge.PullRequest(
            number=1, head_ref="platform-agent/x", author="Kube-Agents-Bot[bot]"
        )
        provider = forge.GitHubProvider(run=FakeGh())
        self.assertEqual(provider.self_login(pr), "kube-agents-bot")


class ProviderForTest(unittest.TestCase):
    def test_github_host_selects_the_github_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_settings(tmp, "https://github.com/acme/toolkit")
            self.assertIsInstance(forge.provider_for(path), forge.GitHubProvider)

    def test_bare_shorthand_means_github(self):
        """The operator writes `owner/repo` through verbatim; it is `gh -R`'s own form."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_settings(tmp, "acme/toolkit")
            self.assertIsInstance(forge.provider_for(path), forge.GitHubProvider)

    def test_a_missing_settings_file_still_yields_a_provider(self):
        self.assertIsInstance(
            forge.provider_for("/nonexistent/SETTINGS.md"), forge.GitHubProvider
        )

    def test_the_run_seam_is_forwarded_to_the_provider(self):
        fake = FakeGh()
        with tempfile.TemporaryDirectory() as tmp:
            path = write_settings(tmp, "acme/toolkit")
            provider = forge.provider_for(path, run=fake)
        self.assertIs(provider._run, fake)


class ProtocolConformanceTest(unittest.TestCase):
    def test_github_provider_implements_every_operation(self):
        provider = forge.GitHubProvider(run=FakeGh())
        for name in (
            "self_login",
            "list_open_prs",
            "list_comments",
            "post_comment",
            "acknowledge",
        ):
            self.assertTrue(callable(getattr(provider, name)), name)
        self.assertTrue(provider.supports_acknowledge)


if __name__ == "__main__":
    unittest.main()
