#!/usr/bin/env python3
"""Tests for repo_ref.py.

The corpus is the union of what the seven validators this module replaces were
each asserting separately, so a case here is usually a case one of them used to
own. Four properties carry the weight:

* **The host is parsed, never searched for.** `https://evil.example/github.com/o/r`
  and `https://github.com.evil.example/o/r` both contain "github.com", and a
  substring test hands a token request for someone else's repository to Minty.
* **Depth is not the parser's business.** A GitLab `group/subgroup/project`
  parses; only `github_slug` refuses it. That split is the point of the module.
* **A segment can satisfy the character class and still be an instruction.**
  `..` and a leading dash both match `[A-Za-z0-9_.-]+`.
* **A bare slug is not a URL.** `is_github_slug` refuses anything carrying a
  host, because the allowlist callers have already chosen their repository.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import repo_ref  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_bare_shorthand_names_no_host(self):
        ref = repo_ref.parse("acme/toolkit")
        self.assertEqual((ref.host, ref.path), ("", "acme/toolkit"))

    def test_https_url(self):
        ref = repo_ref.parse("https://github.com/acme/toolkit")
        self.assertEqual((ref.host, ref.path), ("github.com", "acme/toolkit"))

    def test_scp_remote(self):
        ref = repo_ref.parse("git@github.com:acme/toolkit.git")
        self.assertEqual((ref.host, ref.path), ("github.com", "acme/toolkit"))

    def test_git_suffix_and_trailing_slash_both_go(self):
        self.assertEqual(repo_ref.parse("acme/toolkit.git/").path, "acme/toolkit")

    def test_userinfo_is_not_the_host(self):
        ref = repo_ref.parse("https://user:token@github.com/acme/toolkit")
        self.assertEqual(ref.host, "github.com")

    def test_host_is_lowercased(self):
        self.assertEqual(repo_ref.parse("https://GitHub.COM/a/b").host, "github.com")

    def test_nested_group_path_parses_at_full_depth(self):
        """A GitLab project. The parser's job is to carry it, not to refuse it."""
        ref = repo_ref.parse("https://gitlab.com/group/subgroup/project")
        self.assertEqual(ref.host, "gitlab.com")
        self.assertEqual(ref.segments, ("group", "subgroup", "project"))

    def test_known_host_in_a_schemeless_value_is_lifted(self):
        ref = repo_ref.parse("github.com/acme/toolkit")
        self.assertEqual((ref.host, ref.path), ("github.com", "acme/toolkit"))

    def test_an_unknown_first_segment_stays_in_the_path(self):
        """`my.org` is a legal owner in the bare form; it must not read as a host."""
        ref = repo_ref.parse("my.org/toolkit")
        self.assertEqual((ref.host, ref.path), ("", "my.org/toolkit"))

    def test_traversal_matches_the_character_class_and_is_still_rejected(self):
        self.assertIsNotNone(repo_ref.SEGMENT_RE.fullmatch(".."))
        with self.assertRaises(repo_ref.RepoRefError):
            repo_ref.parse("../..")

    def test_leading_dash_would_be_parsed_as_a_flag(self):
        with self.assertRaises(repo_ref.RepoRefError):
            repo_ref.parse("-oops/repo")

    def test_empty_and_non_string(self):
        for value in ("", "   ", None, 17, ["acme/toolkit"]):
            with self.subTest(value=value), self.assertRaises(repo_ref.RepoRefError):
                repo_ref.parse(value)

    def test_over_length_is_refused(self):
        with self.assertRaises(repo_ref.RepoRefError):
            repo_ref.parse("a" * repo_ref.MAX_REPO_LENGTH + "/b")

    def test_reason_code_is_machine_readable(self):
        with self.assertRaises(repo_ref.RepoRefError) as ctx:
            repo_ref.parse("../..")
        self.assertEqual(ctx.exception.reason, repo_ref.REASON_UNPARSEABLE)

    def test_try_parse_returns_none_rather_than_raising(self):
        self.assertIsNone(repo_ref.try_parse("../.."))

    def test_str_round_trips_the_host(self):
        self.assertEqual(str(repo_ref.parse("git@gitlab.com:g/p")), "gitlab.com/g/p")
        self.assertEqual(str(repo_ref.parse("acme/toolkit")), "acme/toolkit")


