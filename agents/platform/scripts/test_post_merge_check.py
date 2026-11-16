"""Tests for post_merge_check.notice_merges: one forge read per waiting record, with a fake forge and clock."""

import re
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import post_merge_check

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
REPO = "https://github.com/acme/fleet"


def schema() -> str:
    """The table as session_kv_server.py creates it, read from its source so the two cannot drift."""
    source = (Path(__file__).resolve().parent / "session_kv_server.py").read_text(encoding="utf-8")
    return re.search(r"CREATE TABLE IF NOT EXISTS post_merge_records \((.*?)\)\s*\"\"\"", source, re.DOTALL).group(0)[:-3]


class FakeForge:
    """``view(repo, number)`` from a table of proposals; counts every read."""

    def __init__(self, proposals):
        self.proposals = proposals
        self.reads = []

    def __call__(self, repo, number):
        self.reads.append((repo, number))
        answer = self.proposals[number]
        if isinstance(answer, Exception):
            raise answer
        return answer


class NoticeMergesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(schema())

    def add(self, number, **columns):
        row = {"url": f"{REPO}/pull/{number}", "repo": REPO, "number": number, "kind": "no-pdb",
               "opened_at": (NOW - timedelta(hours=100 - number)).isoformat(), **columns}
        names = ", ".join(row)
        self.conn.execute(f"INSERT INTO post_merge_records ({names}) VALUES ({', '.join('?' * len(row))})",
                          tuple(row.values()))

    def row(self, number):
        self.conn.row_factory = sqlite3.Row
        found = dict(self.conn.execute("SELECT * FROM post_merge_records WHERE number = ?", (number,)).fetchone())
        self.conn.row_factory = None
        return found

    def test_a_merged_pull_request_records_its_merge_time_and_commit(self):
        self.add(1)
        forge = FakeForge({1: {"state": "merged", "mergeCommit": "abc123", "closed": "2026-11-12T08:30:00Z"}})
        seen = post_merge_check.notice_merges(self.conn, forge, NOW)
        self.assertEqual(seen["merged"], [f"{REPO}/pull/1"])
        row = self.row(1)
        self.assertEqual((row["merged_at"], row["merge_commit"], row["closed_at"], row["outcome"]),
                         ("2026-11-12T08:30:00Z", "abc123", None, None))
        self.assertEqual(forge.reads, [(REPO, 1)])

    def test_a_merge_with_no_commit_or_time_still_records_the_merge(self):
        self.add(1)
        post_merge_check.notice_merges(self.conn, FakeForge({1: {"state": "merged", "mergeCommit": None}}), NOW)
        row = self.row(1)
        self.assertEqual((row["merged_at"], row["merge_commit"]), (NOW.isoformat(), None))

    def test_closed_unmerged_ends_silently_after_the_grace(self):
        self.add(2)
        closed = {"state": "closed", "closed": "2026-11-12T07:00:00Z"}
        seen = post_merge_check.notice_merges(self.conn, FakeForge({2: closed}), NOW)
        self.assertEqual(seen["closed"], [f"{REPO}/pull/2"])
        row = self.row(2)
        self.assertEqual((row["closed_at"], row["merged_at"], row["outcome"]), ("2026-11-12T07:00:00Z", None, None))
        # Within the grace it is read again; past it, never.
        forge = FakeForge({2: closed})
        post_merge_check.notice_merges(self.conn, forge, NOW + timedelta(hours=12))
        self.assertEqual(forge.reads, [(REPO, 2)])
        forge.reads.clear()
        post_merge_check.notice_merges(self.conn, forge, NOW + timedelta(days=2))
        self.assertEqual(forge.reads, [])
        self.assertIsNone(self.row(2)["outcome"])

    def test_a_reopen_inside_the_grace_waits_again_and_a_later_merge_is_noticed(self):
        self.add(3)
        post_merge_check.notice_merges(self.conn, FakeForge({3: {"state": "closed", "closed": NOW.isoformat()}}), NOW)
        post_merge_check.notice_merges(self.conn, FakeForge({3: {"state": "open"}}), NOW + timedelta(hours=1))
        self.assertIsNone(self.row(3)["closed_at"])
        later = NOW + timedelta(days=3)
        post_merge_check.notice_merges(self.conn, FakeForge({3: {"state": "merged", "mergeCommit": "c3"}}), later)
        self.assertEqual(self.row(3)["merge_commit"], "c3")

    def test_an_open_pull_request_is_only_stamped_as_read(self):
        self.add(3)
        seen = post_merge_check.notice_merges(self.conn, FakeForge({3: {"state": "open"}}), NOW)
        self.assertEqual(seen["open"], [f"{REPO}/pull/3"])
        row = self.row(3)
        self.assertEqual((row["merged_at"], row["closed_at"], row["merge_checked_at"]), (None, None, NOW.isoformat()))

    def test_a_failed_read_is_logged_and_the_rest_still_run(self):
        self.add(4)
        self.add(5)
        forge = FakeForge({4: RuntimeError("broker down"), 5: {"state": "merged", "mergeCommit": "def"}})
        with self.assertLogs("post_merge_check", level="WARNING"):
            seen = post_merge_check.notice_merges(self.conn, forge, NOW)
        self.assertEqual((seen["failed"], seen["merged"]), ([f"{REPO}/pull/4"], [f"{REPO}/pull/5"]))
        self.assertIsNone(self.row(4)["merged_at"])
        self.assertEqual(self.row(4)["merge_checked_at"], NOW.isoformat(), "a failed read still rotates the queue")

    def test_a_database_error_on_one_record_does_not_stop_the_tick(self):
        self.add(10)
        self.add(11)
        real = self.conn

        class Flaky:
            calls = 0

            def execute(self, sql, params=()):
                if sql.startswith("UPDATE") and params[-1].endswith("/10"):
                    raise sqlite3.OperationalError("database is locked")
                return real.execute(sql, params)

            def __enter__(self):
                return real.__enter__()

            def __exit__(self, *exc):
                return real.__exit__(*exc)

        forge = FakeForge({10: {"state": "open"}, 11: {"state": "open"}})
        with self.assertLogs("post_merge_check", level="WARNING"):
            seen = post_merge_check.notice_merges(Flaky(), forge, NOW)
        self.assertEqual((seen["failed"], seen["open"]), ([f"{REPO}/pull/10"], [f"{REPO}/pull/11"]))

    def test_only_waiting_records_are_read(self):
        self.add(6, merged_at="2026-11-11T00:00:00Z")
        self.add(7, closed_at="2026-11-10T00:00:00Z")
        self.add(8, outcome="not_applied")
        self.add(9)
        forge = FakeForge({9: {"state": "open"}})
        post_merge_check.notice_merges(self.conn, forge, NOW)
        self.assertEqual(forge.reads, [(REPO, 9)])

    def test_at_most_twenty_reads_a_tick_and_every_record_gets_its_turn(self):
        for number in range(1, 31):
            self.add(number)
        forge = FakeForge({n: {"state": "open"} for n in range(1, 31)})
        post_merge_check.notice_merges(self.conn, forge, NOW)
        self.assertEqual(len(forge.reads), post_merge_check.READS_PER_TICK)
        first = {n for _, n in forge.reads}
        forge.reads.clear()
        post_merge_check.notice_merges(self.conn, forge, NOW + timedelta(minutes=5))
        second = {n for _, n in forge.reads}
        self.assertEqual(len(second), 20)
        self.assertTrue(set(range(1, 31)) - first <= second, "the ten not read last tick are read this one")

    def test_forge_view_reads_the_proposal_through_forge_call(self):
        import types
        from unittest import mock

        calls = []
        fake = types.ModuleType("forge")
        fake.call = lambda verb, payload, repo, **kw: calls.append((verb, payload, repo, kw)) or {
            "proposal": {"state": "merged"}
        }
        with mock.patch.dict(sys.modules, {"forge": fake}):
            self.assertEqual(post_merge_check.forge_view(REPO, 7), {"state": "merged"})
        self.assertEqual(calls[0][:3], ("proposal-view", {"number": 7}, REPO))
        self.assertTrue(calls[0][3]["retry_transient"])
        fake.call = lambda *a, **kw: {"forge": "github"}
        with mock.patch.dict(sys.modules, {"forge": fake}), self.assertRaises(TypeError):
            post_merge_check.forge_view(REPO, 7)


