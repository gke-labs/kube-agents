#!/usr/bin/env python3
"""Unit tests for the reviewer request gate.

Run: cd scripts && python3 -m unittest test_request_reviewers

Two classes of behaviour are worth testing here, and both fail *green*.

The glob matching is a port of minimatch, and the rule that decides this
repository's config -- `*` and `**` never match a leading dot -- is the one a
reimplementation quietly gets wrong. Getting it wrong does not raise; it just
routes `.github/**` changes to a reviewer group nobody chose.

The gates decide whether a human is pinged at all. A gate that stops matching
means either reviewers are never requested (silence that looks like a quiet
week) or requested on every completed check, which is what this change exists
to stop.
"""

import contextlib
import io
import json
import os
import random
import re
import sys
import tempfile
import unittest

import yaml
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import request_reviewers as rr

# The live config at .github/auto_request_review.yml, in the order that file
# lists the globs -- `last_files_match_only` makes that order the deciding
# input, so a test fixture that reorders them tests nothing.
CONFIG = {
    "reviewers": {
        "defaults": ["repository-owners", "repository-reviewers"],
        "groups": {
            "repository-owners": ["bradhoekstra", "jayantid", "toshiowang", "dshnayder", "bnaylor", "haoxuw"],
            "repository-reviewers": ["lapis2002", "stalhaali", "Fuxiao-Gao"],
            "eval-crew": ["jayantid", "lapis2002"],
        },
    },
    "files": {
        "**": ["repository-owners", "repository-reviewers"],
        "hack/eval/presubmit-cases.txt": ["eval-crew"],
        "hack/eval/blocking-roster.txt": ["eval-crew"],
    },
    "options": {
        "ignore_draft": True,
        "ignored_keywords": ["DO NOT REVIEW"],
        "enable_group_assignment": False,
        "number_of_reviewers": 1,
        "last_files_match_only": True,
        "robot_accounts": ["kyber775"],
    },
}

OWNERS = CONFIG["reviewers"]["groups"]["repository-owners"]
REVIEWERS = CONFIG["reviewers"]["groups"]["repository-reviewers"]
EVAL_CREW = CONFIG["reviewers"]["groups"]["eval-crew"]
# What an ordinary change draws from: the approvers and the non-approver
# reviewers, in config order, before the author is removed and the pool is
# narrowed for an author whose own approval does not cover the change.
POOL = OWNERS + REVIEWERS
REPO_ROOT = _HERE.parent
LIVE_CONFIG = REPO_ROOT / rr.DEFAULT_CONFIG_PATH

# The OWNERS approvers the verdict check is handed in these tests, plus one
# login outside them. The set mirrors the `repository-owners` group. What the
# fixtures below depend on is two memberships rather than the exact names:
# `jayantid` is inside it, so an `APPROVED` review from him reads as an
# approval, and `NON_APPROVER` is outside it, so the same review from him
# does not.
APPROVERS = {"bradhoekstra", "jayantid", "toshiowang", "dshnayder", "bnaylor", "haoxuw"}
NON_APPROVER = "outside-contributor"

# The root OWNERS, OWNERS_ALIASES and hack/OWNERS as they stand, for the walk
# tests that need a tree they can also mutate.
OWNERS_TREE = {
    "OWNERS": "approvers:\n- AntonTyb\n- bradhoekstra\n- jayantid\nreviewers:\n- bradhoekstra\n- lapis2002\n- stalhaali\n",
    "OWNERS_ALIASES": "aliases:\n  eval-crew:\n    - jayantid\n    - lapis2002\n",
    "hack/OWNERS": (
        "filters:\n"
        "  'eval/(presubmit-cases|blocking-roster)\\.txt$':\n"
        "    approvers:\n"
        "    - eval-crew\n"
        "options:\n"
        "  no_parent_owners: true\n"
    ),
}
ROOT_APPROVERS = {"antontyb", "bradhoekstra", "jayantid"}
ROOT_REVIEWERS = {"bradhoekstra", "lapis2002", "stalhaali"}


