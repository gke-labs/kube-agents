"""Tests for clean_merges: the install's clean-merge track record per kind of fix, on a fake clock."""

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import clean_merges

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def record(merged_at, kind="no-pdb", outcome="verified_fixed", human_edited=0, reverted_at=None):
    return {
        "kind": kind,
        "outcome": outcome,
        "merged_at": merged_at.isoformat() if isinstance(merged_at, datetime) else merged_at,
        "human_edited": human_edited,
        "reverted_at": reverted_at.isoformat() if isinstance(reverted_at, datetime) else reverted_at,
    }


def days_ago(days, hours=0):
    return NOW - timedelta(days=days, hours=hours)


class IsCleanTest(unittest.TestCase):
    def test_a_verified_unedited_unreverted_merge_is_clean(self):
        self.assertTrue(clean_merges.is_clean(record(days_ago(1)), NOW))

    def test_every_other_outcome_is_not_clean(self):
        for outcome in ("still_broken", "applied_not_rechecked", "not_applied", None):
            with self.subTest(outcome=outcome):
                self.assertFalse(clean_merges.is_clean(record(days_ago(1), outcome=outcome), NOW))

    def test_a_human_edit_or_an_unrecorded_one_is_not_clean(self):
        for edited in (1, None, "yes"):
            with self.subTest(human_edited=edited):
                self.assertFalse(clean_merges.is_clean(record(days_ago(1), human_edited=edited), NOW))

    def test_a_revert_inside_the_window_is_not_clean(self):
        merged = days_ago(10)
        self.assertFalse(clean_merges.is_clean(record(merged, reverted_at=merged + timedelta(days=7)), NOW))
        self.assertFalse(clean_merges.is_clean(record(merged, reverted_at=merged + timedelta(hours=1)), NOW))

    def test_a_revert_after_the_window_stays_clean(self):
        merged = days_ago(10)
        self.assertTrue(clean_merges.is_clean(record(merged, reverted_at=merged + timedelta(days=8)), NOW))

    def test_an_unreadable_revert_time_is_still_a_revert(self):
        self.assertFalse(clean_merges.is_clean(record(days_ago(10), reverted_at="soon"), NOW))

    def test_no_merge_time_or_one_after_now_is_not_clean(self):
        for merged in (None, "", "not a time", NOW + timedelta(minutes=1)):
            with self.subTest(merged_at=merged):
                self.assertFalse(clean_merges.is_clean(record(merged), NOW))

    def test_a_naive_or_z_suffixed_merge_time_reads_as_utc(self):
        self.assertTrue(clean_merges.is_clean(record("2026-11-19T12:00:00"), NOW))
        self.assertTrue(clean_merges.is_clean(record("2026-11-19T12:00:00Z"), NOW))