class MainTest(unittest.TestCase):
    def test_off_reads_nothing_and_on_runs_one_tick(self):
        import io
        import json
        import tempfile
        from contextlib import redirect_stdout
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kv.db")
            with sqlite3.connect(db) as conn:
                conn.execute(schema())
                conn.execute(
                    "INSERT INTO post_merge_records (url, repo, number, kind, opened_at) VALUES (?, ?, 1, 'no-pdb', ?)",
                    (f"{REPO}/pull/1", REPO, NOW.isoformat()),
                )
            forge = FakeForge({1: {"state": "merged", "mergeCommit": "abc"}})
            import kubeagents_switches

            for value, expected in (("off", {"switch": "off"}), ("silent", None)):
                out = io.StringIO()
                with mock.patch.dict("os.environ", {"KUBEAGENTS_POST_MERGE_CHECK": value, "SESSION_KV_DB_PATH": db}), \
                        mock.patch.object(kubeagents_switches, "MANAGED_DOTENV_PATH", Path(tmp) / "no-env"), \
                        mock.patch.object(kubeagents_switches, "SWITCH_DIR", Path(tmp) / "no-dir"), \
                        redirect_stdout(out):
                    self.assertEqual(post_merge_check.main(["notice"], view=forge), 0)
                answer = json.loads(out.getvalue())
                if expected is not None:
                    self.assertEqual(answer, expected)
                    self.assertEqual(forge.reads, [])
                else:
                    self.assertEqual(answer["merged"], [f"{REPO}/pull/1"])
        self.assertEqual(post_merge_check.main(["other"]), 2)

if __name__ == "__main__":
    unittest.main()