def write_tree(root, files):
    for relative, content in files.items():
        path = Path(root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def pull_request(**overrides):
    base = {
        "number": 1,
        "state": "open",
        "draft": False,
        "title": "feat: something",
        "user": {"login": "author", "type": "User"},
        "requested_reviewers": [],
        "requested_teams": [],
        "head": {"sha": "deadbeef"},
    }
    base.update(overrides)
    return base


def check_run(conclusion="success", **overrides):
    base = {
        "id": 1,
        "name": rr.AI_REVIEW_CHECK_NAME,
        "app": {"id": rr.AI_REVIEW_APP_ID},
        "status": "completed",
        "conclusion": conclusion,
        "started_at": "2026-08-17T13:07:42Z",
        "output": {"title": "No findings"},
    }
    base.update(overrides)
    return base


def review(login, state="COMMENTED", user_type="User", submitted_at=None, commit_id=None):
    filed = {"user": {"login": login, "type": user_type}, "state": state}
    if submitted_at is not None:
        filed["submitted_at"] = submitted_at
    if commit_id is not None:
        filed["commit_id"] = commit_id
    return filed


def bot_review(commit_id):
    """One of the bot's own reviews, as the reviews listing returns it."""
    return review(rr.AI_REVIEW_BOT_LOGIN, user_type="Bot", commit_id=commit_id)


def tally(outcome="findings", **counts):
    """The last line of a finished `AI Review` entry's summary, as the bot writes it."""
    values = {
        "outcome": outcome, "high": 0, "medium": 0, "low": 0, "other": 0, "folded": 0,
        "near_miss": 0, "standing": 0, "advisory": 0, "description": False, "partial": False,
    }
    values.update(counts)
    return f"{rr.AI_REVIEW_TALLY_PREFIX} {json.dumps(values, separators=(',', ':'))}"


def tallied(conclusion="neutral", title="Found 2 issues", outcome="findings", **overrides):
    """A finished review entry: the tally is the last line of its summary."""
    summary = f"One automated read.\n\n{tally(outcome)}"
    return check_run(conclusion, output={"title": title, "summary": summary}, **overrides)


class FakeAPI:
    """Just enough of `GitHubAPI` for the functions that call it, `main` included."""

    def __init__(
        self, pulls=(), commits=None, check_runs=None, reviews=None, files=None, comments=None, check_runs_by_id=None
    ):
        self.repo = "gke-labs/kube-agents"
        self.pulls = list(pulls)
        self.commits = commits or {}
        self.check_runs = check_runs or {}
        self.reviews = reviews or {}
        self.files = files or {}
        self.comments = comments or {}
        self.check_runs_by_id = check_runs_by_id or {}
        self.posts = []
        self.lists = []

    def get_all(self, path):
        self.lists.append(path)
        if any(path.endswith(suffix) for suffix in getattr(self, "failing_lists", ())):
            raise RuntimeError(f"HTTP 502 on {path}")
        if path.endswith("/pulls?state=open"):
            return self.pulls
        matched = re.search(r"/issues/(\d+)/comments$", path)
        if matched:
            return self.comments.get(int(matched.group(1)), [])
        matched = re.search(r"/pulls/(\d+)/(commits|reviews|files)$", path)
        if matched:
            number, kind = int(matched.group(1)), matched.group(2)
            if kind == "commits":
                return [{"sha": sha} for sha in self.commits.get(number, [])]
            if kind == "reviews":
                return self.reviews.get(number, [])
            return [{"filename": name} for name in self.files.get(number, [])]
        raise AssertionError(f"unexpected list call: {path}")

    def get(self, path):
        matched = re.search(r"/check-runs/(\d+)$", path)
        if matched:
            return self.check_runs_by_id[int(matched.group(1))]
        matched = re.search(r"/commits/([0-9a-f]+)/check-runs$", path)
        if matched:
            return {"check_runs": self.check_runs.get(matched.group(1), [])}
        matched = re.search(r"/pulls/(\d+)$", path)
        if matched:
            return next(pull for pull in self.pulls if pull["number"] == int(matched.group(1)))
        raise AssertionError(f"unexpected call: {path}")

    def post(self, path, payload=None):
        if path in getattr(self, "failing", ()):
            raise RuntimeError(f"HTTP 502 on {path}")
        self.posts.append((path, payload))
        return {}


class GlobTest(unittest.TestCase):
    """`glob_to_regex` -- the minimatch subset."""

    def assert_matches(self, pattern, path):
        self.assertTrue(rr.glob_to_regex(pattern).match(path), f"{pattern!r} should match {path!r}")

    def assert_no_match(self, pattern, path):
        self.assertIsNone(rr.glob_to_regex(pattern).match(path), f"{pattern!r} should not match {path!r}")

    def test_double_star_matches_any_depth(self):
        self.assert_matches("**", "README.md")
        self.assert_matches("**", "docs/site/src/content/docs/contributing.md")

    def test_double_star_does_not_match_a_dot_segment(self):
        # The dotfile rule: `**` does not reach `.github/workflows/...`, so a
        # dotfile path is routed only by a literal entry or by the defaults.
        self.assert_no_match("**", ".github/workflows/validate.yml")
        self.assert_no_match("**", ".gitignore")
        self.assert_no_match("k8s-operator/**", "k8s-operator/.golangci.yml")

    def test_prefixed_double_star(self):
        self.assert_matches("k8s-operator/**", "k8s-operator/main.go")
        self.assert_matches("k8s-operator/**", "k8s-operator/internal/controller/pa.go")
        self.assert_no_match("k8s-operator/**", "scripts/main.go")
        self.assert_no_match("k8s-operator/**", "k8s-operator")

    def test_middle_double_star_spans_zero_segments(self):
        self.assert_matches("a/**/b.go", "a/b.go")
        self.assert_matches("a/**/b.go", "a/x/y/b.go")

    def test_single_star_stops_at_a_slash(self):
        self.assert_matches(".github/workflows/staging-redeploy-agent.yml", ".github/workflows/staging-redeploy-agent.yml")
        self.assert_matches(".github/workflows/staging-redeploy-*.yml", ".github/workflows/staging-redeploy-controller.yml")
        self.assert_no_match(".github/workflows/staging-redeploy-*.yml", ".github/workflows/nested/staging-redeploy-x.yml")
        self.assert_no_match("*.md", "docs/README.md")

    def test_question_mark_matches_one_character(self):
        self.assert_matches("v?.md", "v1.md")
        self.assert_no_match("v?.md", "v10.md")

    def test_unsupported_syntax_raises_rather_than_guessing(self):
        for pattern in ("!(a).md", "{a,b}.md", "[abc].md", "+(a|b).md"):
            with self.assertRaises(ValueError, msg=pattern):
                rr.glob_to_regex(pattern)


class ConfigValidationTest(unittest.TestCase):
    """`validate_config` -- refuse what the port does not implement."""

    def test_the_live_config_is_accepted(self):
        rr.validate_config(CONFIG)

    def test_the_fixture_is_the_live_config(self):
        # The docstring above promises the fixture mirrors the file, order
        # included. Nothing else would notice the two drifting apart. Dict
        # equality is order-blind, so the glob order is compared on its own:
        # it is the input `last_files_match_only` decides on.
        live = rr.load_config(LIVE_CONFIG)
        self.assertEqual(live, CONFIG)
        self.assertEqual(list(live["files"]), list(CONFIG["files"]))

    def test_per_author_is_refused(self):
        config = {"reviewers": {"per_author": {"alice": ["bob"]}}}
        with self.assertRaises(ValueError) as caught:
            rr.validate_config(config)
        self.assertIn("per_author", str(caught.exception))

    def test_group_assignment_is_refused_only_when_enabled(self):
        rr.validate_config({"options": {"enable_group_assignment": False}})
        with self.assertRaises(ValueError):
            rr.validate_config({"options": {"enable_group_assignment": True}})

    def test_an_unsupported_glob_in_the_files_map_is_refused(self):
        with self.assertRaises(ValueError):
            rr.validate_config({"files": {"{a,b}/**": ["repository-owners"]}})

    def test_robot_accounts_must_be_a_list_of_logins(self):
        # Both comparisons the list feeds are exact against GitHub's `login`,
        # so a decorated or mistyped entry would pass a non-empty check and
        # then match nothing, silently: the shape is what is validated. Only
        # the shape: what comes after the first character is GitHub's to rule
        # on, and an Enterprise Managed User's `handle_shortcode` or an older
        # account's underscore is a login GitHub issued.
        for good in (["kyber775"], ["a"], ["k-y-b-3"], ["A" * 39], ["a" + "-b" * 19], ["reviewbot_acme"], ["legacy_user"], ["kyber-"], ["ky--ber"]):
            rr.validate_config({"options": {"robot_accounts": good}})
        rr.validate_config({"options": {}})
        # The hyphenated lengths matter: a pattern that counts repetitions rather
        # than characters lets `a-b-b-…` run to 77 characters.
        for bad in ("kyber775", [{"login": "kyber775"}], [""], [7], ["@kyber775"], ["kyber775 "], ["kyber775[bot]"], ["org/kyber775"], ["kyber.bot"], ["-kyber"], ["_kyber"], ["A" * 40], ["a" + "-b" * 20], ["a" + "-b" * 38]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                rr.validate_config({"options": {"robot_accounts": bad}})
        self.assertEqual(rr.robot_accounts({"options": {"robot_accounts": ["Kyber775"]}}), {"kyber775"})
        self.assertEqual(rr.robot_accounts({}), frozenset())

    def test_a_yaml_coerced_login_is_refused_with_the_remedy(self):
        # An unquoted all-digit login arrives as an int and an unquoted YAML
        # word as a bool; both are valid logins once quoted, so the message
        # says to quote rather than calling a valid login malformed.
        for entry in ("12345", "no", "on", "true", "2024-01-01", "null"):
            with self.subTest(entry=entry):
                config = yaml.safe_load(f"options:\n  robot_accounts:\n    - {entry}\n")
                with self.assertRaises(ValueError) as caught:
                    rr.validate_config(config)
                self.assertIn("quote", str(caught.exception))
                self.assertNotIn("not a bare GitHub login", str(caught.exception))
                quoted = yaml.safe_load(f'options:\n  robot_accounts:\n    - "{entry}"\n')
                rr.validate_config(quoted)
        # `~` and an empty item arrive as None too; quoted `~` is no login, so
        # only the message is checked for those.
        for entry in ("~", ""):
            with self.subTest(entry=entry), self.assertRaises(ValueError) as caught:
                rr.validate_config(yaml.safe_load(f"options:\n  robot_accounts:\n    - {entry}\n"))
            self.assertIn("remove an empty entry", str(caught.exception))
            self.assertNotIn("not a mapping or a list", str(caught.exception))
        # A mapping or a nested list is a different mistake with a different
        # remedy: quoting does nothing for it, so the message says the shape.
        for entry in ([{"login": "kyber775"}], [["kyber775"]]):
            with self.subTest(entry=entry), self.assertRaises(ValueError) as caught:
                rr.validate_config({"options": {"robot_accounts": entry}})
            self.assertIn("not a mapping or a list", str(caught.exception))
            self.assertNotIn("quote", str(caught.exception))
        # Quoting keeps what the operator typed: `007` is a login, `7` is not it.
        self.assertEqual(rr.robot_accounts(yaml.safe_load('options:\n  robot_accounts:\n    - "007"\n')), {"007"})


class SelectionTest(unittest.TestCase):
    """`select_reviewers` and the two functions under it."""

    def select(self, changed_files, author="author"):
        return rr.select_reviewers(CONFIG, changed_files, author, rng=random.Random(0))

    def test_ordinary_change_falls_to_the_catch_all_group(self):
        matched = rr.reviewers_by_changed_files(CONFIG, ["README.md"], "author")
        self.assertEqual(matched, POOL)

    def test_a_literal_dot_entry_still_matches(self):
        # The live config names no dotfile path today, but the port has to
        # honour a literal entry when one exists: `**` cannot stand in for it.
        config = dict(CONFIG, files={**CONFIG["files"], ".github/workflows/validate.yml": ["eval-crew"]})
        matched = rr.reviewers_by_changed_files(config, [".github/workflows/validate.yml"], "author")
        self.assertEqual(matched, EVAL_CREW)

    def test_dotfile_only_change_matches_no_glob_and_uses_defaults(self):
        # `**` cannot reach `.github/workflows/validate.yml`, so nothing matches
        # and the defaults carry it.
        self.assertEqual(rr.reviewers_by_changed_files(CONFIG, [".github/workflows/validate.yml"], "author"), [])
        self.assertEqual(self.select([".github/workflows/validate.yml"])[0] in POOL, True)

    def test_a_presubmit_roster_change_goes_to_eval_crew(self):
        # Only eval-crew can /approve hack/eval/presubmit-cases.txt and
        # blocking-roster.txt (hack/OWNERS, no_parent_owners), so a random root
        # owner would review a pull request they cannot clear, and nothing
        # would tell eval-crew it exists.
        for path in ("hack/eval/presubmit-cases.txt", "hack/eval/blocking-roster.txt"):
            with self.subTest(path=path):
                self.assertEqual(rr.reviewers_by_changed_files(CONFIG, [path], "author"), EVAL_CREW)
                self.assertIn(self.select([path])[0], EVAL_CREW)

    def test_the_nightly_file_a_case_and_the_script_stay_with_root(self):
        # Decision A (#1546, 2026-09-15): the nightly file, a new case directory
        # and ci-eval-pr.sh itself fall through to the root OWNERS, so they
        # route to the default reviewers like any other change.
        for path in ("hack/eval/nightly-cases.txt", "bench/tasks/new-case/task.yaml", "hack/ci-eval-pr.sh", "hack/ci-deploy.sh"):
            with self.subTest(path=path):
                self.assertEqual(rr.reviewers_by_changed_files(CONFIG, [path], "author"), POOL)

    def test_a_mixed_change_still_goes_to_eval_crew(self):
        # Last match wins, and eval-crew is listed last: the reviewer who can
        # clear the roster half is asked. Not every member is a root owner, so
        # the README half may still wait on a root approver's /approve; the
        # alternative, a random root owner who cannot clear the roster at all,
        # is the gap this entry closes.
        matched = rr.reviewers_by_changed_files(CONFIG, ["README.md", "hack/eval/presubmit-cases.txt"], "author")
        self.assertEqual(matched, EVAL_CREW)

    def test_eval_crews_own_roster_change_goes_to_the_other_member(self):
        # The author is never requested, so a member's own roster change goes
        # to the rest of the group; the author's approved is already on it (#1075).
        author = EVAL_CREW[0]
        matched = rr.reviewers_by_changed_files(CONFIG, ["hack/eval/blocking-roster.txt"], author)
        self.assertEqual(matched, [name for name in EVAL_CREW if name != author])
        self.assertEqual(self.select(["hack/eval/blocking-roster.txt"], author=author), matched)

    def test_a_roster_change_by_the_whole_group_falls_back_to_the_defaults(self):
        # Only the author is excluded, so this needs a one-member group: the
        # shape the config had before lapis2002 joined, and the shape it has
        # again if the alias ever shrinks. The defaults carry it to a root
        # owner, whose review sets lgtm.
        config = dict(CONFIG, reviewers=dict(CONFIG["reviewers"], groups=dict(CONFIG["reviewers"]["groups"], **{"eval-crew": ["jayantid"]})))
        self.assertEqual(rr.reviewers_by_changed_files(config, ["hack/eval/presubmit-cases.txt"], "jayantid"), [])
        picked = rr.select_reviewers(config, ["hack/eval/presubmit-cases.txt"], "jayantid", rng=random.Random(0))
        self.assertEqual(len(picked), 1)
        self.assertIn(picked[0], [name for name in POOL if name != "jayantid"])

    def test_the_author_is_never_requested(self):
        matched = rr.reviewers_by_changed_files(CONFIG, ["README.md"], "bradhoekstra")
        self.assertNotIn("bradhoekstra", matched)
        self.assertEqual(matched, [name for name in POOL if name != "bradhoekstra"])

    def test_number_of_reviewers_caps_the_request(self):
        picked = self.select(["README.md"])
        self.assertEqual(len(picked), 1)
        self.assertIn(picked[0], POOL)

    def test_sampling_is_reproducible_for_a_given_seed(self):
        first = rr.select_reviewers(CONFIG, ["README.md"], "author", rng=random.Random(7))
        second = rr.select_reviewers(CONFIG, ["README.md"], "author", rng=random.Random(7))
        self.assertEqual(first, second)

    def test_fewer_candidates_than_requested_is_not_an_error(self):
        # One more than the group holds, so the request stays short of the
        # candidates however many names the group grows to.
        config = dict(CONFIG, options=dict(CONFIG["options"], number_of_reviewers=len(POOL) + 1))
        picked = rr.select_reviewers(config, ["README.md"], "author", rng=random.Random(0))
        self.assertCountEqual(picked, POOL)

    def test_a_restriction_narrows_the_pool_to_the_logins_given(self):
        # The pool for a pull request whose author cannot self-approve: the
        # non-approver reviewers would leave it with lgtm and nobody asked for
        # the /approve, so only the OWNERS approvers for the change are drawn.
        config = dict(CONFIG, options=dict(CONFIG["options"], number_of_reviewers=None))
        approvers = {name.lower() for name in OWNERS}
        picked = rr.select_reviewers(config, ["README.md"], "author", rng=random.Random(0), restrict_to=approvers)
        self.assertEqual(picked, OWNERS)
        for seed in range(20):
            picked = rr.select_reviewers(CONFIG, ["README.md"], "author", rng=random.Random(seed), restrict_to=approvers)
            self.assertIn(picked[0], OWNERS, seed)

    def test_no_restriction_draws_from_the_whole_pool(self):
        # An approver's own pull request is self-approved and needs lgtm
        # alone, which any name in the pool can give: equal weight, no
        # preference either way.
        picked = {rr.select_reviewers(CONFIG, ["README.md"], "bradhoekstra", rng=random.Random(seed))[0] for seed in range(60)}
        self.assertEqual(picked, set(POOL) - {"bradhoekstra"})

    def test_a_restriction_matches_logins_case_insensitively(self):
        # OWNERS logins come out of the walk lower-cased; the config spells
        # them as GitHub shows them.
        config = dict(CONFIG, options=dict(CONFIG["options"], number_of_reviewers=None))
        picked = rr.select_reviewers(config, ["README.md"], "author", rng=random.Random(0), restrict_to={"fuxiao-gao"})
        self.assertEqual(picked, ["Fuxiao-Gao"])

    def test_a_restriction_that_empties_the_pool_falls_back_to_the_whole_of_it(self):
        # Nobody in the matched pool can /approve: a config shape, not a
        # pull-request one. Asking someone who can lgtm beats asking nobody,
        # and the run log says the pool was not narrowed.
        config = dict(CONFIG, options=dict(CONFIG["options"], number_of_reviewers=None))
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            picked = rr.select_reviewers(config, ["README.md"], "author", rng=random.Random(0), restrict_to={"nobody"})
        self.assertEqual(picked, POOL)
        self.assertIn("none of them", stderr.getvalue())

    def test_teams_are_split_from_users(self):
        users, teams = rr.split_teams(["bradhoekstra", "team:sre"])
        self.assertEqual(users, ["bradhoekstra"])
        self.assertEqual(teams, ["sre"])


class OwnersTest(unittest.TestCase):
    """`applicable_approvers` -- Prow's approver walk over the OWNERS files."""

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        write_tree(self.root.name, OWNERS_TREE)

    def approvers(self, *changed):
        return rr.applicable_approvers(list(changed), self.root.name)

    def test_a_root_file_gets_the_root_approvers_lower_cased(self):
        # GitHub logins are case-insensitive and OWNERS spells `AntonTyb` in
        # mixed case; the reviews API may spell it either way.
        self.assertEqual(self.approvers("README.md"), ROOT_APPROVERS)

    def test_a_nested_file_walks_up_to_the_root(self):
        self.assertEqual(self.approvers("k8s-operator/internal/controller/pa.go"), ROOT_APPROVERS)

    def test_a_filtered_path_under_no_parent_owners_gets_only_the_filter(self):
        # hack/OWNERS: only eval-crew can /approve the presubmit rosters, and a
        # root approver does not count (#1546).
        for path in ("hack/eval/presubmit-cases.txt", "hack/eval/blocking-roster.txt"):
            with self.subTest(path=path):
                self.assertEqual(self.approvers(path), {"jayantid", "lapis2002"})

    def test_an_unfiltered_path_under_no_parent_owners_falls_through_to_the_root(self):
        # The same file's comment: everything else under hack/ matches no
        # filter and falls through, so no_parent_owners only bites on a match.
        for path in ("hack/eval/nightly-cases.txt", "hack/ci-eval-pr.sh"):
            with self.subTest(path=path):
                self.assertEqual(self.approvers(path), ROOT_APPROVERS)

    def test_the_reviewer_walk_reads_the_other_list(self):
        self.assertEqual(rr.applicable_reviewers(["README.md"], self.root.name), ROOT_REVIEWERS)

    def test_the_reviewer_walk_falls_through_no_parent_owners_that_names_no_reviewers(self):
        # Prow stops a walk at no_parent_owners only once *that* walk has
        # collected something, and hack/OWNERS names no reviewers, so a
        # roster-only change takes its lgtm from the root reviewers as well.
        for path in ("hack/eval/presubmit-cases.txt", "hack/eval/blocking-roster.txt"):
            with self.subTest(path=path):
                self.assertEqual(rr.applicable_reviewers([path], self.root.name), ROOT_REVIEWERS)

    def test_a_filter_with_reviewers_of_its_own_stops_the_reviewer_walk(self):
        write_tree(
            self.root.name,
            {"hack/OWNERS": OWNERS_TREE["hack/OWNERS"].replace("    - eval-crew\n", "    - eval-crew\n    reviewers:\n    - roster-reader\n")},
        )
        self.assertEqual(rr.applicable_reviewers(["hack/eval/presubmit-cases.txt"], self.root.name), {"roster-reader"})
        self.assertEqual(rr.applicable_reviewers(["hack/ci-eval-pr.sh"], self.root.name), ROOT_REVIEWERS)

    def test_an_author_self_approves_only_when_every_file_is_theirs(self):
        # Prow's `approved` is per file: a root approver's own pull request
        # is approved on open (#1075), but not the roster half of a mixed one,
        # since only eval-crew approves that (hack/OWNERS, no_parent_owners).
        def approves(author, *changed):
            return rr.author_approves(list(changed), author, self.root.name)

        self.assertTrue(approves("bradhoekstra", "README.md", "k8s-operator/go.mod"))
        self.assertTrue(approves("BradHoekstra", "README.md"))
        self.assertFalse(approves("bradhoekstra", "README.md", "hack/eval/presubmit-cases.txt"))
        self.assertTrue(approves("jayantid", "README.md", "hack/eval/presubmit-cases.txt"))
        self.assertFalse(approves("lapis2002", "README.md", "hack/eval/presubmit-cases.txt"))
        self.assertTrue(approves("lapis2002", "hack/eval/presubmit-cases.txt"))
        self.assertFalse(approves("stalhaali", "README.md"))
        self.assertFalse(approves("bradhoekstra"))

    def test_a_mixed_change_is_the_union(self):
        self.assertEqual(
            self.approvers("README.md", "hack/eval/presubmit-cases.txt"),
            ROOT_APPROVERS | {"lapis2002"},
        )

    def test_a_plain_approvers_list_in_a_subdirectory_adds_to_the_root(self):
        write_tree(self.root.name, {"docs/OWNERS": "approvers:\n- writer\n"})
        self.assertEqual(self.approvers("docs/README.md"), ROOT_APPROVERS | {"writer"})
        self.assertEqual(self.approvers("README.md"), ROOT_APPROVERS)

    def test_no_parent_owners_stops_on_approvers_collected_below_it_too(self):
        # Prow's `entriesForFile` breaks on "any approver collected so far",
        # not "matched at this level". With an OWNERS file under hack/eval/,
        # a file there arrives at hack/ already holding an approver, so
        # hack/'s no_parent_owners stops the walk whether or not its filter
        # matched -- and the root approvers never enter the set.
        write_tree(self.root.name, {"hack/eval/OWNERS": "approvers:\n- sub\n"})
        self.assertEqual(self.approvers("hack/eval/nightly-cases.txt"), {"sub"})
        self.assertEqual(self.approvers("hack/eval/presubmit-cases.txt"), {"sub", "jayantid", "lapis2002"})
        self.assertEqual(self.approvers("hack/ci-eval-pr.sh"), ROOT_APPROVERS)

    def test_top_level_approvers_beside_filters_are_ignored_as_prow_does(self):
        # Prow reads a file with `filters:` as a filtered file and drops a
        # top-level `approvers:` next to them.
        write_tree(
            self.root.name,
            {"hack/OWNERS": "approvers:\n- stray\n" + OWNERS_TREE["hack/OWNERS"]},
        )
        self.assertEqual(self.approvers("hack/eval/presubmit-cases.txt"), {"jayantid", "lapis2002"})
        self.assertEqual(self.approvers("hack/ci-eval-pr.sh"), ROOT_APPROVERS)

    def test_no_changed_files_means_no_approvers(self):
        self.assertEqual(self.approvers(), set())

    def test_a_missing_owners_tree_means_no_approvers(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(rr.applicable_approvers(["README.md"], empty), set())

    def test_the_live_tree_agrees_with_the_bots_config(self):
        # The properties docs/pull-request-workflow.md states of the config:
        # repository-owners and eval-crew are approvers for what the config
        # assigns them, and repository-reviewers may lgtm and not approve. If
        # this fails, one of OWNERS, OWNERS_ALIASES, hack/OWNERS or the config
        # moved and the other did not.
        root_approvers = rr.applicable_approvers(["README.md"], REPO_ROOT)
        self.assertTrue(set(OWNERS) <= root_approvers, root_approvers)
        self.assertEqual(rr.applicable_approvers(["hack/eval/presubmit-cases.txt"], REPO_ROOT), set(EVAL_CREW))
        self.assertNotIn(NON_APPROVER, root_approvers)
        # OWNERS repeats every root approver under `reviewers`: Prow's reviewer
        # walk falls through hack/OWNERS (no_parent_owners, no reviewers of its
        # own) to the root list, which is therefore who may lgtm a roster-only
        # change besides eval-crew, and the file's own comment says so. Pin
        # both halves: the repeat, and hack/OWNERS still naming no reviewers.
        # eval-crew is pinned as well so its members keep lgtm repository-wide
        # (lapis2002 is not a root approver).
        root_reviewers = {login.lower() for login in rr._read_yaml(REPO_ROOT / rr.OWNERS_FILENAME).get("reviewers") or []}
        self.assertTrue({login.lower() for login in root_approvers} <= root_reviewers, root_approvers - root_reviewers)
        self.assertTrue({login.lower() for login in EVAL_CREW} <= root_reviewers, set(EVAL_CREW) - root_reviewers)
        # repository-reviewers are asked for lgtm alone, which Prow takes from
        # them only while OWNERS lists them under `reviewers`; and none of them
        # is a root approver, or they belong in repository-owners, where the
        # narrowed pool for a non-approver's pull request can reach them.
        reviewers = {login.lower() for login in REVIEWERS}
        self.assertTrue(reviewers <= root_reviewers, reviewers - root_reviewers)
        self.assertFalse(reviewers & {login.lower() for login in root_approvers}, reviewers & root_approvers)
        hack_owners = rr._read_yaml(REPO_ROOT / "hack" / rr.OWNERS_FILENAME)
        self.assertNotIn("reviewers", hack_owners)
        for pattern, rules in (hack_owners.get("filters") or {}).items():
            self.assertNotIn("reviewers", rules or {}, pattern)


class SkipReasonTest(unittest.TestCase):
    """`skip_reason` -- the pull request states that get no reviewer, override or not."""

    def test_an_open_untouched_pull_request_is_not_skipped(self):
        self.assertIsNone(rr.skip_reason(pull_request(), CONFIG))

    def test_a_closed_pull_request_is_skipped(self):
        self.assertIn("not open", rr.skip_reason(pull_request(state="closed"), CONFIG))

    def test_a_draft_is_skipped(self):
        self.assertIn("draft", rr.skip_reason(pull_request(draft=True), CONFIG))

    def test_a_draft_is_not_skipped_when_ignore_draft_is_off(self):
        config = dict(CONFIG, options=dict(CONFIG["options"], ignore_draft=False))
        self.assertIsNone(rr.skip_reason(pull_request(draft=True), config))

    def test_an_ignored_keyword_in_the_title_is_skipped(self):
        skipped = rr.skip_reason(pull_request(title="DO NOT REVIEW: wip"), CONFIG)
        self.assertIn("DO NOT REVIEW", skipped)

    def test_a_request_outstanding_to_a_listed_robot_is_nobody_asked(self):
        # The robot answers the request and GitHub clears it, and nothing
        # re-fires after that; a person requested alongside still counts.
        self.assertIsNone(rr.skip_reason(pull_request(requested_reviewers=[{"login": "kyber775"}]), CONFIG))
        both = pull_request(requested_reviewers=[{"login": "kyber775"}, {"login": "jayantid"}])
        self.assertIn("jayantid", rr.skip_reason(both, CONFIG))

    def test_an_existing_request_is_not_duplicated(self):
        # The workflow fires on every completed AI Review check, so a pull
        # request already handed to a human must not be handed over again.
        skipped = rr.skip_reason(pull_request(requested_reviewers=[{"login": "jayantid"}]), CONFIG)
        self.assertIn("jayantid", skipped)

    def test_an_existing_team_request_is_not_duplicated(self):
        skipped = rr.skip_reason(pull_request(requested_teams=[{"slug": "sre"}]), CONFIG)
        self.assertIn("team:sre", skipped)


class AlreadyReviewedTest(unittest.TestCase):
    """`already_reviewed_reason` -- whose verdict makes a request redundant."""

    def reason(self, reviews, approvers=APPROVERS):
        return rr.already_reviewed_reason(pull_request(), reviews, approvers)

    def test_an_approvers_verdict_is_skipped(self):
        for state in ("APPROVED", "CHANGES_REQUESTED"):
            reviews = [review("jayantid", state)]
            self.assertIn("jayantid", self.reason(reviews), state)

    def test_a_non_approvers_approval_does_not_count(self):
        # The defect behind #1653, #1672 and #1545: an approval from someone
        # outside OWNERS cannot produce the `approved` label, so the pull
        # request sat with nobody asked and no way to merge.
        self.assertIsNone(self.reason([review(NON_APPROVER, "APPROVED")]))

    def test_a_non_approvers_changes_requested_still_counts(self):
        # Decided, not inherited: whoever asked for changes is owed a reply,
        # and asking a fresh reviewer over an open objection is noise.
        self.assertIn(NON_APPROVER, self.reason([review(NON_APPROVER, "CHANGES_REQUESTED")]))

    def test_a_robot_under_a_user_account_never_counts(self):
        # kyber775 reviews under a User account, re-reviews every push and files
        # its follow-ups as COMMENTED, so the CHANGES_REQUESTED it filed on one
        # commit stood as its verdict for the life of the pull request and a
        # green AI Review requested nobody. Listed as a robot, it counts no
        # more than the App's own review does, whatever state it files.
        robots = {"kyber775"}
        stale = [review("kyber775", "CHANGES_REQUESTED", submitted_at="1"), review("kyber775", submitted_at="2")]
        self.assertIsNone(rr.already_reviewed_reason(pull_request(), stale, APPROVERS, robots))
        self.assertIsNone(rr.already_reviewed_reason(pull_request(), [review("KYBER775", "APPROVED")], APPROVERS, robots))
        # Not listed, the same review counts, as it does from any other person.
        self.assertIn("kyber775", rr.already_reviewed_reason(pull_request(), stale, APPROVERS))

    def test_approver_logins_match_case_insensitively(self):
        self.assertIsNotNone(self.reason([review("JayantiD", "APPROVED")]))
        self.assertIsNotNone(self.reason([review("jayantid", "APPROVED")], approvers={"JayantiD"}))

    def test_the_bots_own_review_does_not_count_as_human_coverage(self):
        reviews = [review("kube-agents-bot[bot]", user_type="Bot")]
        self.assertIsNone(self.reason(reviews))
        self.assertIsNone(self.reason([review("kube-agents-bot[bot]", "APPROVED", user_type="Bot")]))

    def test_the_authors_replies_to_the_bot_do_not_block_their_own_reviewer(self):
        # Answering a review thread files a `COMMENTED` review under the
        # replier's name, and AGENTS.md tells authors to answer every finding
        # before running `/review`. Counting those would starve exactly the pull
        # requests that follow the process.
        reviews = [review("kube-agents-bot[bot]", user_type="Bot"), review("author")]
        self.assertIsNone(self.reason(reviews))

    def test_a_drive_by_comment_from_a_colleague_does_not_block_it_either(self):
        self.assertIsNone(self.reason([review("jayantid")]))

    def test_a_verdict_from_the_author_is_still_the_author(self):
        # GitHub will not let you approve your own pull request, but a
        # `CHANGES_REQUESTED` on your own is possible and is not coverage.
        self.assertIsNone(self.reason([review("author", "CHANGES_REQUESTED")]))
        self.assertIsNone(self.reason([review("author", "CHANGES_REQUESTED")], approvers={"author"}))

    def test_the_live_fixture_shape_is_not_skipped(self):
        # #1545 on 2026-09-17: one bot COMMENTED, one APPROVED from a colleague
        # outside OWNERS, nobody requested. main said "already reviewed it".
        reviews = [review("kube-agents-bot[bot]", user_type="Bot"), review(NON_APPROVER, "APPROVED")]
        self.assertIsNone(self.reason(reviews))

    def test_with_no_approvers_only_an_objection_counts(self):
        self.assertIsNone(self.reason([review("jayantid", "APPROVED")], approvers=set()))
        self.assertIsNotNone(self.reason([review("jayantid", "CHANGES_REQUESTED")], approvers=set()))

    def test_a_withdrawn_objection_no_longer_counts(self):
        # The reviews list keeps every review ever filed. A non-approver who
        # asked for changes and then approved has said their piece; only the
        # latest verdict is theirs, and that one cannot produce `approved`.
        reviews = [review(NON_APPROVER, "CHANGES_REQUESTED"), review(NON_APPROVER, "APPROVED")]
        self.assertIsNone(self.reason(reviews))
        # The same sequence from an approver is an approval that counts.
        reviews = [review("jayantid", "CHANGES_REQUESTED"), review("jayantid", "APPROVED")]
        self.assertIn("jayantid", self.reason(reviews))

    def test_an_objection_after_an_approval_counts(self):
        reviews = [review(NON_APPROVER, "APPROVED"), review(NON_APPROVER, "CHANGES_REQUESTED")]
        self.assertIn(NON_APPROVER, self.reason(reviews))

    def test_a_later_comment_does_not_clear_an_objection(self):
        # GitHub's own rule: only a new verdict or a dismissal moves the state.
        reviews = [review(NON_APPROVER, "CHANGES_REQUESTED"), review(NON_APPROVER)]
        self.assertIn(NON_APPROVER, self.reason(reviews))

    def test_verdicts_are_ordered_by_submission_time_not_list_order(self):
        reviews = [
            review(NON_APPROVER, "APPROVED", submitted_at="2026-09-17T12:00:00Z"),
            review(NON_APPROVER, "CHANGES_REQUESTED", submitted_at="2026-09-17T10:00:00Z"),
        ]
        self.assertIsNone(self.reason(reviews))

    def test_a_dismissed_review_does_not_count(self):
        self.assertIsNone(self.reason([review("jayantid", "DISMISSED"), review(NON_APPROVER, "DISMISSED")]))


class _MainHarness(unittest.TestCase):
    """`main` end to end against `FakeAPI`; the test classes below inherit it and add nothing else here."""

    COMMENT_ID = 5719654130
    REQUESTED = "/repos/gke-labs/kube-agents/pulls/1/requested_reviewers"
    HANDOFF = "/repos/gke-labs/kube-agents/issues/1/comments"
    REACTIONS = f"/repos/gke-labs/kube-agents/issues/comments/{COMMENT_ID}/reactions"

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        write_tree(self.root.name, OWNERS_TREE)
        self.summary = Path(self.root.name) / "summary.md"
        env = {"GITHUB_TOKEN": "t", rr.STEP_SUMMARY_ENV: str(self.summary), rr.ACTIONS_ENV: "true"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(
        self, pull, reviews, *extra, files=("README.md",), check_runs=None, comments=(), check_runs_by_id=None, commits=()
    ):
        self.api = FakeAPI(
            pulls=[pull],
            reviews={1: reviews},
            files={1: list(files)},
            check_runs=check_runs,
            comments={1: list(comments)},
            check_runs_by_id=check_runs_by_id,
            commits={1: list(commits)},
        )
        # `--check-run-id` is the workflow's own path and excludes `--pr`: the
        # pull request is then resolved from the commit the entry names.
        target = [] if "--check-run-id" in extra else ["--pr", "1"]
        argv = [*target, "--config", str(LIVE_CONFIG), "--owners-root", self.root.name, "--seed", "0", *extra]
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(rr, "GitHubAPI", return_value=self.api):
            with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
                self.code = rr.main(argv)
        return [path for path, _ in self.api.posts]

    def reaction(self):
        return [payload["content"] for path, payload in self.api.posts if path == self.REACTIONS]

    def run_main_with_failing(self, failing, pull, reviews, *extra, failing_lists=(), **kwargs):
        """`run_main`, with the given POST paths (and listing suffixes) raising as the API wrapper would after its retries."""
        original = FakeAPI.__init__

        def init(api, *a, **kw):
            original(api, *a, **kw)
            api.failing = failing
            api.failing_lists = failing_lists

        with mock.patch.object(FakeAPI, "__init__", init):
            return self.run_main(pull, reviews, *extra, **kwargs)

    def requested(self):
        return [login for path, payload in self.api.posts if path == self.REQUESTED for login in payload["reviewers"]]


class MainTest(_MainHarness):
    """`main` -- the check-run path and the `/request-review` override end to end."""

    def test_a_non_approvers_approval_no_longer_blocks_the_check_run_path(self):
        posts = self.run_main(pull_request(), [review(NON_APPROVER, "APPROVED")])
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])
        self.assertEqual(self.code, 0)

    def test_a_listed_robots_changes_requested_does_not_block_the_check_run_path(self):
        # Through the live roster, which lists kyber775: the check-run path
        # reads the robot's standing CHANGES_REQUESTED as no verdict at all
        # and requests a human.
        posts = self.run_main(pull_request(), [review("kyber775", "CHANGES_REQUESTED", submitted_at="1"), review("kyber775", submitted_at="2")])
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])
        self.assertEqual(self.code, 0)
        # The run log names the list in effect, since shape is all that can be
        # validated and a well-formed login naming no account sits inert.
        self.assertIn("Robot accounts, whose reviews never count: kyber775", self.stderr.getvalue())

    def test_a_request_outstanding_to_a_listed_robot_does_not_block_the_check_run_path(self):
        posts = self.run_main(pull_request(requested_reviewers=[{"login": "kyber775"}]), [])
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])
        self.assertEqual(self.code, 0)

    def test_a_non_approver_author_is_sent_an_approver(self):
        # The live roster now lists non-approvers in the catch-all pool, and
        # "author" approves nothing in the fixture tree: whoever is asked has
        # to be able to clear `approved`, or the pull request takes an lgtm it
        # cannot use and sits with nobody asked for the rest.
        # Against the fixture tree's approvers, not the config group's names:
        # the group holds three logins the tree does not make approvers, and
        # an implementation that narrowed by group name would hand them out.
        for seed in range(20):
            self.run_main(pull_request(), [], "--seed", str(seed))
            self.assertEqual(self.code, 0)
            self.assertIn(self.requested()[0].lower(), ROOT_APPROVERS, seed)
        self.assertIn("author's own approval does not cover", self.stderr.getvalue())

    def test_the_override_narrows_the_pool_too(self):
        # `/request-review` skips the verdict check, not the narrowing: the
        # person asking wants a reviewer now, and a non-approver's pull request
        # still needs an approver's. The two live in different blocks of
        # `main`, so this pins that moving the narrowing into the verdict block
        # cannot ship green.
        for seed in range(20):
            self.run_main(pull_request(), [], "--react-to", str(self.COMMENT_ID), "--seed", str(seed))
            self.assertEqual(self.code, 0)
            self.assertIn(self.requested()[0].lower(), ROOT_APPROVERS, seed)
            self.assertEqual(self.reaction(), [rr.REACTION_ACKNOWLEDGED])

    def test_an_approver_author_draws_from_the_whole_pool(self):
        # bradhoekstra approves README.md in the fixture tree, so the pull
        # request opens approved and needs lgtm alone: the non-approver
        # reviewers are in the draw, at the same weight as everyone else.
        picked = set()
        for seed in range(60):
            self.run_main(pull_request(user={"login": "bradhoekstra", "type": "User"}), [], "--seed", str(seed))
            picked.update(self.requested())
        self.assertEqual(picked, set(POOL) - {"bradhoekstra"})

    def test_a_mixed_change_by_an_approver_is_not_self_approved(self):
        # The roster half needs eval-crew, so the pool is narrowed to the
        # OWNERS approvers for the change -- root and eval-crew -- after the
        # files map has already cut it to eval-crew. Either member clears the
        # roster half; the README half is the author's own.
        self.run_main(
            pull_request(user={"login": "bradhoekstra", "type": "User"}), [], files=("README.md", "hack/eval/presubmit-cases.txt")
        )
        self.assertEqual(len(self.requested()), 1)
        self.assertIn(self.requested()[0], EVAL_CREW)
        self.assertIn("own approval does not cover", self.stderr.getvalue())

    def test_a_reviewers_approval_covers_an_approvers_pull_request(self):
        # stalhaali is under `reviewers` in the fixture OWNERS: on a
        # self-approved pull request that approval sets lgtm, the last label
        # it needed, so a later check run asks nobody else.
        posts = self.run_main(pull_request(user={"login": "bradhoekstra", "type": "User"}), [review("stalhaali", "APPROVED")])
        self.assertEqual(posts, [])
        self.assertIn("stalhaali already reviewed it", self.stderr.getvalue())

    def test_a_reviewers_approval_does_not_cover_anyone_elses(self):
        # The same approval on a non-approver's pull request leaves `approved`
        # outstanding, so an approver is still asked, and only an approver.
        posts = self.run_main(pull_request(), [review("stalhaali", "APPROVED")])
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])
        self.assertIn(self.requested()[0].lower(), ROOT_APPROVERS)

    def test_a_change_no_approver_covers_gets_nobody(self):
        # No changed files -- an empty commit, or a branch reset to its base
        # while the pull request stays open: the approver walk returns nothing
        # and the author self-approves nothing, so narrowing to an empty set
        # would fall back to the whole pool and could hand a non-approver an
        # `approved` nobody can give. Both paths decline and say why.
        posts = self.run_main(pull_request(), [], files=())
        self.assertEqual(posts, [])
        self.assertIn("no OWNERS approver covers the change", self.stderr.getvalue())
        self.assertEqual(self.code, 0)
        posts = self.run_main(pull_request(), [], "--react-to", str(self.COMMENT_ID), files=())
        self.assertEqual(posts, [self.REACTIONS])
        self.assertEqual(self.reaction(), [rr.REACTION_DECLINED])
        self.assertIn("no OWNERS approver covers the change", self.stdout.getvalue())

    def test_an_outsiders_approval_covers_nothing_even_for_an_approver(self):
        # Not under `reviewers` either: Prow sets no lgtm for it.
        posts = self.run_main(pull_request(user={"login": "bradhoekstra", "type": "User"}), [review(NON_APPROVER, "APPROVED")])
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])

    def test_an_approvers_approval_still_blocks_the_check_run_path(self):
        posts = self.run_main(pull_request(), [review("jayantid", "APPROVED")])
        self.assertEqual(posts, [])
        self.assertIn("jayantid already reviewed it", self.stderr.getvalue())
        # The quiet path: no annotation, no summary line, exit 0.
        self.assertNotIn(rr.WORKFLOW_WARNING_PREFIX, self.stdout.getvalue())
        self.assertFalse(self.summary.exists())
        self.assertEqual(self.code, 0)

    def test_the_override_bypasses_the_verdict_and_acknowledges(self):
        posts = self.run_main(pull_request(), [review("jayantid", "APPROVED")], "--react-to", str(self.COMMENT_ID))
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF, self.REACTIONS])
        self.assertEqual(self.reaction(), [rr.REACTION_ACKNOWLEDGED])
        self.assertEqual(self.code, 0)

    def test_the_override_does_not_re_ask_when_someone_is_already_requested(self):
        pull = pull_request(requested_reviewers=[{"login": "jayantid"}])
        posts = self.run_main(pull, [], "--react-to", str(self.COMMENT_ID))
        self.assertEqual(posts, [self.REACTIONS])
        self.assertEqual(self.reaction(), [rr.REACTION_DECLINED])
        self.assertEqual(self.code, 0)

    def test_a_declined_override_is_written_where_the_person_can_see_it(self):
        pull = pull_request(requested_reviewers=[{"login": "jayantid"}])
        self.run_main(pull, [], "--react-to", str(self.COMMENT_ID))
        message = "Not requesting a reviewer: review is already requested from jayantid"
        self.assertIn(f"{rr.WORKFLOW_WARNING_PREFIX}{message}", self.stdout.getvalue())
        self.assertEqual(self.summary.read_text(encoding="utf-8"), f"{message}\n")
        self.assertIn(message, self.stderr.getvalue())

    def test_a_declined_override_on_a_draft_reacts_too(self):
        posts = self.run_main(pull_request(draft=True), [], "--react-to", str(self.COMMENT_ID))
        self.assertEqual(posts, [self.REACTIONS])
        self.assertEqual(self.reaction(), [rr.REACTION_DECLINED])

    def test_dry_run_posts_nothing_on_either_outcome(self):
        pull = pull_request(requested_reviewers=[{"login": "jayantid"}])
        self.assertEqual(self.run_main(pull, [], "--react-to", str(self.COMMENT_ID), "--dry-run"), [])
        self.assertIn(f"would react {rr.REACTION_DECLINED}", self.stderr.getvalue())
        self.assertEqual(self.run_main(pull_request(), [], "--react-to", str(self.COMMENT_ID), "--dry-run"), [])
        self.assertIn(f"would react {rr.REACTION_ACKNOWLEDGED}", self.stderr.getvalue())

    def test_the_check_run_path_never_reacts(self):
        posts = self.run_main(pull_request(requested_reviewers=[{"login": "jayantid"}]), [])
        self.assertEqual(posts, [])