class CountTest(unittest.TestCase):
    def test_five_clean_merges_on_two_days_are_ready(self):
        rows = [record(days_ago(2, hours=h)) for h in range(3)] + [record(days_ago(1, hours=h)) for h in range(2)]
        self.assertEqual(clean_merges.count(rows, NOW)["no-pdb"], {"clean": 5, "days": 2, "settled": 0, "ready": True})

    def test_five_clean_merges_on_one_day_are_not_ready(self):
        rows = [record(datetime(2026, 11, 19, h, tzinfo=timezone.utc)) for h in range(1, 6)]
        self.assertEqual(clean_merges.count(rows, NOW)["no-pdb"], {"clean": 5, "days": 1, "settled": 0, "ready": False})

    def test_four_clean_merges_are_not_ready(self):
        rows = [record(days_ago(d)) for d in range(1, 5)]
        entry = clean_merges.count(rows, NOW)["no-pdb"]
        self.assertEqual((entry["clean"], entry["days"], entry["ready"]), (4, 4, False))

    def test_days_are_utc_calendar_days_not_24_hour_spans(self):
        rows = [record(datetime(2026, 11, 18, 23, 50, tzinfo=timezone.utc)), record(datetime(2026, 11, 19, 0, 10, tzinfo=timezone.utc))]
        self.assertEqual(clean_merges.count(rows, NOW)["no-pdb"]["days"], 2)

    def test_kinds_are_counted_apart(self):
        rows = [record(days_ago(1)), record(days_ago(2), kind="computeclass"), record(days_ago(3), kind="computeclass")]
        counts = clean_merges.count(rows, NOW)
        self.assertEqual(counts["no-pdb"]["clean"], 1)
        self.assertEqual(counts["computeclass"]["clean"], 2)

    def test_a_kind_with_only_unclean_records_reads_as_zero(self):
        counts = clean_merges.count([record(days_ago(1), outcome="still_broken")], NOW)
        self.assertEqual(counts, {"no-pdb": {"clean": 0, "days": 0, "settled": 0, "ready": False}})

    def test_a_record_with_no_kind_is_skipped(self):
        self.assertEqual(clean_merges.count([record(days_ago(1), kind=None), record(days_ago(1), kind="")], NOW), {})

    def test_settled_counts_merges_past_the_revert_window(self):
        rows = [record(days_ago(8)), record(days_ago(7, hours=1)), record(days_ago(6))]
        self.assertEqual(clean_merges.count(rows, NOW)["no-pdb"]["settled"], 2)

    def test_the_clock_moves_a_merge_into_settled_and_a_revert_takes_it_out(self):
        merged = days_ago(1)
        rows = [record(merged)]
        self.assertEqual(clean_merges.count(rows, NOW)["no-pdb"]["settled"], 0)
        later = NOW + timedelta(days=7)
        self.assertEqual(clean_merges.count(rows, later)["no-pdb"]["settled"], 1)
        rows = [record(merged, reverted_at=merged + timedelta(days=3))]
        self.assertEqual(clean_merges.count(rows, later)["no-pdb"]["clean"], 0)


class TrackRecordTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_no_table_yet_is_no_kinds(self):
        result = clean_merges.track_record(self.conn, NOW)
        self.assertEqual(
            result, {"kinds": {}, "clean_merges_needed": 5, "days_needed": 2, "revert_window_days": 7}
        )

    def test_reads_the_table_and_ignores_columns_it_does_not_count(self):
        self.conn.execute(
            "CREATE TABLE post_merge_records (url TEXT PRIMARY KEY, repo TEXT, number INTEGER, kind TEXT, "
            "outcome TEXT, outcome_at TEXT, merged_at TEXT, human_edited INTEGER, reverted_at TEXT)"
        )
        for n in range(5):
            self.conn.execute(
                "INSERT INTO post_merge_records VALUES (?, 'a/b', ?, 'no-pdb', 'verified_fixed', '', ?, 0, NULL)",
                (f"https://github.com/a/b/pull/{n}", n, days_ago(n % 2 + 1).isoformat()),
            )
        self.assertEqual(
            clean_merges.track_record(self.conn, NOW)["kinds"],
            {"no-pdb": {"clean": 5, "days": 2, "settled": 0, "ready": True}},
        )

    def test_a_table_missing_a_required_column_is_an_error_not_zero(self):
        self.conn.execute("CREATE TABLE post_merge_records (url TEXT PRIMARY KEY, outcome TEXT, outcome_at TEXT)")
        self.conn.execute("INSERT INTO post_merge_records VALUES ('u', 'verified_fixed', '')")
        with self.assertRaisesRegex(clean_merges.SchemaIncomplete, "kind, merged_at, human_edited, reverted_at"):
            clean_merges.track_record(self.conn, NOW)

    def test_reads_through_a_row_factory_connection(self):
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE post_merge_records (url TEXT PRIMARY KEY, kind TEXT, outcome TEXT, merged_at TEXT, "
            "human_edited INTEGER, reverted_at TEXT)"
        )
        self.conn.execute(
            "INSERT INTO post_merge_records VALUES ('u', 'no-pdb', 'verified_fixed', ?, 0, NULL)",
            (days_ago(1).isoformat(),),
        )
        self.assertEqual(clean_merges.track_record(self.conn, NOW)["kinds"]["no-pdb"]["clean"], 1)


if __name__ == "__main__":
    unittest.main()
