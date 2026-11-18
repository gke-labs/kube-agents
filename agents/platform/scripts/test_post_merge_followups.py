"""Tests for post_merge_followups: step-down with an attempt cap, the gated offer, and revert notices."""

import json
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import post_merge_followups as pmf
import post_merge_stepdown
from test_post_merge_check import schema

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
REPO = "https://github.com/acme/fleet"


def add_record(conn, number, outcome="still_broken", environment="dev", outcome_at=NOW, **extra):
    columns = {"url": f"{REPO}/pull/{number}", "repo": REPO, "number": number, "kind": "no-pdb",
               "environment": environment, "outcome": outcome, "outcome_at": outcome_at.isoformat(), **extra}
    conn.execute(f"INSERT INTO post_merge_records ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                 tuple(columns.values()))


class Script:
    """The sandbox scripts: answers per script name, every call recorded."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def __call__(self, name, args):
        self.calls.append((name, [str(a) for a in args]))
        answer = self.answers[name]
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


class FollowupsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(schema())
        self.comments = []
        self.marker = False
        self.ready = {"clean": 5, "days": 2, "ready": True}
        self.ready_calls = []

    def ready_in(self, conn, *, repo, kind, environment, now):
        self.ready_calls.append((repo, kind, environment))
        return self.ready

    def follow(self, script, minutes=0, limit=20):
        return pmf.run_followups(self.conn, script, ready_in=self.ready_in,
                                 comment=lambda r, n, b: self.comments.append((n, b)),
                                 has_marker=lambda r, n, m: self.marker, now=NOW + timedelta(minutes=minutes),
                                 limit=limit)

    def row(self, number=1):
        self.conn.row_factory = sqlite3.Row
        found = dict(self.conn.execute("SELECT * FROM post_merge_records WHERE number = ?", (number,)).fetchone())
        self.conn.row_factory = None
        return found

    def test_the_marker_and_branch_match_the_step_down_script(self):
        self.assertEqual(pmf.STEPDOWN_MARKER, post_merge_stepdown.MARKER)
        self.assertEqual(pmf.REVERT_BRANCH, post_merge_stepdown.REVERT_BRANCH)

    def test_still_broken_runs_the_step_down_once(self):
        add_record(self.conn, 1)
        script = Script(**{pmf.STEPDOWN_SCRIPT: {"action": "stepped-down", "revert": "r"}})
        seen = self.follow(script)
        self.assertEqual(seen["stepped-down"], [f"{REPO}/pull/1"])
        self.assertEqual(script.calls, [(pmf.STEPDOWN_SCRIPT, ["--repo", REPO, "--number", "1", "--kind", "no-pdb",
                                                               "--environment", "dev", "--outcome", "still_broken"])])
        self.assertEqual(self.row()["followup_state"], "stepped-down")
        self.follow(script, minutes=5)
        self.assertEqual(len(script.calls), 1, "a finished follow-up is not run again")

    def test_a_step_down_that_keeps_retrying_is_capped_and_says_so_once(self):
        add_record(self.conn, 1)
        script = Script(**{pmf.STEPDOWN_SCRIPT: {"action": "retry", "errors": ["revert: diff too large"]}})
        for attempt in range(1, pmf.FOLLOWUP_ATTEMPTS):
            self.assertEqual(self.follow(script, minutes=attempt)["retry"], [f"{REPO}/pull/1"])
            self.assertEqual(self.comments, [])
        seen = self.follow(script, minutes=pmf.FOLLOWUP_ATTEMPTS)
        self.assertEqual((seen["failed"], seen["errors"]), ([f"{REPO}/pull/1"], [f"{REPO}/pull/1"]))
        self.assertEqual(len(script.calls), pmf.FOLLOWUP_ATTEMPTS)
        row = self.row()
        self.assertEqual((row["followup_state"], row["followup_attempts"]), ("failed", pmf.FOLLOWUP_ATTEMPTS))
        self.assertEqual(json.loads(row["followup_detail"])["gaveUp"], "commented")
        [(number, body)] = self.comments
        self.assertEqual(number, 1)
        self.assertIn("revert: diff too large", body)
        self.assertIn("could not open a revert or a pull request to stop auto-merging no-pdb fixes in dev", body)
        self.assertIn("Revert this change by hand", body)
        self.assertTrue(body.endswith(post_merge_stepdown.MARKER))
        self.follow(script, minutes=30)
        self.assertEqual((len(script.calls), len(self.comments)), (pmf.FOLLOWUP_ATTEMPTS, 1))

    def test_a_script_that_does_not_answer_counts_as_an_attempt(self):
        add_record(self.conn, 1)
        script = Script(**{pmf.STEPDOWN_SCRIPT: RuntimeError("post_merge_stepdown.py exited 1 without an answer")})
        with self.assertLogs("post_merge_check", level="WARNING"):
            self.follow(script)
        self.assertEqual((self.row()["followup_state"], self.row()["followup_attempts"]), ("retry", 1))

    def test_a_give_up_comment_already_there_is_not_posted_again(self):
        add_record(self.conn, 1, followup_state="retry", followup_attempts=pmf.FOLLOWUP_ATTEMPTS - 1)
        self.marker = True
        self.follow(Script(**{pmf.STEPDOWN_SCRIPT: {"action": "retry", "reason": "x"}}))
        self.assertEqual(self.comments, [])
        self.assertEqual(json.loads(self.row()["followup_detail"])["gaveUp"], "already commented")

    def test_a_give_up_comment_that_fails_is_tried_again_without_rerunning_the_script(self):
        add_record(self.conn, 1, followup_state="retry", followup_attempts=pmf.FOLLOWUP_ATTEMPTS - 1)
        script = Script(**{pmf.STEPDOWN_SCRIPT: {"action": "retry", "reason": "base is protected"}})

        def refuse(repo, number, body):
            raise RuntimeError("403")

        with self.assertLogs("post_merge_check", level="WARNING"):
            pmf.run_followups(self.conn, script, ready_in=self.ready_in, comment=refuse,
                              has_marker=lambda r, n, m: False, now=NOW)
        self.assertEqual(self.row()["followup_state"], "retry")
        self.follow(script, minutes=5)
        self.assertEqual(len(script.calls), 1)
        self.assertEqual(self.row()["followup_state"], "failed")
        self.assertIn("base is protected", self.comments[0][1])

    def test_a_long_answer_is_stored_whole_and_a_capped_run_reads_it_back(self):
        # followup_detail used to be cut mid-JSON, and the capped run's json.loads then aborted the tick.
        add_record(self.conn, 1, followup_state="retry", followup_attempts=pmf.FOLLOWUP_ATTEMPTS - 1)
        add_record(self.conn, 2, outcome_at=NOW - timedelta(minutes=1))
        long_errors = {"action": "retry", "errors": ["revert: " + "x" * 400, "step-down: " + "y" * 400]}
        script = Script(**{pmf.STEPDOWN_SCRIPT: long_errors})

        def refuse(repo, number, body):
            raise RuntimeError("403")

        with self.assertLogs("post_merge_check", level="WARNING"):
            pmf.run_followups(self.conn, script, ready_in=self.ready_in, comment=refuse,
                              has_marker=lambda r, n, m: False, now=NOW)
        detail = self.row()["followup_detail"]
        self.assertLessEqual(len(detail), pmf.DETAIL_CHARS)
        json.loads(detail)
        seen = self.follow(script, minutes=5)
        self.assertEqual(seen["failed"], [f"{REPO}/pull/1"])
        self.assertIn("revert: xxx", self.comments[0][1])
        self.assertEqual(self.row(2)["followup_state"], "retry", "the record behind it still ran")

    def test_a_detail_that_is_not_json_still_gives_up(self):
        add_record(self.conn, 1, followup_state="retry", followup_attempts=pmf.FOLLOWUP_ATTEMPTS,
                   followup_detail='{"action": "retry", "reason": "cut')
        self.follow(Script())
        self.assertEqual(self.row()["followup_state"], "failed")
        self.assertEqual(len(self.comments), 1)

    def test_a_retry_that_ages_out_before_the_cap_still_gets_the_give_up_comment(self):
        add_record(self.conn, 1, outcome_at=NOW - timedelta(days=1, minutes=1), followup_state="retry",
                   followup_attempts=2, followup_detail=json.dumps({"action": "retry", "reason": "base is protected"}))
        script = Script()
        seen = self.follow(script)
        self.assertEqual((seen["failed"], script.calls), ([f"{REPO}/pull/1"], []))
        self.assertIn("base is protected", self.comments[0][1])

    def test_an_aged_out_give_up_the_forge_keeps_refusing_ends_after_two_days(self):
        add_record(self.conn, 1, outcome_at=NOW - timedelta(days=2, minutes=1), followup_state="retry",
                   followup_attempts=2)

        def refuse(repo, number, body):
            raise RuntimeError("403")

        with self.assertLogs("post_merge_check", level="WARNING"):
            pmf.run_followups(self.conn, Script(), ready_in=self.ready_in, comment=refuse,
                              has_marker=lambda r, n, m: False, now=NOW)
        self.assertEqual(self.row()["followup_state"], "failed")
        self.assertIn("not commented", json.loads(self.row()["followup_detail"])["gaveUp"])

    def test_an_unreadable_outcome_time_is_old_not_new(self):
        add_record(self.conn, 1)
        self.conn.execute("UPDATE post_merge_records SET outcome_at = 'garbage'")
        script = Script()
        self.follow(script)
        self.assertEqual((script.calls, self.row()["followup_state"]), ([], None))

    def test_the_give_up_names_a_revert_that_did_open(self):
        body = pmf.give_up_text({"kind": "no-pdb", "environment": "dev"},
                                {"action": "retry", "revert": "https://x/pull/9", "errors": ["step-down: 502"]})
        self.assertIn("I opened a revert (https://x/pull/9), but could not open a pull request to stop", body)
        self.assertNotIn("by hand", body)

    def test_too_many_comments_gives_up_without_a_comment(self):
        add_record(self.conn, 1, followup_state="retry", followup_attempts=pmf.FOLLOWUP_ATTEMPTS - 1)
        self.marker = None
        self.follow(Script(**{pmf.STEPDOWN_SCRIPT: {"action": "retry"}}))
        self.assertEqual(self.comments, [])
        self.assertEqual(self.row()["followup_state"], "failed")

    def test_verified_fixed_offers_only_when_ready_in_says_ready(self):
        add_record(self.conn, 1, outcome="verified_fixed")
        script = Script(**{pmf.OFFER_SCRIPT: {"action": "offered", "offer": "o"}})
        self.ready = {"clean": 5, "days": 1, "ready": False}
        seen = self.follow(script)
        self.assertEqual((seen["none"], script.calls), ([f"{REPO}/pull/1"], []))
        self.assertIn("not ready", json.loads(self.row()["followup_detail"])["reason"])
        self.assertEqual(self.ready_calls, [(REPO, "no-pdb", "dev")])

    def test_a_ready_kind_runs_the_offer_without_counts(self):
        add_record(self.conn, 1, outcome="verified_fixed")
        script = Script(**{pmf.OFFER_SCRIPT: {"action": "offered", "offer": "o"}})
        self.assertEqual(self.follow(script)["offered"], [f"{REPO}/pull/1"])
        self.assertEqual(script.calls, [(pmf.OFFER_SCRIPT, ["--repo", REPO, "--number", "1", "--kind", "no-pdb",
                                                            "--environment", "dev"])])

    def test_a_capped_offer_gives_up_without_a_comment(self):
        add_record(self.conn, 1, outcome="verified_fixed", followup_state="retry",
                   followup_attempts=pmf.FOLLOWUP_ATTEMPTS - 1)
        self.follow(Script(**{pmf.OFFER_SCRIPT: {"action": "retry"}}))
        self.assertEqual((self.row()["followup_state"], self.comments), ("failed", []))

    def test_a_record_without_an_environment_waits(self):
        add_record(self.conn, 1, environment=None)
        script = Script()
        self.assertEqual(self.follow(script)["waiting"], [f"{REPO}/pull/1"])
        self.assertEqual(script.calls, [])
        self.assertIsNone(self.row()["followup_state"])

    def test_old_outcomes_and_other_outcomes_are_not_followed_up(self):
        add_record(self.conn, 1, outcome_at=NOW - timedelta(days=2))
        add_record(self.conn, 2, outcome="applied_not_rechecked")
        add_record(self.conn, 3, outcome="not_applied")
        script = Script()
        self.follow(script)
        self.assertEqual(script.calls, [])

    def test_at_most_limit_scripts_run(self):
        for number in (1, 2, 3):
            add_record(self.conn, number, outcome_at=NOW - timedelta(minutes=number))
        script = Script(**{pmf.STEPDOWN_SCRIPT: {"action": "stepped-down"}})
        self.follow(script, limit=2)
        self.assertEqual([args[3] for _, args in script.calls], ["1", "2"])


class ForgeHelpersTest(unittest.TestCase):
    def test_marker_comment_and_branch_calls(self):
        import forge as real

        sent = []
        answers = {"identity": {"identity": {"login": "kube-agents[bot]"}},
                   "proposal-view": {"comments": [{"author": "mallory", "body": "M"}], "commentsTruncated": False},
                   "proposal-list": {"proposals": [{"number": 4}, {"number": 9, "state": "merged"}]}}
        fake = types.ModuleType("forge")
        fake.normalise_login = real.normalise_login
        fake.call = lambda verb, payload, repo, **kw: sent.append((verb, payload)) or answers.get(verb, {})
        with mock.patch.dict(sys.modules, {"forge": fake}):
            self.assertFalse(pmf.forge_has_marker(REPO, 3, "M"), "someone else's marker does not count")
            answers["proposal-view"]["commentsTruncated"] = True
            self.assertIsNone(pmf.forge_has_marker(REPO, 3, "M"))
            answers["proposal-view"]["comments"].append({"author": "kube-agents", "body": "a M"})
            self.assertTrue(pmf.forge_has_marker(REPO, 3, "M"))
            pmf.forge_comment(REPO, 3, "b")
            self.assertEqual(pmf.forge_branch_proposal(REPO, "platform-agent/revert-3")["number"], 9)
        self.assertIn(("proposal-comment", {"number": 3, "body": "b"}), sent)

    def test_last_json_document(self):
        self.assertEqual(pmf.last_json_document('log line\n{\n  "action": "none"\n}\n'), {"action": "none"})
        self.assertIsNone(pmf.last_json_document("Traceback\n"))


class NoticeRevertsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(schema())

    def test_a_merged_revert_stamps_reverted_at(self):
        add_record(self.conn, 1, followup_state="stepped-down")
        add_record(self.conn, 2, followup_state="stepped-down")
        add_record(self.conn, 3, followup_state="stepped-down", outcome_at=NOW - timedelta(days=9))
        add_record(self.conn, 4, followup_state="none")
        branches = []

        def view(repo, branch):
            branches.append(branch)
            return {"state": "merged", "closed": "2026-11-12T10:00:00Z"} if branch.endswith("-1") else {"state": "open"}

        seen = pmf.notice_reverts(self.conn, view, NOW)
        self.assertEqual(seen["reverted"], [f"{REPO}/pull/1"])
        self.assertEqual(seen["waiting"], [f"{REPO}/pull/2"])
        self.assertEqual(sorted(branches), ["platform-agent/revert-1", "platform-agent/revert-2"])
        reverted = dict(self.conn.execute("SELECT number, reverted_at FROM post_merge_records").fetchall())
        self.assertEqual(reverted[1], "2026-11-12T10:00:00Z")
        self.assertIsNone(reverted[2])

    def test_a_read_that_fails_is_tried_next_time(self):
        add_record(self.conn, 1, followup_state="stepped-down")

        def broken(repo, branch):
            raise TimeoutError("hop")

        with self.assertLogs("post_merge_check", level="WARNING"):
            self.assertEqual(pmf.notice_reverts(self.conn, broken, NOW)["errors"], [f"{REPO}/pull/1"])


if __name__ == "__main__":
    unittest.main()