class GithubSlugTest(unittest.TestCase):
    def test_accepts_the_spellings_a_remote_produces(self):
        for value in (
            "acme/toolkit",
            "https://github.com/acme/toolkit",
            "http://github.com/acme/toolkit",
            "https://www.github.com/acme/toolkit",
            "git@github.com:acme/toolkit.git",
            "ssh://git@ssh.github.com/acme/toolkit",
            "github.com/acme/toolkit",
        ):
            with self.subTest(value=value):
                self.assertEqual(repo_ref.github_slug(value), "acme/toolkit")

    def test_github_com_as_a_path_segment_on_another_host_is_rejected(self):
        """The confused-deputy shape every copy of this parser existed to stop."""
        with self.assertRaises(repo_ref.RepoRefError):
            repo_ref.github_slug("https://evil.example/github.com/attacker/repo")

    def test_userinfo_cannot_smuggle_the_host(self):
        with self.assertRaises(repo_ref.RepoRefError):
            repo_ref.github_slug("https://user@evil.example/github.com/a/b")

    def test_lookalike_host_is_rejected(self):
        for value in (
            "https://evilgithub.com/attacker/repo",
            "https://github.com.evil.example/attacker/repo",
        ):
            with self.subTest(value=value), self.assertRaises(repo_ref.RepoRefError):
                repo_ref.github_slug(value)

    def test_a_gitlab_project_is_refused_here_and_not_in_the_parser(self):
        value = "git@gitlab.com:group/project"
        self.assertEqual(repo_ref.parse(value).host, "gitlab.com")
        with self.assertRaises(repo_ref.RepoRefError):
            repo_ref.github_slug(value)

    def test_depth_other_than_two_is_refused(self):
        for value in ("https://github.com/acme", "https://github.com/acme/tool/kit"):
            with self.subTest(value=value), self.assertRaises(repo_ref.RepoRefError):
                repo_ref.github_slug(value)

    def test_a_deep_link_is_refused_rather_than_truncated(self):
        with self.assertRaises(repo_ref.RepoRefError):
            repo_ref.github_slug("https://github.com/acme/toolkit/tree/main")

    def test_hosts_can_be_narrowed_to_the_registration_spelling(self):
        narrow = frozenset({repo_ref.GITHUB_CANONICAL_HOST})
        self.assertEqual(
            repo_ref.github_slug("https://github.com/a/b", hosts=narrow), "a/b"
        )
        with self.assertRaises(repo_ref.RepoRefError):
            repo_ref.github_slug("https://www.github.com/a/b", hosts=narrow)

    def test_require_host_refuses_the_bare_shorthand(self):
        self.assertEqual(repo_ref.github_slug("a/b"), "a/b")
        with self.assertRaises(repo_ref.RepoRefError):
            repo_ref.github_slug("a/b", require_host=True)

    def test_try_variant_returns_none(self):
        self.assertIsNone(repo_ref.try_github_slug("git@gitlab.com:g/p"))
        self.assertEqual(repo_ref.try_github_slug("a/b"), "a/b")


