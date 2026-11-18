"""Tests for post_merge_report: one comment per outcome under ``report``, none under ``silent``."""

import io
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kubeagents_switches
import post_merge_check
import post_merge_report
from test_post_merge_check import schema

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
REPO = "https://github.com/acme/fleet"
MERGE = "3d1a1b0bf1b13e4e40a0301114750aadf81bae38"
MARKER = post_merge_report.COMMENT_MARKER


def add_record(conn, number, outcome="verified_fixed", detail="re-check: clean", outcome_at=NOW, reported_at=None):
    conn.execute(
        "INSERT INTO post_merge_records (url, repo, number, kind, cluster, namespace, object, merge_commit, outcome, "
        "outcome_detail, outcome_at, reported_at) VALUES (?, ?, ?, 'no-pdb', 'prod', 'shop', 'Deployment/checkout', "
        "?, ?, ?, ?, ?)",
        (f"{REPO}/pull/{number}", REPO, number, MERGE, outcome, detail,
         outcome_at.isoformat() if outcome_at else None, reported_at),
    )


class Forge:
    """Comments per pull request; ``truncated`` makes the marker read answer None."""

    def __init__(self):
        self.comments = []
        self.reads = []
        self.truncated = False

    def comment(self, repo, number, body):
        self.comments.append((number, body))

    def has_marker(self, repo, number):
        self.reads.append(number)
        if any(MARKER in body for n, body in self.comments if n == number):
            return True
        return None if self.truncated else False


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(schema())
        self.forge = Forge()

    def report(self, mode="report", minutes=0, limit=post_merge_check.READS_PER_TICK, comment=None):
        return post_merge_report.report_outcomes(self.conn, mode, comment or self.forge.comment,
                                                 self.forge.has_marker, NOW + timedelta(minutes=minutes), limit)

    def reported_at(self, number=1):
        return self.conn.execute("SELECT reported_at FROM post_merge_records WHERE number = ?", (number,)).fetchone()[0]

    def test_report_comments_once_with_the_marker(self):
        add_record(self.conn, 1)
        seen = self.report()
        self.assertEqual(seen["commented"], [f"{REPO}/pull/1"])
        self.assertEqual(len(self.forge.comments), 1)
        body = self.forge.comments[0][1]
        self.assertTrue(body.startswith("**Verified fixed.**"))
        self.assertIn(MERGE[:12], body)
        self.assertNotIn(MERGE[:13], body)
        self.assertIn("`Deployment/checkout` in `shop` on `prod`", body)
        self.assertTrue(body.endswith(MARKER))
        self.assertEqual(self.reported_at(), NOW.isoformat())
        self.report(minutes=5)
        self.assertEqual(len(self.forge.comments), 1, "never twice")
        self.assertEqual(self.forge.reads, [1], "a reported record is not read again")

    def test_silent_stamps_the_outcome_and_never_comments_it_later(self):
        add_record(self.conn, 1)
        seen = self.report(mode="silent")
        self.assertEqual(seen["silent"], [f"{REPO}/pull/1"])
        self.assertTrue(self.reported_at().startswith(post_merge_report.SILENT_PREFIX))
        self.report(mode="report", minutes=5)
        self.assertEqual((self.forge.comments, self.forge.reads), ([], []))

    def test_a_record_without_an_outcome_is_not_reported(self):
        add_record(self.conn, 1, outcome=None, detail=None, outcome_at=None)
        self.assertEqual(self.report()["commented"], [])
        self.assertIsNone(self.reported_at())

    def test_a_comment_that_landed_before_a_crash_is_not_posted_again(self):
        add_record(self.conn, 1)
        self.forge.comments.append((1, "earlier\n\n" + MARKER))
        seen = self.report()
        self.assertEqual(len(self.forge.comments), 1)
        self.assertEqual(seen["commented"], [f"{REPO}/pull/1"])
        self.assertEqual(self.reported_at(), NOW.isoformat())

    def test_too_many_comments_to_check_means_no_comment(self):
        add_record(self.conn, 1)
        self.forge.truncated = True
        seen = self.report()
        self.assertEqual((self.forge.comments, seen["unread"]), ([], [f"{REPO}/pull/1"]))
        self.assertTrue(self.reported_at().startswith(post_merge_report.UNREAD_PREFIX))
        self.forge.truncated = False
        self.report(minutes=5)
        self.assertEqual(self.forge.comments, [], "an unread record is not tried again")

    def test_a_comment_the_forge_keeps_refusing_is_retried_then_given_up_after_a_day(self):
        add_record(self.conn, 1)

        def refuse(repo, number, body):
            raise RuntimeError("403 locked")

        with self.assertLogs("post_merge_check", level="WARNING"):
            seen = self.report(comment=refuse, minutes=60)
        self.assertEqual((seen["failed"], seen["errors"]), ([f"{REPO}/pull/1"], []))
        self.assertTrue(self.reported_at().startswith(post_merge_report.RETRY_PREFIX))
        with self.assertLogs("post_merge_check", level="WARNING"):
            seen = self.report(comment=refuse, minutes=25 * 60)
        self.assertEqual(seen["given_up"], [f"{REPO}/pull/1"])
        self.assertEqual(seen["errors"], [f"{REPO}/pull/1"])
        self.assertTrue(self.reported_at().startswith(post_merge_report.GAVE_UP_PREFIX))
        self.report(minutes=26 * 60)
        self.assertEqual(self.forge.comments, [], "a given-up record is not tried again")

    def test_a_retried_comment_lands_on_a_later_tick(self):
        add_record(self.conn, 1)
        with self.assertLogs("post_merge_check", level="WARNING"):
            self.report(comment=lambda r, n, b: (_ for _ in ()).throw(TimeoutError("hop dropped")))
        self.assertEqual(self.report(minutes=5)["commented"], [f"{REPO}/pull/1"])
        self.assertEqual(len(self.forge.comments), 1)

    def test_a_refused_comment_goes_behind_the_untried_ones(self):
        for number in (1, 2, 3):
            add_record(self.conn, number)
        tried = []

        def comment(repo, number, body):
            tried.append(number)
            if number == 1:
                raise RuntimeError("403 locked")

        with self.assertLogs("post_merge_check", level="WARNING"):
            self.report(comment=comment, limit=1)
        self.report(comment=comment, limit=1)
        self.report(comment=comment, limit=1)
        self.assertEqual(tried, [1, 2, 3])

    def test_report_reads_at_most_limit_pull_requests_a_tick_and_silent_is_not_capped(self):
        for number in (1, 2, 3):
            add_record(self.conn, number)
        seen = self.report(limit=2)
        self.assertEqual(len(seen["commented"]), 2)
        self.assertIsNone(self.reported_at(3))
        seen = self.report(mode="silent", limit=0)
        self.assertEqual(seen["silent"], [f"{REPO}/pull/3"])

    def test_a_database_that_refuses_the_stamp_is_an_error(self):
        add_record(self.conn, 1)
        self.conn.execute(
            "CREATE TRIGGER no_stamp BEFORE UPDATE OF reported_at ON post_merge_records "
            "BEGIN SELECT RAISE(ABORT, 'read-only'); END"
        )
        with self.assertLogs("post_merge_check", level="WARNING"):
            seen = self.report(mode="silent")
        self.assertEqual((seen["errors"], seen["silent"]), ([f"{REPO}/pull/1"], []))