class HandoffTest(unittest.TestCase):
    """The third round summons a human whatever the colour, once per pull request."""

    HEADS = ("1111aaa", "2222bbb", "3333ccc")

    def rounds(self, *heads):
        return [bot_review(sha) for sha in heads]

    def test_a_tally_is_read_off_the_last_line_of_the_summary(self):
        self.assertEqual(rr.ai_review_tally(tallied())["outcome"], "findings")
        self.assertEqual(rr.ai_review_tally(tallied(outcome="clean", conclusion="success"))["outcome"], "clean")

    def test_a_push_row_carries_no_tally(self):
        # `check_pushed` writes the previous title with no tally line: nothing was read.
        pushed = check_run("neutral", output={"title": "Found 2 issues, on an earlier commit", "summary": "Pushed."})
        self.assertIsNone(rr.ai_review_tally(pushed))
        self.assertIsNone(rr.ai_review_tally(check_run("neutral", output={"title": "x"})))
        self.assertIsNone(rr.ai_review_tally(None))

    def test_a_malformed_tally_reads_as_none(self):
        broken = check_run("neutral", output={"summary": f"{rr.AI_REVIEW_TALLY_PREFIX} not json"})
        self.assertIsNone(rr.ai_review_tally(broken))
        wrong_shape = check_run("neutral", output={"summary": f"{rr.AI_REVIEW_TALLY_PREFIX} [1, 2]"})
        self.assertIsNone(rr.ai_review_tally(wrong_shape))

    def test_rounds_are_distinct_commits_the_bot_reviewed(self):
        # A re-cut is a second review of the same commit and a human's review is not a round.
        reviews = self.rounds("1111aaa", "1111aaa", "2222bbb") + [review("jayantid", commit_id="9999fff")]
        self.assertEqual(rr.reviewed_commits(reviews), {"1111aaa", "2222bbb"})

    def test_the_deciding_commit_counts_even_before_its_review_is_listed(self):
        self.assertEqual(rr.reviewed_commits(self.rounds("1111aaa"), "2222bbb", None), {"1111aaa", "2222bbb"})

    def test_the_marker_is_what_says_a_hand_off_happened(self):
        own = {"user": {"login": rr.HANDOFF_AUTHOR}}
        self.assertFalse(rr.handed_off([{**own, "body": "/review"}, {**own, "body": None}]))
        self.assertTrue(rr.handed_off([{**own, "body": f"{rr.HANDOFF_MARKER}\nHanded to @x"}]))
        # A reply quoting the hand-off carries the marker too, further down.
        self.assertFalse(rr.handed_off([{**own, "body": f"> {rr.HANDOFF_MARKER}\n> Handed to @x"}]))

    def test_a_marker_anyone_else_posted_is_not_a_hand_off(self):
        # The marker renders as nothing, so an agent pasting a hand-off it saw
        # elsewhere, or anyone typing it, would otherwise switch the rule off.
        forged = [{"user": {"login": "author"}, "body": f"{rr.HANDOFF_MARKER}\nHanded to @x"}]
        self.assertFalse(rr.handed_off(forged))
        self.assertFalse(rr.handed_off([{"body": f"{rr.HANDOFF_MARKER}\nno user field at all"}]))

    def test_the_third_round_clears_a_grey_check(self):
        reason = rr.ai_review_block_reason(tallied(), author_is_bot=False, rounds=3, already_handed_off=False)
        self.assertIsNone(reason)

    def test_a_clean_but_partial_read_clears_it_too(self):
        partial = tallied(conclusion="neutral", title="No findings in what could be checked", outcome="clean")
        self.assertIsNone(rr.ai_review_block_reason(partial, author_is_bot=False, rounds=4, already_handed_off=False))

    def test_two_rounds_still_hold(self):
        reason = rr.ai_review_block_reason(tallied(), author_is_bot=False, rounds=2, already_handed_off=False)
        self.assertIn("not success", reason)
        self.assertIn("round 2", reason)

    def test_a_hand_off_already_made_holds(self):
        reason = rr.ai_review_block_reason(tallied(), author_is_bot=False, rounds=7, already_handed_off=True)
        self.assertIn("already handed", reason)

    def test_a_push_row_never_clears_however_many_rounds(self):
        # The gate decides on the newest entry on the head; after a push that is
        # the carried-title row, which read nothing.
        pushed = check_run("neutral", output={"title": "Found 2 issues, on an earlier commit", "summary": "Pushed."})
        self.assertIsNotNone(rr.ai_review_block_reason(pushed, author_is_bot=False, rounds=9, already_handed_off=False))

    def test_a_row_the_bot_did_not_finish_reading_never_clears(self):
        for outcome in ("broken", "conflicted", "superseded", "unparsed"):
            entry = tallied(outcome=outcome, title="Review did not complete")
            self.assertIsNotNone(rr.ai_review_block_reason(entry, author_is_bot=False, rounds=5, already_handed_off=False), outcome)

    def test_without_the_round_count_the_old_rule_stands(self):
        # The pure call with no rounds given is the first-review gate, unchanged.
        self.assertIsNotNone(rr.ai_review_block_reason(tallied(), author_is_bot=False))
        self.assertIsNone(rr.ai_review_block_reason(check_run("success"), author_is_bot=False))

    def test_the_comment_names_the_reviewer_the_commit_and_the_verdict(self):
        body = rr.handoff_comment(["jayantid"], ["eval-crew"], "deadbeefcafe", tallied(), "the bot has reviewed 3 commits")
        self.assertTrue(body.startswith(rr.HANDOFF_MARKER))
        for expected in ("@jayantid", "team eval-crew", "deadbee", "Found 2 issues", "3 commits"):
            self.assertIn(expected, body)
        # Prow reads a slash command at the start of a line; none of these lines may start one.
        self.assertFalse(any(line.startswith("/") for line in body.splitlines()))
        # One rule, the same one AGENTS.md and the skill state: no further `/review` after the hand-off.
        self.assertIn("Then wait.", body)
        self.assertNotIn("one `/review`", body)

    def test_the_comment_claims_nothing_about_a_check_it_did_not_read(self):
        body = rr.handoff_comment(["jayantid"], [], "deadbeefcafe", None, "a person asked for a reviewer with /request-review")
        self.assertNotIn("check reads", body)
        self.assertNotIn("no verdict", body)