class IsGithubSlugTest(unittest.TestCase):
    def test_bare_two_segment_slug(self):
        self.assertTrue(repo_ref.is_github_slug("acme/toolkit"))

    def test_anything_carrying_a_host_is_not_a_slug(self):
        for value in (
            "https://github.com/acme/toolkit",
            "git@github.com:acme/toolkit",
            "github.com/acme/toolkit",
        ):
            with self.subTest(value=value):
                self.assertFalse(repo_ref.is_github_slug(value))

    def test_traversal_and_flag_shapes(self):
        for value in ("acme/..", "acme/-x", "../..", "-oops/repo"):
            with self.subTest(value=value):
                self.assertFalse(repo_ref.is_github_slug(value))

    def test_the_owner_slot_may_not_be_a_spelling_of_github(self):
        """`github.com/acme` fails on depth; the other two need the host check.

        Only `github.com` is in `KNOWN_HOSTS`, so it alone is lifted out of the
        path and leaves a one-segment remainder. `www.` and `ssh.` stay in the
        path and would otherwise read as an owner — one GitHub cannot issue,
        since a namespace may not contain a dot, and one that reaches Minty as
        an org name if this predicate says yes.
        """
        for value in (
            "github.com/acme",
            "www.github.com/acme",
            "ssh.github.com/acme",
            "GitHub.com/acme",
            "WWW.GitHub.COM/acme",
        ):
            with self.subTest(value=value):
                self.assertFalse(repo_ref.is_github_slug(value))

    def test_an_owner_that_merely_resembles_a_host_is_still_an_owner(self):
        """The refusal is the exact host set, not "looks like a domain".

        `my.org` is a legal GitHub owner in the bare form the operator writes
        through verbatim, and the module docstring's "On the host" section
        turns on that being true.
        """
        for value in ("my.org/repo", "github.io/repo", "notgithub.com/repo"):
            with self.subTest(value=value):
                self.assertTrue(repo_ref.is_github_slug(value))

    def test_wrong_depth(self):
        for value in ("acme", "acme/tool/kit", ""):
            with self.subTest(value=value):
                self.assertFalse(repo_ref.is_github_slug(value))

    def test_non_string(self):
        for value in (None, 17, ["acme/toolkit"]):
            with self.subTest(value=value):
                self.assertFalse(repo_ref.is_github_slug(value))

    def test_a_value_that_only_normalises_to_a_slug_is_not_one(self):
        """`parse` normalises; this predicate's callers keep the raw string.

        `credential_proxy.is_valid_repository` admits the value and its caller
        then execs `github_token_refresh.py` with the original, which splits on
        `/` and sends the left half to Minty as an org name.
        """
        for value in (
            " acme/toolkit ",
            "acme/toolkit\n",
            "acme/toolkit/",
            "/acme/toolkit",
            "/acme/toolkit/",
            "acme/toolkit.git",
            "file:///acme/toolkit",
            "https:///acme/toolkit",
        ):
            with self.subTest(value=value):
                self.assertFalse(repo_ref.is_github_slug(value))


class MalformedUrlTest(unittest.TestCase):
    """`urlsplit` raises a bare `ValueError` on these, which must not escape.

    Every caller of this module catches `RepoRefError` and nothing else, so a
    `ValueError` reaching `get_managed_github_repos` would end the whole sweep
    over one bad ConfigMap entry rather than skipping it.
    """

    MALFORMED = (
        "https://[::1/x",
        "https://[/a/b",
        "http://[abc]:x/a/b",
        "https://a]b/c/d",
    )

    def test_parse_raises_the_modules_own_error(self):
        for value in self.MALFORMED:
            with self.subTest(value=value):
                with self.assertRaises(repo_ref.RepoRefError):
                    repo_ref.parse(value)

    def test_try_parse_returns_none(self):
        for value in self.MALFORMED:
            with self.subTest(value=value):
                self.assertIsNone(repo_ref.try_parse(value))

    def test_the_slug_helpers_stay_total(self):
        for value in self.MALFORMED:
            with self.subTest(value=value):
                self.assertIsNone(repo_ref.try_github_slug(value))
                self.assertFalse(repo_ref.is_github_slug(value))


class KnownHostsTest(unittest.TestCase):
    """The schemeless first-segment lift — see "On the host" in the module."""

    def test_the_canonical_spelling_lifts(self):
        self.assertEqual(repo_ref.parse("github.com/acme/toolkit").host, "github.com")

    def test_a_remote_only_spelling_does_not(self):
        """`KNOWN_HOSTS` is not `GITHUB_HOSTS`: git produces `ssh.github.com`
        in a clone URL, but nobody types it as a shorthand, and lifting it
        would turn a legal three-segment path into a host and two segments."""
        ref = repo_ref.parse("ssh.github.com/acme/toolkit")
        self.assertEqual(ref.host, "")
        self.assertEqual(ref.segments, ("ssh.github.com", "acme", "toolkit"))

    def test_a_bare_owner_that_looks_like_a_host_does_not_lift(self):
        ref = repo_ref.parse("my.org/repo")
        self.assertEqual(ref.host, "")
        self.assertEqual(ref.path, "my.org/repo")


if __name__ == "__main__":
    unittest.main()