class CommentTextTest(unittest.TestCase):
    def text(self, outcome, detail, merge_commit=MERGE):
        return post_merge_report.comment_text({
            "object": "Deployment/x", "namespace": "ns", "cluster": "c", "merge_commit": merge_commit,
            "outcome": outcome, "outcome_detail": detail,
        })

    def test_each_outcome_and_category_gets_its_own_line(self):
        cases = (
            ("verified_fixed", "re-check: clean", "**Verified fixed.**"),
            ("still_broken", "re-check: the audit's check still fires", "audit's check still fires"),
            ("still_broken", "health: pod restarted", "did not stay healthy"),
            ("applied_not_rechecked", "re-check: could not judge", "**Applied, not re-checked.**"),
            ("applied_not_rechecked", "unreadable: workload gone", "**Applied, not checked.**"),
            ("not_applied", "unreadable: cluster did not answer", "could not read `Deployment/x`"),
            ("not_applied", "apply: not live 60 minutes after the merge", "**Not applied.**"),
            ("not_applied", "out of scope: c is excluded", "left this install's declared scope"),
            ("applied_not_rechecked", "scope unknown: project unread", "could not confirm that `c`"),
        )
        for outcome, detail, expected in cases:
            with self.subTest(detail):
                body = self.text(outcome, detail)
                self.assertIn(expected, body)
                self.assertTrue(body.endswith(MARKER))

    def test_the_comment_carries_no_detail_text(self):
        body = self.text("still_broken", "health: pod x-1 is not Ready <script>")
        self.assertNotIn("<script>", body)
        self.assertNotIn("x-1", body)

    def test_no_merge_commit_reads_as_the_merge(self):
        self.assertIn("the merge is live", self.text("still_broken", "health: x", merge_commit=None))