class HandoffMainTest(_MainHarness):
    """`main` on the check-run path: round three requests and hands off, once."""

    GREY = {"deadbeef": [tallied(id=7, head_sha="deadbeef")]}

    def test_round_three_requests_a_human_on_a_grey_check(self):
        posts = self.run_main(
            pull_request(), [bot_review("1111aaa"), bot_review("2222bbb"), bot_review("deadbeef")],
            "--require-ai-review-pass", check_runs=self.GREY,
        )
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])
        self.assertIn("round 3", self.stderr.getvalue())
        body = self.api.posts[1][1]["body"]
        self.assertIn(rr.HANDOFF_MARKER, body)
        self.assertIn("3 commits", body)

    def test_round_two_still_waits_for_green(self):
        posts = self.run_main(
            pull_request(), [bot_review("1111aaa"), bot_review("deadbeef")], "--require-ai-review-pass", check_runs=self.GREY
        )
        self.assertEqual(posts, [])
        self.assertIn("not success", self.stderr.getvalue())

    def test_a_re_cut_is_not_a_round(self):
        posts = self.run_main(
            pull_request(), [bot_review("1111aaa"), bot_review("1111aaa"), bot_review("deadbeef")],
            "--require-ai-review-pass", check_runs=self.GREY,
        )
        self.assertEqual(posts, [])

    def test_the_hand_off_happens_once(self):
        handed = [{"user": {"login": rr.HANDOFF_AUTHOR}, "body": f"{rr.HANDOFF_MARKER}\nHanded to @jayantid at `1111aaa`."}]
        posts = self.run_main(
            pull_request(), [bot_review("1111aaa"), bot_review("2222bbb"), bot_review("deadbeef")],
            "--require-ai-review-pass", check_runs=self.GREY, comments=handed,
        )
        self.assertEqual(posts, [])
        self.assertIn("already handed", self.stderr.getvalue())

    def test_a_forged_marker_does_not_switch_the_rule_off(self):
        forged = [{"user": {"login": "author"}, "body": f"{rr.HANDOFF_MARKER}\nHanded to @jayantid at `1111aaa`."}]
        posts = self.run_main(
            pull_request(), [bot_review("1111aaa"), bot_review("2222bbb"), bot_review("deadbeef")],
            "--require-ai-review-pass", check_runs=self.GREY, comments=forged,
        )
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])

    def test_a_hand_off_that_fails_to_post_leaves_the_request_standing(self):
        # Two writes: the request went out, so the comment's failure is a
        # warning in the log, never a red run and never a lost request.
        posts = self.run_main_with_failing(
            (self.HANDOFF,), pull_request(), [bot_review("1111aaa"), bot_review("2222bbb"), bot_review("deadbeef")],
            "--require-ai-review-pass", check_runs=self.GREY,
        )
        self.assertEqual(posts, [self.REQUESTED])
        self.assertEqual(self.code, 0)
        self.assertIn("could not post the hand-off comment", self.stderr.getvalue())

    def test_a_request_run_by_hand_announces_nothing(self):
        # Outside Actions the comment would land under the maintainer's login,
        # which `handed_off` never counts, so the next grey third-plus round
        # would post a second hand-off anyway: announce nothing and say so.
        with mock.patch.dict(os.environ, {rr.ACTIONS_ENV: ""}):
            posts = self.run_main(pull_request(), [], check_runs={})
        self.assertEqual(posts, [self.REQUESTED])
        self.assertIn("not announcing the hand-off", self.stderr.getvalue())

    def test_the_hand_run_reason_is_named_when_the_workflow_runs_without_flags(self):
        # `--pr N` with neither flag under Actions (a workflow_dispatch, say): the
        # comment attributes the request to a hand run, not to a /request-review.
        posts = self.run_main(pull_request(), [], check_runs={})
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])
        body = self.api.posts[1][1]["body"]
        self.assertIn("ran the request by hand", body)
        self.assertNotIn("/request-review", body)

    def test_a_failed_comments_listing_never_costs_the_request_on_the_green_path(self):
        # The listing decides decoration here; the request goes out and the
        # hand-off is posted anyway (a duplicate is the worse case).
        green = {"deadbeef": [tallied(conclusion="success", title="No findings", outcome="clean", head_sha="deadbeef")]}
        posts = self.run_main_with_failing(
            (), pull_request(), [bot_review("deadbeef")], "--require-ai-review-pass",
            check_runs=green, failing_lists=("/issues/1/comments",),
        )
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])
        self.assertEqual(self.code, 0)
        self.assertIn("announcing anyway", self.stderr.getvalue())

    def test_a_failed_comments_listing_still_holds_the_grey_gate(self):
        # On the grey path the same listing decides whether the third-round
        # request fires at all; a listing that fails must not fire it blind.
        with self.assertRaises(RuntimeError):
            self.run_main_with_failing(
                (), pull_request(), [bot_review("1111aaa"), bot_review("2222bbb"), bot_review("deadbeef")],
                "--require-ai-review-pass", check_runs=self.GREY, failing_lists=("/issues/1/comments",),
            )

    def test_a_green_check_still_requests_and_hands_off(self):
        green = {"deadbeef": [tallied(conclusion="success", title="No findings", outcome="clean", head_sha="deadbeef")]}
        posts = self.run_main(pull_request(), [bot_review("deadbeef")], "--require-ai-review-pass", check_runs=green)
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])
        self.assertIn("the check is green", self.api.posts[1][1]["body"])

    def test_a_green_check_does_not_post_a_second_hand_off(self):
        green = {"deadbeef": [tallied(conclusion="success", title="No findings", outcome="clean", head_sha="deadbeef")]}
        handed = [{"user": {"login": rr.HANDOFF_AUTHOR}, "body": f"{rr.HANDOFF_MARKER}\nHanded to @jayantid at `1111aaa`."}]
        posts = self.run_main(pull_request(), [bot_review("deadbeef")], "--require-ai-review-pass", check_runs=green, comments=handed)
        self.assertEqual(posts, [self.REQUESTED])

    def test_the_triggering_check_run_supplies_the_commit_it_read(self):
        # `--check-run-id`, the workflow's own path: the review of the deciding
        # commit may not be listed yet when the check completes, so the commit
        # counts from the entry itself.
        entry = tallied(id=7, head_sha="deadbeef")
        posts = self.run_main(
            pull_request(), [bot_review("1111aaa"), bot_review("2222bbb")], "--require-ai-review-pass",
            "--check-run-id", "7", check_runs={}, check_runs_by_id={7: entry},
        )
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])

    def test_a_stale_triggering_entry_still_decides_when_the_new_head_has_none(self):
        # The author pushed during the third read; the head carries no entry
        # yet, so the entry that triggered this run decides, as before.
        entry = tallied(id=7, head_sha="0000abc")
        posts = self.run_main(
            pull_request(), [bot_review("1111aaa"), bot_review("2222bbb"), bot_review("0000abc")],
            "--require-ai-review-pass", "--check-run-id", "7", check_runs={"deadbeef": []}, check_runs_by_id={7: entry},
            commits=("0000abc", "deadbeef"),
        )
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])
        # The comment names the commit the quoted verdict is on, not the head it is not on yet.
        body = self.api.posts[1][1]["body"]
        self.assertIn("`0000abc`", body)
        self.assertNotIn("`deadbee`", body)

    def test_a_head_with_no_entry_declines_instead_of_crashing(self):
        # `--pr N --require-ai-review-pass` by hand before the bot has posted
        # anything: the old decline, not an AttributeError on a missing entry.
        posts = self.run_main(pull_request(), [bot_review("1111aaa")], "--require-ai-review-pass", check_runs={})
        self.assertEqual(posts, [])
        self.assertIn("there is no AI Review check run", self.stderr.getvalue())

    def test_a_running_check_declines_without_counting_rounds(self):
        running = {"deadbeef": [check_run(None, status="in_progress", id=9, head_sha="deadbeef")]}
        posts = self.run_main(
            pull_request(), [bot_review("1111aaa"), bot_review("2222bbb"), bot_review("3333ccc")],
            "--require-ai-review-pass", check_runs=running,
        )
        self.assertEqual(posts, [])
        self.assertIn("in_progress", self.stderr.getvalue())
        # The guard, not only the decline: the comments listing is the one the grey path adds.
        self.assertFalse(any(path.endswith("/issues/1/comments") for path in self.api.lists))

    def test_a_push_row_on_the_head_decides_and_holds(self):
        pushed = check_run("neutral", id=8, head_sha="deadbeef", started_at="2026-10-08T12:00:00Z",
                           output={"title": "Found 2 issues, on an earlier commit", "summary": "Pushed."})
        posts = self.run_main(
            pull_request(), [bot_review("1111aaa"), bot_review("2222bbb"), bot_review("3333ccc")],
            "--require-ai-review-pass", check_runs={"deadbeef": [pushed]},
        )
        self.assertEqual(posts, [])

    def test_a_bot_author_is_handed_off_on_the_first_completed_check_as_before(self):
        posts = self.run_main(
            pull_request(user={"login": "dependabot[bot]", "type": "Bot"}), [bot_review("deadbeef")],
            "--require-ai-review-pass", check_runs=self.GREY,
        )
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF])

    def test_dry_run_posts_neither_the_request_nor_the_comment(self):
        posts = self.run_main(
            pull_request(), [bot_review("1111aaa"), bot_review("2222bbb"), bot_review("deadbeef")],
            "--require-ai-review-pass", "--dry-run", check_runs=self.GREY,
        )
        self.assertEqual(posts, [])
        self.assertIn("would post the hand-off", self.stderr.getvalue())

    def test_the_override_hands_off_too(self):
        green = {"deadbeef": [tallied(conclusion="success", title="No findings", outcome="clean", head_sha="deadbeef")]}
        posts = self.run_main(pull_request(), [], "--react-to", str(self.COMMENT_ID), check_runs=green)
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF, self.REACTIONS])
        body = self.api.posts[1][1]["body"]
        self.assertIn("asked for a reviewer", body)
        # It did not consult the check to decide, but it reads the head's verdict for the reviewer.
        self.assertIn("No findings", body)
        self.assertNotIn("no verdict", body)

    def test_the_override_on_a_head_with_no_entry_names_no_verdict(self):
        posts = self.run_main(pull_request(), [], "--react-to", str(self.COMMENT_ID), check_runs={})
        self.assertEqual(posts, [self.REQUESTED, self.HANDOFF, self.REACTIONS])
        self.assertNotIn("check reads", self.api.posts[1][1]["body"])



