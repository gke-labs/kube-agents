"""Tests for clean_merges: the clean-merge track record per repository, kind of fix and environment, on a fake clock."""

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import clean_merges

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


REPO = "https://github.com/org/infra"
OTHER = "https://github.com/org/apps"
KEY = (clean_merges.repo_key(REPO), "no-pdb", "dev")


def record(merged_at, kind="no-pdb", outcome="verified_fixed", human_edited=0, reverted_at=None, repo=REPO,
           environment="dev"):
    return {
        "repo": repo,
        "kind": kind,
        "environment": environment,
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
        self.assertEqual(clean_merges.count(rows, NOW)[KEY], {"repo": REPO, "clean": 5, "days": 2, "settled": 0, "ready": True})

    def test_five_clean_merges_on_one_day_are_not_ready(self):
        rows = [record(datetime(2026, 11, 19, h, tzinfo=timezone.utc)) for h in range(1, 6)]
        self.assertEqual(clean_merges.count(rows, NOW)[KEY], {"repo": REPO, "clean": 5, "days": 1, "settled": 0, "ready": False})

    def test_four_clean_merges_are_not_ready(self):
        rows = [record(days_ago(d)) for d in range(1, 5)]
        entry = clean_merges.count(rows, NOW)[KEY]
        self.assertEqual((entry["clean"], entry["days"], entry["ready"]), (4, 4, False))

    def test_days_are_utc_calendar_days_not_24_hour_spans(self):
        rows = [record(datetime(2026, 11, 18, 23, 50, tzinfo=timezone.utc)), record(datetime(2026, 11, 19, 0, 10, tzinfo=timezone.utc))]
        self.assertEqual(clean_merges.count(rows, NOW)[KEY]["days"], 2)

    def test_kinds_are_counted_apart(self):
        rows = [record(days_ago(1)), record(days_ago(2), kind="computeclass"), record(days_ago(3), kind="computeclass")]
        counts = clean_merges.count(rows, NOW)
        self.assertEqual(counts[KEY]["clean"], 1)
        self.assertEqual(counts[(KEY[0], "computeclass", "dev")]["clean"], 2)

    def test_repositories_and_environments_are_counted_apart(self):
        rows = [record(days_ago(1)), record(days_ago(1), repo=OTHER), record(days_ago(2), repo=OTHER),
                record(days_ago(1), environment="prod")]
        counts = clean_merges.count(rows, NOW)
        self.assertEqual(counts[KEY]["clean"], 1)
        self.assertEqual(counts[(clean_merges.repo_key(OTHER), "no-pdb", "dev")]["clean"], 2)
        self.assertEqual(counts[(KEY[0], "no-pdb", "prod")]["clean"], 1)

    def test_one_repository_spelled_two_ways_is_one_key(self):
        rows = [record(days_ago(1)), record(days_ago(2), repo="git@github.com:Org/infra.git"),
                record(days_ago(3), repo=REPO + ".git/")]
        self.assertEqual(clean_merges.count(rows, NOW)[KEY]["clean"], 3)

    def test_a_record_with_no_repository_is_skipped_and_no_environment_counts_under_empty(self):
        counts = clean_merges.count([record(days_ago(1), repo=None), record(days_ago(1), environment=None)], NOW)
        self.assertEqual(list(counts), [(KEY[0], "no-pdb", "")])

    def test_a_kind_with_only_unclean_records_reads_as_zero(self):
        counts = clean_merges.count([record(days_ago(1), outcome="still_broken")], NOW)
        self.assertEqual(counts, {KEY: {"repo": REPO, "clean": 0, "days": 0, "settled": 0, "ready": False}})

    def test_a_record_with_no_kind_is_skipped(self):
        self.assertEqual(clean_merges.count([record(days_ago(1), kind=None), record(days_ago(1), kind="")], NOW), {})

    def test_settled_counts_merges_past_the_revert_window(self):
        rows = [record(days_ago(8)), record(days_ago(7, hours=1)), record(days_ago(6))]
        self.assertEqual(clean_merges.count(rows, NOW)[KEY]["settled"], 2)

    def test_the_clock_moves_a_merge_into_settled_and_a_revert_takes_it_out(self):
        merged = days_ago(1)
        rows = [record(merged)]
        self.assertEqual(clean_merges.count(rows, NOW)[KEY]["settled"], 0)
        later = NOW + timedelta(days=7)
        self.assertEqual(clean_merges.count(rows, later)[KEY]["settled"], 1)
        rows = [record(merged, reverted_at=merged + timedelta(days=3))]
        self.assertEqual(clean_merges.count(rows, later)[KEY]["clean"], 0)


class TrackRecordTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_no_table_yet_is_no_kinds(self):
        result = clean_merges.track_record(self.conn, NOW)
        self.assertEqual(
            result, {"counts": [], "clean_merges_needed": 5, "days_needed": 2, "revert_window_days": 7}
        )

    def test_reads_the_table_and_ignores_columns_it_does_not_count(self):
        self.conn.execute(
            "CREATE TABLE post_merge_records (url TEXT PRIMARY KEY, repo TEXT, number INTEGER, kind TEXT, "
            "environment TEXT, outcome TEXT, outcome_at TEXT, merged_at TEXT, human_edited INTEGER, reverted_at TEXT)"
        )
        for n in range(5):
            self.conn.execute(
                "INSERT INTO post_merge_records VALUES (?, 'https://github.com/a/b', ?, 'no-pdb', 'dev', "
                "'verified_fixed', '', ?, 0, NULL)",
                (f"https://github.com/a/b/pull/{n}", n, days_ago(n % 2 + 1).isoformat()),
            )
        self.assertEqual(
            clean_merges.track_record(self.conn, NOW)["counts"],
            [{"repo_key": "github.com/a/b", "kind": "no-pdb", "environment": "dev", "repo": "https://github.com/a/b",
              "clean": 5, "days": 2, "settled": 0, "ready": True}],
        )

    def test_a_table_missing_a_required_column_is_an_error_not_zero(self):
        self.conn.execute("CREATE TABLE post_merge_records (url TEXT PRIMARY KEY, outcome TEXT, outcome_at TEXT)")
        self.conn.execute("INSERT INTO post_merge_records VALUES ('u', 'verified_fixed', '')")
        with self.assertRaisesRegex(
            clean_merges.SchemaIncomplete, "repo, kind, environment, merged_at, human_edited, reverted_at"
        ):
            clean_merges.track_record(self.conn, NOW)

    def test_reads_through_a_row_factory_connection(self):
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE post_merge_records (url TEXT PRIMARY KEY, repo TEXT, kind TEXT, environment TEXT, "
            "outcome TEXT, merged_at TEXT, human_edited INTEGER, reverted_at TEXT)"
        )
        self.conn.execute(
            "INSERT INTO post_merge_records VALUES ('u', ?, 'no-pdb', 'dev', 'verified_fixed', ?, 0, NULL)",
            (REPO, days_ago(1).isoformat()),
        )
        self.assertEqual(clean_merges.track_record(self.conn, NOW)["counts"][0]["clean"], 1)


class TwoRepositoriesTest(unittest.TestCase):
    """Merges in one repository never make another ready."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "CREATE TABLE post_merge_records (url TEXT PRIMARY KEY, repo TEXT, kind TEXT, environment TEXT, "
            "outcome TEXT, merged_at TEXT, human_edited INTEGER, reverted_at TEXT)"
        )

    def _merge(self, repo, n, environment="dev"):
        self.conn.execute(
            "INSERT INTO post_merge_records VALUES (?, ?, 'no-pdb', ?, 'verified_fixed', ?, 0, NULL)",
            (f"{repo}/pull/{n}", repo, environment, days_ago(n % 2 + 1).isoformat()),
        )

    def test_merges_in_repo_a_never_make_repo_b_ready(self):
        for n in range(6):
            self._merge(REPO, n)
        self.assertTrue(clean_merges.ready_in(self.conn, repo=REPO, kind="no-pdb", environment="dev", now=NOW)["ready"])
        self.assertEqual(
            clean_merges.ready_in(self.conn, repo=OTHER, kind="no-pdb", environment="dev", now=NOW),
            {"clean": 0, "days": 0, "settled": 0, "ready": False},
        )
        for n in range(4):
            self._merge(OTHER, 100 + n)
        self.assertFalse(clean_merges.ready_in(self.conn, repo=OTHER, kind="no-pdb", environment="dev", now=NOW)["ready"])
        self.assertEqual(clean_merges.ready_in(self.conn, repo=OTHER, kind="no-pdb", environment="dev", now=NOW)["clean"], 4)

    def test_ready_in_with_no_table_or_an_unknown_kind_is_zero(self):
        self.conn.execute("DROP TABLE post_merge_records")
        self.assertFalse(clean_merges.ready_in(self.conn, repo=REPO, kind="no-pdb", environment="dev", now=NOW)["ready"])
        self.setUp()
        self._merge(REPO, 1)
        self.assertEqual(clean_merges.ready_in(self.conn, repo=REPO, kind="nope", environment="dev", now=NOW)["clean"], 0)

    def test_ready_in_matches_the_repository_however_it_is_spelled(self):
        for n in range(5):
            self._merge(REPO, n)
        self.assertTrue(clean_merges.ready_in(self.conn, repo="https://GitHub.com/org/infra.git", kind="no-pdb", environment="dev", now=NOW)["ready"])

    def test_ready_in_is_keyword_only(self):
        with self.assertRaises(TypeError):
            clean_merges.ready_in(self.conn, "no-pdb", "dev", NOW)

    def test_environments_compare_lower_case(self):
        for n in range(5):
            self._merge(REPO, n, environment=" Dev")
        self.assertTrue(clean_merges.ready_in(self.conn, repo=REPO, kind="no-pdb", environment="dev", now=NOW)["ready"])

    def test_ready_in_counts_only_the_environment_asked_for(self):
        for n in range(3):
            self._merge(REPO, n)
        for n in range(3, 6):
            self._merge(REPO, n, environment="prod")
        self.assertEqual(clean_merges.ready_in(self.conn, repo=REPO, kind="no-pdb", environment="dev", now=NOW)["clean"], 3)
        self.assertFalse(clean_merges.ready_in(self.conn, repo=REPO, kind="no-pdb", environment="prod", now=NOW)["ready"])

    def test_track_record_lists_each_repository_and_narrows_to_one(self):
        self._merge(REPO, 1)
        self._merge(OTHER, 2)
        self._merge(OTHER, 3)
        counts = clean_merges.track_record(self.conn, NOW)["counts"]
        self.assertEqual([(c["repo"], c["clean"]) for c in counts], [(OTHER, 2), (REPO, 1)])
        narrowed = clean_merges.track_record(self.conn, NOW, repo=REPO + ".git")["counts"]
        self.assertEqual([(c["repo"], c["clean"]) for c in narrowed], [(REPO, 1)])


if __name__ == "__main__":
    unittest.main()
