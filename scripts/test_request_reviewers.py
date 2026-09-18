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
import os
import random
import re
import sys
import tempfile
import unittest
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
        "defaults": ["repository-owners"],
        "groups": {
            "repository-owners": ["bradhoekstra", "jayantid", "toshiowang", "dshnayder"],
            "eval-crew": ["jayantid", "lapis2002"],
        },
    },
    "files": {
        "**": ["repository-owners"],
        "hack/eval/presubmit-cases.txt": ["eval-crew"],
        "hack/eval/blocking-roster.txt": ["eval-crew"],
    },
    "options": {
        "ignore_draft": True,
        "ignored_keywords": ["DO NOT REVIEW"],
        "enable_group_assignment": False,
        "number_of_reviewers": 1,
        "last_files_match_only": True,
    },
}

OWNERS = CONFIG["reviewers"]["groups"]["repository-owners"]
EVAL_CREW = CONFIG["reviewers"]["groups"]["eval-crew"]
REPO_ROOT = _HERE.parent
LIVE_CONFIG = REPO_ROOT / rr.DEFAULT_CONFIG_PATH

# The OWNERS approvers the verdict check is handed in these tests. `bnaylor`
# is deliberately not one of them: he is the colleague whose real approvals
# are `COMMENTED`-equivalent for Tide, and the shape the fixtures below reuse.
APPROVERS = {"bradhoekstra", "jayantid", "toshiowang", "dshnayder"}
NON_APPROVER = "kyber775"

# The root OWNERS, OWNERS_ALIASES and hack/OWNERS as they stand, for the walk
# tests that need a tree they can also mutate.
OWNERS_TREE = {
    "OWNERS": "approvers:\n- AntonTyb\n- bradhoekstra\n- jayantid\n",
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


def review(login, state="COMMENTED", user_type="User", submitted_at=None):
    filed = {"user": {"login": login, "type": user_type}, "state": state}
    if submitted_at is not None:
        filed["submitted_at"] = submitted_at
    return filed


class FakeAPI:
    """Just enough of `GitHubAPI` for the functions that call it, `main` included."""

    def __init__(self, pulls=(), commits=None, check_runs=None, reviews=None, files=None):
        self.repo = "gke-labs/kube-agents"
        self.pulls = list(pulls)
        self.commits = commits or {}
        self.check_runs = check_runs or {}
        self.reviews = reviews or {}
        self.files = files or {}
        self.posts = []

    def get_all(self, path):
        if path.endswith("/pulls?state=open"):
            return self.pulls
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
        matched = re.search(r"/commits/([0-9a-f]+)/check-runs$", path)
        if matched:
            return {"check_runs": self.check_runs.get(matched.group(1), [])}
        matched = re.search(r"/pulls/(\d+)$", path)
        if matched:
            return next(pull for pull in self.pulls if pull["number"] == int(matched.group(1)))
        raise AssertionError(f"unexpected call: {path}")

    def post(self, path, payload=None):
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


class SelectionTest(unittest.TestCase):
    """`select_reviewers` and the two functions under it."""

    def select(self, changed_files, author="author"):
        return rr.select_reviewers(CONFIG, changed_files, author, rng=random.Random(0))

    def test_ordinary_change_falls_to_the_catch_all_group(self):
        matched = rr.reviewers_by_changed_files(CONFIG, ["README.md"], "author")
        self.assertEqual(matched, OWNERS)

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
        self.assertEqual(self.select([".github/workflows/validate.yml"])[0] in OWNERS, True)

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
                self.assertEqual(rr.reviewers_by_changed_files(CONFIG, [path], "author"), OWNERS)

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
        self.assertIn(picked[0], [name for name in OWNERS if name != "jayantid"])

    def test_the_author_is_never_requested(self):
        matched = rr.reviewers_by_changed_files(CONFIG, ["README.md"], "bradhoekstra")
        self.assertNotIn("bradhoekstra", matched)
        self.assertEqual(matched, [name for name in OWNERS if name != "bradhoekstra"])

    def test_number_of_reviewers_caps_the_request(self):
        picked = self.select(["README.md"])
        self.assertEqual(len(picked), 1)
        self.assertIn(picked[0], OWNERS)

    def test_sampling_is_reproducible_for_a_given_seed(self):
        first = rr.select_reviewers(CONFIG, ["README.md"], "author", rng=random.Random(7))
        second = rr.select_reviewers(CONFIG, ["README.md"], "author", rng=random.Random(7))
        self.assertEqual(first, second)

    def test_fewer_candidates_than_requested_is_not_an_error(self):
        config = dict(CONFIG, options=dict(CONFIG["options"], number_of_reviewers=5))
        picked = rr.select_reviewers(config, ["README.md"], "author", rng=random.Random(0))
        self.assertCountEqual(picked, OWNERS)

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
        # The same property docs/pull-request-workflow.md states of the config:
        # everyone the bot can assign is an approver for what it assigns them.
        # If this fails, one of OWNERS, OWNERS_ALIASES, hack/OWNERS or the
        # config moved and the other did not.
        root_approvers = rr.applicable_approvers(["README.md"], REPO_ROOT)
        self.assertTrue(set(OWNERS) <= root_approvers, root_approvers)
        self.assertEqual(rr.applicable_approvers(["hack/eval/presubmit-cases.txt"], REPO_ROOT), set(EVAL_CREW))
        self.assertNotIn(NON_APPROVER, root_approvers)


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


class MainTest(unittest.TestCase):
    """`main` -- the check-run path and the `/request-review` override end to end."""

    COMMENT_ID = 5719654130
    REQUESTED = "/repos/gke-labs/kube-agents/pulls/1/requested_reviewers"
    REACTIONS = f"/repos/gke-labs/kube-agents/issues/comments/{COMMENT_ID}/reactions"

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        write_tree(self.root.name, OWNERS_TREE)
        self.summary = Path(self.root.name) / "summary.md"
        env = {"GITHUB_TOKEN": "t", rr.STEP_SUMMARY_ENV: str(self.summary)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, pull, reviews, *extra):
        self.api = FakeAPI(pulls=[pull], reviews={1: reviews}, files={1: ["README.md"]})
        argv = ["--pr", "1", "--config", str(LIVE_CONFIG), "--owners-root", self.root.name, "--seed", "0", *extra]
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(rr, "GitHubAPI", return_value=self.api):
            with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
                self.code = rr.main(argv)
        return [path for path, _ in self.api.posts]

    def reaction(self):
        return [payload["content"] for path, payload in self.api.posts if path == self.REACTIONS]

    def test_a_non_approvers_approval_no_longer_blocks_the_check_run_path(self):
        posts = self.run_main(pull_request(), [review(NON_APPROVER, "APPROVED")])
        self.assertEqual(posts, [self.REQUESTED])
        self.assertEqual(self.code, 0)

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
        self.assertEqual(posts, [self.REQUESTED, self.REACTIONS])
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