class AiReviewGateTest(unittest.TestCase):
    """`latest_ai_review` and `ai_review_block_reason` -- the gate itself."""

    def test_the_most_recent_run_is_the_verdict(self):
        # `/review` posts a new check run rather than updating the old one.
        runs = [
            check_run("neutral", id=1, started_at="2026-08-17T10:00:00Z"),
            check_run("success", id=2, started_at="2026-08-17T12:00:00Z"),
        ]
        self.assertEqual(rr.latest_ai_review(runs)["id"], 2)

    def test_a_check_run_from_another_app_is_ignored(self):
        impostor = check_run("success", app={"id": 1})
        self.assertIsNone(rr.latest_ai_review([impostor]))

    def test_a_check_run_with_another_name_is_ignored(self):
        self.assertIsNone(rr.latest_ai_review([check_run("success", name="build")]))

    def test_success_clears_the_gate(self):
        self.assertIsNone(rr.ai_review_block_reason(check_run("success"), author_is_bot=False))

    def test_findings_hold_the_gate(self):
        reason = rr.ai_review_block_reason(
            check_run("neutral", output={"title": "Found 2 issues"}), author_is_bot=False
        )
        self.assertIn("neutral", reason)
        self.assertIn("Found 2 issues", reason)

    def test_a_missing_check_run_holds_the_gate(self):
        self.assertIn("no AI Review", rr.ai_review_block_reason(None, author_is_bot=False))

    def test_a_running_check_holds_the_gate(self):
        reason = rr.ai_review_block_reason(
            check_run(None, status="in_progress"), author_is_bot=False
        )
        self.assertIn("in_progress", reason)

    def test_findings_do_not_hold_the_gate_for_a_bot_author(self):
        # Dependabot cannot read its own findings and comment `/review`, so its
        # pull requests pass on any completed conclusion.
        self.assertIsNone(rr.ai_review_block_reason(check_run("neutral"), author_is_bot=True))

    def test_a_bot_author_still_needs_the_check_to_have_run(self):
        self.assertIsNotNone(rr.ai_review_block_reason(None, author_is_bot=True))