class ForgeCallsTest(unittest.TestCase):
    def test_forge_marker_and_comment_calls(self):
        sent = []
        answer = {"comments": [{"body": "hello"}], "commentsTruncated": False}
        fake = types.ModuleType("forge")
        fake.call = lambda verb, payload, repo, **kw: sent.append((verb, payload, repo)) or answer
        with mock.patch.dict(sys.modules, {"forge": fake}):
            self.assertFalse(post_merge_report.forge_has_marker(REPO, 3))
            self.assertEqual(sent[-1], ("proposal-view", {"number": 3, "comments": True, "limit": 100}, REPO))
            answer["commentsTruncated"] = True
            self.assertIsNone(post_merge_report.forge_has_marker(REPO, 3))
            answer["comments"].append({"body": "x " + MARKER})
            self.assertTrue(post_merge_report.forge_has_marker(REPO, 3), "a marker found ends the question")
            post_merge_report.forge_comment(REPO, 3, "body")
        self.assertEqual(sent[-1], ("proposal-comment", {"number": 3, "body": "body"}, REPO))


class MainTest(unittest.TestCase):
    def run_main(self, tmp, value):
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"KUBEAGENTS_POST_MERGE_CHECK": value,
                                            "SESSION_KV_DB_PATH": str(Path(tmp) / "kv.db")}), \
                mock.patch.object(kubeagents_switches, "MANAGED_DOTENV_PATH", Path(tmp) / "no-env"), \
                mock.patch.object(kubeagents_switches, "SWITCH_DIR", Path(tmp) / "no-dir"), \
                redirect_stdout(out):
            self.assertEqual(post_merge_check.main(["report"]), 0)
        return json.loads(out.getvalue())

    def test_report_runs_one_tick_in_the_switchs_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            with sqlite3.connect(str(Path(tmp) / "kv.db")) as conn:
                conn.execute(schema())
                add_record(conn, 1)
            fake = types.ModuleType("forge")
            fake.call = mock.Mock(side_effect=AssertionError("off and silent post nothing"))
            with mock.patch.dict(sys.modules, {"forge": fake}):
                self.assertEqual(self.run_main(tmp, "off"), {"switch": "off"})
                self.assertEqual(self.run_main(tmp, "silent")["silent"], [f"{REPO}/pull/1"])
            with sqlite3.connect(str(Path(tmp) / "kv.db")) as conn:
                add_record(conn, 2)
            fake.call = mock.Mock(return_value={"comments": [], "commentsTruncated": False})
            with mock.patch.dict(sys.modules, {"forge": fake}):
                self.assertEqual(self.run_main(tmp, "report")["commented"], [f"{REPO}/pull/2"])
            self.assertEqual([c.args[0] for c in fake.call.call_args_list], ["proposal-view", "proposal-comment"])


if __name__ == "__main__":
    unittest.main()
