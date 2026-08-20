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

import random
import re
import unittest

import request_reviewers as rr

# The live config at .github/auto_request_review.yml, in the order that file
# lists the globs -- `last_files_match_only` makes that order the deciding
# input, so a test fixture that reorders them tests nothing.
CONFIG = {
    "reviewers": {
        "defaults": ["repository-owners"],
        "groups": {
            "repository-owners": ["bradhoekstra", "jayantid", "toshiowang", "dshnayder"],
            "waw-leads": ["fatoshoti", "mateuszklinowski", "mplakhtiy"],
        },
    },
    "files": {
        "**": ["repository-owners"],
        "k8s-operator/**": ["waw-leads"],
        ".github/workflows/k8s-operator-test.yml": ["waw-leads"],
        ".github/workflows/staging-redeploy-*.yml": ["waw-leads"],
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
WAW = CONFIG["reviewers"]["groups"]["waw-leads"]


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


def review(login, state="COMMENTED", user_type="User"):
    return {"user": {"login": login, "type": user_type}, "state": state}


class FakeAPI:
    """Just enough of `GitHubAPI` for the two functions that call it."""

    def __init__(self, pulls=(), commits=None, check_runs=None):
        self.repo = "gke-labs/kube-agents"
        self.pulls = list(pulls)
        self.commits = commits or {}
        self.check_runs = check_runs or {}

    def get_all(self, path):
        if path.endswith("/pulls?state=open"):
            return self.pulls
        matched = re.search(r"/pulls/(\d+)/commits$", path)
        if matched:
            return [{"sha": sha} for sha in self.commits.get(int(matched.group(1)), [])]
        raise AssertionError(f"unexpected list call: {path}")

    def get(self, path):
        matched = re.search(r"/commits/([0-9a-f]+)/check-runs$", path)
        if matched:
            return {"check_runs": self.check_runs.get(matched.group(1), [])}
        raise AssertionError(f"unexpected call: {path}")


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
        # The dotfile rule, and the reason `.github/workflows/...` needs its own
        # literal entries in the config: `**` does not reach them.
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

    def test_last_matching_glob_wins(self):
        # Both `**` and `k8s-operator/**` match, and the later entry replaces
        # the earlier one rather than adding to it.
        matched = rr.reviewers_by_changed_files(CONFIG, ["README.md", "k8s-operator/main.go"], "author")
        self.assertEqual(matched, WAW)

    def test_a_literal_dot_entry_still_matches(self):
        matched = rr.reviewers_by_changed_files(
            CONFIG, [".github/workflows/k8s-operator-test.yml"], "author"
        )
        self.assertEqual(matched, WAW)

    def test_dotfile_only_change_matches_no_glob_and_uses_defaults(self):
        # `**` cannot reach `.github/workflows/validate.yml`, so nothing matches
        # and the defaults carry it.
        self.assertEqual(rr.reviewers_by_changed_files(CONFIG, [".github/workflows/validate.yml"], "author"), [])
        self.assertEqual(self.select([".github/workflows/validate.yml"])[0] in OWNERS, True)

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
        picked = rr.select_reviewers(config, ["k8s-operator/main.go"], "author", rng=random.Random(0))
        self.assertCountEqual(picked, WAW)

    def test_teams_are_split_from_users(self):
        users, teams = rr.split_teams(["bradhoekstra", "team:sre"])
        self.assertEqual(users, ["bradhoekstra"])
        self.assertEqual(teams, ["sre"])


class SkipReasonTest(unittest.TestCase):
    """`skip_reason` -- the pull request states that get no reviewer."""

    def test_an_open_untouched_pull_request_is_not_skipped(self):
        self.assertIsNone(rr.skip_reason(pull_request(), [], CONFIG))

    def test_a_closed_pull_request_is_skipped(self):
        self.assertIn("not open", rr.skip_reason(pull_request(state="closed"), [], CONFIG))

    def test_a_draft_is_skipped(self):
        self.assertIn("draft", rr.skip_reason(pull_request(draft=True), [], CONFIG))

    def test_a_draft_is_not_skipped_when_ignore_draft_is_off(self):
        config = dict(CONFIG, options=dict(CONFIG["options"], ignore_draft=False))
        self.assertIsNone(rr.skip_reason(pull_request(draft=True), [], config))

    def test_an_ignored_keyword_in_the_title_is_skipped(self):
        skipped = rr.skip_reason(pull_request(title="DO NOT REVIEW: wip"), [], CONFIG)
        self.assertIn("DO NOT REVIEW", skipped)

    def test_an_existing_request_is_not_duplicated(self):
        # The workflow fires on every completed AI Review check, so a pull
        # request already handed to a human must not be handed over again.
        skipped = rr.skip_reason(
            pull_request(requested_reviewers=[{"login": "jayantid"}]), [], CONFIG
        )
        self.assertIn("jayantid", skipped)

    def test_an_existing_team_request_is_not_duplicated(self):
        skipped = rr.skip_reason(pull_request(requested_teams=[{"slug": "sre"}]), [], CONFIG)
        self.assertIn("team:sre", skipped)

    def test_a_human_verdict_already_submitted_is_skipped(self):
        for state in ("APPROVED", "CHANGES_REQUESTED"):
            reviews = [review("bnaylor", state)]
            self.assertIn("bnaylor", rr.skip_reason(pull_request(), reviews, CONFIG), state)

    def test_the_bots_own_review_does_not_count_as_human_coverage(self):
        reviews = [review("kube-agents-bot[bot]", user_type="Bot")]
        self.assertIsNone(rr.skip_reason(pull_request(), reviews, CONFIG))

    def test_the_authors_replies_to_the_bot_do_not_block_their_own_reviewer(self):
        # Answering a review thread files a `COMMENTED` review under the
        # replier's name, and AGENTS.md tells authors to answer every finding
        # before running `/review`. Counting those would starve exactly the pull
        # requests that follow the process.
        reviews = [review("kube-agents-bot[bot]", user_type="Bot"), review("author")]
        self.assertIsNone(rr.skip_reason(pull_request(), reviews, CONFIG))

    def test_a_drive_by_comment_from_a_colleague_does_not_block_it_either(self):
        self.assertIsNone(rr.skip_reason(pull_request(), [review("bnaylor")], CONFIG))

    def test_a_verdict_from_the_author_is_still_the_author(self):
        # GitHub will not let you approve your own pull request, but a
        # `CHANGES_REQUESTED` on your own is possible and is not coverage.
        reviews = [review("author", "CHANGES_REQUESTED")]
        self.assertIsNone(rr.skip_reason(pull_request(), reviews, CONFIG))


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