class ResolvePullRequestTest(unittest.TestCase):
    """`resolve_pull_request` -- commit to pull request, head or not."""

    def api(self):
        return FakeAPI(
            pulls=[
                pull_request(number=10, head={"sha": "aaaa111"}),
                pull_request(number=11, head={"sha": "bbbb222"}),
            ],
            commits={10: ["0000abc", "aaaa111"], 11: ["bbbb222"]},
        )

    def test_the_head_commit_resolves_without_listing_commits(self):
        api = self.api()
        api.commits = {}  # listing commits at all would raise here
        self.assertEqual(rr.resolve_pull_request(api, "bbbb222")["number"], 11)

    def test_a_commit_the_head_has_moved_past_still_resolves(self):
        # The author pushed while the bot was reading. Nothing else will fire:
        # a push does not start another AI review.
        self.assertEqual(rr.resolve_pull_request(self.api(), "0000abc")["number"], 10)

    def test_a_commit_on_no_open_pull_request_resolves_to_nothing(self):
        self.assertIsNone(rr.resolve_pull_request(self.api(), "deadbee"))


class GateCheckRunTest(unittest.TestCase):
    """`gate_check_run` -- which verdict the gate is decided on."""

    def test_the_triggering_run_decides_when_it_is_on_the_head(self):
        triggering = check_run("success", id=1, head_sha="aaaa111")
        api = FakeAPI()  # any call would raise
        chosen = rr.gate_check_run(api, pull_request(head={"sha": "aaaa111"}), triggering)
        self.assertEqual(chosen["id"], 1)

    def test_a_newer_verdict_on_the_new_head_wins(self):
        triggering = check_run("success", id=1, head_sha="aaaa111")
        api = FakeAPI(check_runs={"bbbb222": [check_run("neutral", id=2)]})
        chosen = rr.gate_check_run(api, pull_request(head={"sha": "bbbb222"}), triggering)
        self.assertEqual(chosen["id"], 2)

    def test_a_stale_verdict_still_decides_when_the_new_head_has_none(self):
        # Otherwise the pull request waits forever for a review nobody will run.
        triggering = check_run("success", id=1, head_sha="aaaa111")
        api = FakeAPI(check_runs={"bbbb222": []})
        chosen = rr.gate_check_run(api, pull_request(head={"sha": "bbbb222"}), triggering)
        self.assertEqual(chosen["id"], 1)

    def test_without_a_triggering_run_the_head_decides(self):
        api = FakeAPI(check_runs={"deadbeef": [check_run("success", id=3)]})
        self.assertEqual(rr.gate_check_run(api, pull_request(), None)["id"], 3)


if __name__ == "__main__":
    unittest.main()
