"""Tests for post_merge_edited.py: whether a person edited a merged fix, with a fake forge and clock."""

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
import post_merge_edited
from test_post_merge_check import schema

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
REPO = "https://github.com/acme/fleet"


class FillTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(schema())
        self.asked = []

    def add(self, number, **columns):
        row = {"url": f"{REPO}/pull/{number}", "repo": REPO, "number": number, "kind": "no-pdb",
               "opened_at": (NOW - timedelta(hours=2)).isoformat(), **columns}
        self.conn.execute(f"INSERT INTO post_merge_records ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                          tuple(row.values()))

    def edited_value(self, number):
        return self.conn.execute("SELECT human_edited FROM post_merge_records WHERE number = ?", (number,)).fetchone()[0]

    def edited(self, answers):
        def ask(repo, number):
            self.asked.append(number)
            answer = answers[number]
            if isinstance(answer, Exception):
                raise answer
            return answer
        return ask

    def test_each_merged_record_gets_one_or_zero_and_unknown_stays_null(self):
        for n in (1, 2, 3, 4):
            self.add(n, merged_at=(NOW - timedelta(minutes=n)).isoformat())
        seen = post_merge_edited.fill_human_edited(
            self.conn, self.edited({1: True, 2: False, 3: None, 4: RuntimeError("down")}), NOW)
        self.assertEqual((seen["edited"], seen["clean"], seen["unknown"], seen["errors"]),
                         ([f"{REPO}/pull/1"], [f"{REPO}/pull/2"], [f"{REPO}/pull/3"], [f"{REPO}/pull/4"]))
        self.assertEqual([self.edited_value(n) for n in (1, 2, 3, 4)], [1, 0, None, None])

    def test_unknown_is_asked_again_and_a_filled_record_is_not(self):
        self.add(1, merged_at=NOW.isoformat())
        self.add(2, merged_at=NOW.isoformat(), human_edited=0)
        post_merge_edited.fill_human_edited(self.conn, self.edited({1: None}), NOW)
        post_merge_edited.fill_human_edited(self.conn, self.edited({1: False}), NOW)
        self.assertEqual(self.asked, [1, 1])
        self.assertEqual(self.edited_value(1), 0)

    def test_unmerged_and_old_records_are_not_asked_and_cost_nothing(self):
        self.add(1)
        self.add(2, merged_at=(NOW - post_merge_edited.EDITED_DEADLINE - timedelta(minutes=1)).isoformat())
        self.add(3, merged_at=NOW.isoformat())
        self.add(4, merged_at="not a time")
        post_merge_edited.fill_human_edited(self.conn, self.edited({3: False}), NOW, limit=1)
        self.assertEqual(self.asked, [3])
        self.assertIsNone(self.edited_value(2))
        self.assertIsNone(self.edited_value(4))

    def test_the_deadline_runs_from_when_the_merge_was_noticed(self):
        old = (NOW - post_merge_edited.EDITED_DEADLINE - timedelta(days=3)).isoformat()
        self.add(1, merged_at=old, merge_checked_at=(NOW - timedelta(minutes=5)).isoformat())
        self.add(2, merged_at=old, merge_checked_at=old)
        post_merge_edited.fill_human_edited(self.conn, self.edited({1: False}), NOW)
        self.assertEqual(self.asked, [1])

    def test_at_most_limit_reads_newest_merge_first(self):
        for n in (1, 2, 3):
            self.add(n, merged_at=(NOW - timedelta(minutes=10 * n)).isoformat())
        post_merge_edited.fill_human_edited(self.conn, self.edited({1: False, 2: False, 3: False}), NOW, limit=2)
        self.assertEqual(self.asked, [1, 2])


class ForgeEditedTest(unittest.TestCase):
    INSTALL = {"sha": "a", "author": "kube-agents-app[bot]", "email": "platform-agent@kube-agents.invalid", "bot": True}

    def forge(self, commits, reviews, repo=REPO):
        answers = {"identity": {"identity": {"login": "kube-agents-app[bot]"}},
                   "proposal-commits": commits, "proposal-reviews": reviews}
        fake = types.ModuleType("forge")
        fake.call = lambda verb, payload, repo, **kw: answers[verb]
        with mock.patch.dict(sys.modules, {"forge": fake}):
            return post_merge_edited.forge_human_edited(repo, 1)

    def test_only_the_install_is_clean(self):
        approved = {"id": 1, "state": "approved", "author": "alice", "bot": False}
        self.assertFalse(self.forge({"commits": [self.INSTALL]}, {"reviews": [approved]}))

    def test_a_person_or_a_third_party_bot_commit_is_an_edit(self):
        for other in ({"sha": "b", "author": "alice", "email": "alice@example.com", "bot": False},
                      {"sha": "b", "author": "dependabot[bot]", "email": "bot@example.com", "bot": True}):
            with self.subTest(author=other["author"]):
                self.assertTrue(self.forge({"commits": [self.INSTALL, other]}, {"reviews": []}))

    def test_a_change_request_is_an_edit_unless_dismissed_or_the_installs(self):
        def review(state, author="alice"):
            return {"reviews": [{"id": 1, "state": state, "author": author, "bot": False}]}

        commits = {"commits": [self.INSTALL]}
        self.assertTrue(self.forge(commits, review("changes_requested")))
        self.assertFalse(self.forge(commits, review("dismissed")))
        self.assertFalse(self.forge(commits, review("commented")))
        self.assertFalse(self.forge(commits, review("changes_requested", "kube-agents-app[bot]")))

    def test_an_scp_style_repository_still_matches_the_installs_email(self):
        with mock.patch("vcs_readings.install_commit_emails", return_value=frozenset()) as emails:
            self.forge({"commits": [self.INSTALL]}, {"reviews": []}, repo="git@github.com:acme/infra.git")
        self.assertEqual(emails.call_args.args[0], "github.com")

    def test_a_truncated_listing_is_unknown(self):
        self.assertIsNone(self.forge({"commits": [self.INSTALL], "truncated": True}, {"reviews": []}))
        self.assertIsNone(self.forge({"commits": [self.INSTALL]}, {"reviews": [], "truncated": True}))

    def test_an_edit_on_a_truncated_listing_still_settles_it(self):
        change = {"id": 1, "state": "changes_requested", "author": "alice", "bot": False}
        self.assertTrue(self.forge({"commits": [self.INSTALL], "truncated": True}, {"reviews": [change]}))

    def test_commits_at_the_forge_cap_are_unknown_though_no_page_says_truncated(self):
        pages = {1: {"commits": [self.INSTALL] * 100, "truncated": True},
                 2: {"commits": [self.INSTALL] * 100, "truncated": True},
                 3: {"commits": [self.INSTALL] * 50, "truncated": False}}

        def call(verb, payload, repo, **kw):
            if verb == "identity":
                return {"identity": {"login": "kube-agents-app[bot]"}}
            return pages[payload["page"]] if verb == "proposal-commits" else {"reviews": []}

        fake = types.ModuleType("forge")
        fake.call = call
        with mock.patch.dict(sys.modules, {"forge": fake}):
            self.assertIsNone(post_merge_edited.forge_human_edited(REPO, 1))

    def test_commits_are_read_page_by_page_and_reviews_at_the_conversation_size(self):
        full = [dict(self.INSTALL, sha=str(n)) for n in range(100)]
        pages = {1: {"commits": full, "truncated": True}, 2: {"commits": [self.INSTALL]}}
        asked = []

        def call(verb, payload, repo, **kw):
            asked.append((verb, payload.get("page"), payload.get("limit")))
            if verb == "identity":
                return {"identity": {"login": "kube-agents-app[bot]"}}
            return pages[payload["page"]] if verb == "proposal-commits" else {"reviews": []}

        fake = types.ModuleType("forge")
        fake.call = call
        with mock.patch.dict(sys.modules, {"forge": fake}):
            self.assertFalse(post_merge_edited.forge_human_edited(REPO, 1))
        self.assertEqual(asked[1:], [("proposal-commits", 1, 100), ("proposal-commits", 2, 100),
                                     ("proposal-reviews", None, 1000)])

    def test_no_install_login_is_unknown(self):
        answers = {"identity": {"identity": {"login": ""}}, "proposal-commits": {"commits": [self.INSTALL]},
                   "proposal-reviews": {"reviews": []}}
        fake = types.ModuleType("forge")
        fake.call = lambda verb, payload, repo, **kw: answers[verb]
        with mock.patch.dict(sys.modules, {"forge": fake}), self.assertLogs(level="WARNING"):
            self.assertIsNone(post_merge_edited.forge_human_edited(REPO, 1))

    def test_a_failed_read_is_unknown(self):
        fake = types.ModuleType("forge")
        fake.call = mock.Mock(side_effect=RuntimeError("broker down"))
        with mock.patch.dict(sys.modules, {"forge": fake}):
            self.assertIsNone(post_merge_edited.forge_human_edited(REPO, 1))


class MainTest(unittest.TestCase):
    def test_off_reads_nothing_and_on_runs_one_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kv.db")
            with sqlite3.connect(db) as conn:
                conn.execute(schema())
                conn.execute("INSERT INTO post_merge_records (url, repo, number, kind, opened_at, merged_at) "
                             "VALUES (?, ?, 1, 'no-pdb', ?, ?)",
                             (f"{REPO}/pull/1", REPO, NOW.isoformat(), datetime.now(timezone.utc).isoformat()))
            asked = []
            for value, expected in (("off", {"switch": "off"}), ("silent", None)):
                out = io.StringIO()
                with mock.patch.dict("os.environ", {"KUBEAGENTS_POST_MERGE_CHECK": value, "SESSION_KV_DB_PATH": db}), \
                        mock.patch.object(kubeagents_switches, "MANAGED_DOTENV_PATH", Path(tmp) / "no-env"), \
                        mock.patch.object(kubeagents_switches, "SWITCH_DIR", Path(tmp) / "no-dir"), \
                        redirect_stdout(out):
                    self.assertEqual(post_merge_edited.main(["fill"], edited=lambda r, n: asked.append(n) or False), 0)
                answer = json.loads(out.getvalue())
                if expected is not None:
                    self.assertEqual((answer, asked), (expected, []))
                else:
                    self.assertEqual((answer["clean"], asked), ([f"{REPO}/pull/1"], [1]))
            self.assertEqual(post_merge_edited.main(["bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
