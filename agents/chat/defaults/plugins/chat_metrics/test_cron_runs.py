"""Tests for the cron run and skip counters the chat metrics listener reads off the execution ledgers.

Run: python3 -m unittest discover -s agents/chat/defaults/plugins/chat_metrics -p 'test_cron_runs.py'
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_metrics import cron_runs as cron  # noqa: E402
from chat_metrics import metrics  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[5]
SKIP_LEDGER_PATCH = REPO_ROOT / "deploy" / "docker" / "patches" / "cron_skip_ledger.py"

_RUN_LINE = re.compile(r'^kubeagents_cron_runs_total\{cron_job="([^"]*)",outcome="([^"]*)"\} (\d+)$', re.M)
_SKIP_LINE = re.compile(r'^kubeagents_cron_skips_total\{cron_job="([^"]*)",reason="([^"]*)"\} (\d+)$', re.M)

# The executions table as upstream cron/executions.py creates it at v2026.9.14,
# with the skip_reason column this repo's cron_skip_ledger patch adds.
SCHEMA = """
CREATE TABLE executions (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL, process_id TEXT NOT NULL,
    pid INTEGER NOT NULL, process_started_at INTEGER,
    status TEXT NOT NULL CHECK(status IN ('claimed','running','completed','failed','unknown','skipped')),
    handoff_pending INTEGER NOT NULL DEFAULT 0, handoff_started_at REAL,
    claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT, skip_reason TEXT,
    delivery_outcome TEXT, scheduled_instant TEXT)
"""


def _at(minute: int) -> str:
    """A claimed_at in the form Hermes writes, ``minute`` minutes into one day."""
    return f"2026-11-24T{minute // 60:02d}:{minute % 60:02d}:00.000000+00:00"


class _VolumeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.counter = cron.CronRunsCounter(root=lambda: self.root)
        self._next_id = 0

    def ledger(self, profile=None):
        home = self.root / "profiles" / profile if profile else self.root
        path = cron.ledger_path(home)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(path) as conn:
                conn.execute(SCHEMA)
        return path

    def add(self, job, status, minute, profile=None, reason=None, execution_id=None):
        """Insert one row; the id it was given."""
        self._next_id += 1
        execution_id = execution_id or f"e{self._next_id:05d}"
        with sqlite3.connect(self.ledger(profile)) as conn:
            conn.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at, skip_reason) "
                "VALUES (?, ?, 'builtin', 'p', 1, ?, ?, ?)",
                (execution_id, job, status, _at(minute), reason),
            )
        return execution_id

    def finish(self, execution_id, status, profile=None):
        with sqlite3.connect(self.ledger(profile)) as conn:
            conn.execute("UPDATE executions SET status = ? WHERE id = ?", (status, execution_id))

    @staticmethod
    def counted(series):
        """The series that have counted something: a series is served from zero, which most tests do not care about."""
        return {key: value for key, value in series.items() if value}

    def runs(self):
        return self.counted(self.counter.counts()[0])

    def skips(self):
        return self.counted(self.counter.counts()[1])

    def both(self, counter=None):
        runs, skips = (counter or self.counter).counts()
        return self.counted(runs), self.counted(skips)


class LabelTest(unittest.TestCase):
    def test_a_job_id_is_its_label_only_when_it_is_shaped_like_one(self):
        for job in ("compliance-audit", "profile-cron-tick", "oobe", "a1", "sa_role_watch", "0b3f9c2a71de"):
            with self.subTest(job=job):
                self.assertEqual(job, cron.job_label(job))
        for job in ("", "Compliance Audit", "a--b", "-a", "a-", "job\n", 'x"y', "x" * 65, None, 7, b"job"):
            with self.subTest(job=job):
                self.assertEqual("other", cron.job_label(job))

    def test_a_skip_with_no_reason_is_unspecified_and_an_odd_one_is_other(self):
        self.assertEqual("unspecified", cron.reason_label(None))
        self.assertEqual("unspecified", cron.reason_label(""))
        self.assertEqual("missed_window", cron.reason_label("missed_window"))
        for reason in ("Missed Window", "missed-window", "a__b", "x" * 49, 7, "reason: the job was busy"):
            with self.subTest(reason=reason):
                self.assertEqual("other", cron.reason_label(reason))

    @unittest.skipUnless(SKIP_LEDGER_PATCH.is_file(), "the skip ledger patch is not in this checkout")
    def test_every_reason_the_skip_ledger_records_is_served_under_its_own_name(self):
        spec = importlib.util.spec_from_file_location("cron_skip_ledger_under_test", SKIP_LEDGER_PATCH)
        patch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(patch)
        self.assertGreater(len(patch.SKIP_REASONS), 0)
        self.assertLessEqual(len(patch.SKIP_REASONS) + 1, cron.MAX_REASON_SERIES)
        for reason in sorted(patch.SKIP_REASONS) + [patch.SKIP_REASON_UNSPECIFIED]:
            with self.subTest(reason=reason):
                self.assertEqual(reason, cron.reason_label(reason))
        self.assertEqual(patch.SKIP_REASON_UNSPECIFIED, cron.REASON_UNSPECIFIED)

    def test_each_run_outcome_is_one_of_the_schedule_counters_words(self):
        """The operator's AgentSchedule outcomes, read from its source: the cron family uses a subset of them."""
        source = (REPO_ROOT / "k8s-operator" / "api" / "v1alpha1" / "agentschedule_types.go").read_text(encoding="utf-8")
        operator = {word.lower() for word in re.findall(r'AgentScheduleOutcome\w+\s*=\s*"(\w+)"', source)}
        self.assertGreaterEqual(len(operator), 3, operator)
        self.assertEqual({"completed", "failed", "unknown"}, set(cron.RUN_OUTCOMES))
        self.assertLessEqual(set(cron.RUN_OUTCOMES.values()), operator)
        self.assertEqual(len(cron.RUN_OUTCOMES), len(set(cron.RUN_OUTCOMES.values())))


class CounterTest(_VolumeTest):
    def test_rows_terminal_at_the_first_read_are_the_baseline(self):
        self.add("compliance-audit", "completed", 1, profile="platform")
        self.add("compliance-audit", "failed", 2, profile="platform")
        self.add("profile-cron-tick", "skipped", 3, reason="missed_window")
        self.assertEqual(({}, {}), self.both())
        self.assertEqual(({}, {}), self.both())

    def test_runs_that_end_after_the_baseline_count_by_job_and_outcome(self):
        self.add("compliance-audit", "completed", 1, profile="platform")
        self.counter.counts()
        self.add("compliance-audit", "completed", 10, profile="platform")
        self.add("compliance-audit", "failed", 11, profile="platform")
        self.add("stall-watch", "unknown", 12, profile="platform")
        self.add("profile-cron-tick", "completed", 13)
        self.assertEqual(
            {
                ("compliance-audit", "succeeded"): 1,
                ("compliance-audit", "failed"): 1,
                ("stall-watch", "lost"): 1,
                ("profile-cron-tick", "succeeded"): 1,
            },
            self.runs(),
        )
        self.assertEqual({}, self.skips())

    def test_skipped_slots_count_by_job_and_reason(self):
        self.counter.counts()
        self.add("stall-watch", "skipped", 10, profile="platform", reason="already_running")
        self.add("stall-watch", "skipped", 11, profile="platform", reason="already_running")
        self.add("stall-watch", "skipped", 12, profile="platform", reason="missed_window")
        self.add("stall-watch", "skipped", 13, profile="platform")
        self.assertEqual(
            {("stall-watch", "already_running"): 2, ("stall-watch", "missed_window"): 1, ("stall-watch", "unspecified"): 1},
            self.skips(),
        )
        self.assertEqual({}, self.runs())

    def test_a_second_read_adds_only_what_ended_since(self):
        self.counter.counts()
        self.add("stall-watch", "completed", 10, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())
        self.add("stall-watch", "completed", 11, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 2}, self.runs())
        self.assertEqual({("stall-watch", "succeeded"): 2}, self.runs())

    def test_a_run_in_flight_counts_once_when_it_ends(self):
        self.counter.counts()
        long_run = self.add("compliance-audit", "claimed", 10, profile="platform")
        self.add("stall-watch", "completed", 11, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())
        self.finish(long_run, "running", profile="platform")
        self.add("stall-watch", "completed", 12, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 2}, self.runs())
        self.finish(long_run, "completed", profile="platform")
        expected = {("stall-watch", "succeeded"): 2, ("compliance-audit", "succeeded"): 1}
        self.assertEqual(expected, self.runs())
        self.assertEqual(expected, self.runs())

    def test_a_run_in_flight_at_the_baseline_counts_when_it_ends(self):
        self.add("stall-watch", "completed", 1, profile="platform")
        in_flight = self.add("compliance-audit", "running", 2, profile="platform")
        self.add("stall-watch", "completed", 3, profile="platform")
        self.assertEqual({}, self.runs())
        self.finish(in_flight, "failed", profile="platform")
        self.assertEqual({("compliance-audit", "failed"): 1}, self.runs())
        self.assertEqual({("compliance-audit", "failed"): 1}, self.runs())

    def test_rows_hermes_prunes_do_not_change_the_count(self):
        self.counter.counts()
        first = self.add("stall-watch", "completed", 10, profile="platform")
        self.add("stall-watch", "completed", 11, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 2}, self.runs())
        with sqlite3.connect(self.ledger("platform")) as conn:
            conn.execute("DELETE FROM executions WHERE id = ?", (first,))
        self.add("stall-watch", "completed", 12, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 3}, self.runs())

    def test_two_rows_claimed_at_the_same_instant_each_count_once(self):
        self.counter.counts()
        self.add("stall-watch", "completed", 10, profile="platform")
        self.add("sa-role-watch", "completed", 10, profile="platform")
        expected = {("stall-watch", "succeeded"): 1, ("sa-role-watch", "succeeded"): 1}
        self.assertEqual(expected, self.runs())
        self.add("leeway-watch", "completed", 10, profile="platform")
        expected[("leeway-watch", "succeeded")] = 1
        self.assertEqual(expected, self.runs())
        self.assertEqual(expected, self.runs())

    def test_a_ledger_that_appears_after_the_baseline_counts_every_row(self):
        self.counter.counts()
        self.add("stall-watch", "completed", 10, profile="cluster-new")
        self.add("stall-watch", "skipped", 11, profile="cluster-new", reason="missed_window")
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())
        self.assertEqual({("stall-watch", "missed_window"): 1}, self.skips())

    def test_a_volume_with_no_ledger_reads_as_nothing(self):
        with self.assertNoLogs(cron.logger, level="WARNING"):
            self.assertEqual(({}, {}), self.both())
        (self.root / "profiles" / "platform").mkdir(parents=True)
        with self.assertNoLogs(cron.logger, level="WARNING"):
            self.assertEqual(({}, {}), self.both())

    def test_the_same_job_in_two_profiles_adds_up(self):
        self.counter.counts()
        self.add("stall-watch", "completed", 10, profile="platform")
        self.add("stall-watch", "completed", 10)
        self.assertEqual({("stall-watch", "succeeded"): 2}, self.runs())


class FailureTest(_VolumeTest):
    def test_a_ledger_that_is_not_a_database_is_logged_once_and_does_not_stop_the_others(self):
        self.counter.counts()
        broken = cron.ledger_path(self.root / "profiles" / "broken")
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"this is not a sqlite file" * 64)
        self.add("stall-watch", "completed", 10, profile="platform")
        with self.assertLogs(cron.logger, level="WARNING") as logged:
            self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())
        self.assertEqual(1, len(logged.records))
        self.assertIn("DatabaseError", logged.output[0])
        with self.assertNoLogs(cron.logger, level="WARNING"):
            self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())

    def test_a_ledger_with_another_table_shape_does_not_raise(self):
        self.counter.counts()
        other = cron.ledger_path(self.root / "profiles" / "other")
        other.parent.mkdir(parents=True)
        with sqlite3.connect(other) as conn:
            conn.execute("CREATE TABLE executions (id TEXT)")
        with self.assertLogs(cron.logger, level="WARNING"):
            self.assertEqual(({}, {}), self.both())

    def test_no_failure_of_a_read_reaches_the_caller(self):
        self.counter.counts()
        self.add("stall-watch", "completed", 10, profile="platform")
        for error in (RuntimeError("bug"), sqlite3.OperationalError("locked"), MemoryError(), OSError("EIO")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(self.counter, "_read_ledger", side_effect=error):
                    self.assertEqual(({}, {}), self.both())
                with mock.patch.object(cron, "ledger_files", side_effect=error):
                    self.assertEqual(({}, {}), self.both())
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())

    def test_a_locked_ledger_keeps_the_last_counts_and_is_read_when_it_frees(self):
        self.counter.counts()
        self.add("stall-watch", "completed", 10, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())
        self.add("stall-watch", "completed", 11, profile="platform")
        with mock.patch.object(self.counter, "_rows", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertLogs(cron.logger, level="WARNING"):
                self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())
        self.assertEqual({("stall-watch", "succeeded"): 2}, self.runs())

    def test_a_ledger_unreadable_at_the_baseline_does_not_count_its_history_later(self):
        self.add("stall-watch", "completed", 1, profile="platform")
        with mock.patch.object(self.counter, "_rows", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertLogs(cron.logger, level="WARNING"):
                self.assertEqual(({}, {}), self.both())
        self.assertEqual(({}, {}), self.both())
        self.add("stall-watch", "completed", 10, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())

    def test_a_listing_that_fails_at_the_baseline_does_not_count_history_later(self):
        self.add("stall-watch", "completed", 1, profile="platform")
        self.add("profile-cron-tick", "completed", 1)
        with mock.patch.object(cron, "profile_homes", side_effect=OSError("EIO")):
            with self.assertLogs(cron.logger, level="WARNING"):
                self.assertEqual(({}, {}), self.both())
        self.assertEqual(({}, {}), self.both())
        self.add("stall-watch", "completed", 10, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())

    def test_the_ledgers_already_followed_are_still_read_while_the_listing_fails(self):
        self.ledger("platform")
        self.counter.counts()
        self.add("stall-watch", "completed", 10, profile="platform")
        with mock.patch.object(cron, "profile_homes", side_effect=OSError("EIO")):
            with self.assertLogs(cron.logger, level="WARNING"):
                self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())

    def test_a_row_with_values_hermes_does_not_write_is_bounded_not_trusted(self):
        self.counter.counts()
        with sqlite3.connect(self.ledger("platform")) as conn:
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at) VALUES ('odd', 'stall-watch', 'x', 'p', 1, 'exploded', ?)",
                (_at(10),),
            )
        self.add("Not A Job", "completed", 11, profile="platform")
        self.add(b"blob-job", "completed", 12, profile="platform")
        self.add("stall-watch", "skipped", 13, profile="platform", reason="because: reasons")
        self.add("stall-watch", "skipped", 14, profile="platform", reason=7)
        self.assertEqual({("other", "succeeded"): 2}, self.runs())
        self.assertEqual({("stall-watch", "other"): 2}, self.skips())
        self.assertEqual({("other", "succeeded"): 2}, self.runs())


class ZeroTest(_VolumeTest):
    """A series is served from zero, so a rate sees its first count."""

    def test_a_job_met_at_the_baseline_has_its_three_outcomes_at_zero(self):
        self.add("compliance-audit", "completed", 1, profile="platform")
        self.add("stall-watch", "running", 2, profile="platform")
        runs, skips = self.counter.counts()
        self.assertEqual(
            {(job, outcome): 0 for job in ("compliance-audit", "stall-watch") for outcome in ("succeeded", "failed", "lost")},
            runs,
        )
        self.assertEqual({}, skips)

    def test_the_first_failure_after_the_baseline_moves_a_series_that_was_at_zero(self):
        self.add("compliance-audit", "completed", 1, profile="platform")
        before = self.counter.counts()[0]
        self.assertEqual(0, before[("compliance-audit", "failed")])
        self.add("compliance-audit", "failed", 10, profile="platform")
        self.assertEqual(1, self.counter.counts()[0][("compliance-audit", "failed")])

    def test_a_skip_met_at_the_baseline_has_its_series_at_zero(self):
        self.add("stall-watch", "skipped", 1, profile="platform", reason="missed_window")
        runs, skips = self.counter.counts()
        self.assertEqual({("stall-watch", "missed_window"): 0}, skips)
        self.assertEqual(0, runs[("stall-watch", "failed")])
        self.add("stall-watch", "skipped", 10, profile="platform", reason="missed_window")
        self.assertEqual({("stall-watch", "missed_window"): 1}, self.counter.counts()[1])

    def test_a_job_first_met_after_the_baseline_starts_from_zero_too(self):
        self.counter.counts()
        self.add("leeway-watch", "claimed", 10, profile="platform")
        runs = self.counter.counts()[0]
        self.assertEqual({("leeway-watch", "succeeded"): 0, ("leeway-watch", "failed"): 0, ("leeway-watch", "lost"): 0}, runs)


class ValueBoundTest(_VolumeTest):
    def test_an_oversized_value_is_not_loaded_whole(self):
        self.counter.counts()
        huge = "j" * 2_000_000
        self.add(huge, "completed", 10, profile="platform")
        self.add("stall-watch", "skipped", 11, profile="platform", reason="r" * 2_000_000)
        rows = self.counter._rows(self.ledger("platform"), 0)[1]
        self.assertLessEqual(max(len(str(value)) for row in rows for value in row), cron.JOB_ID_MAX_LENGTH + 1)
        self.assertEqual({("other", "succeeded"): 1}, self.runs())
        self.assertEqual({("stall-watch", "other"): 1}, self.skips())

    def test_a_value_one_character_too_long_still_reads_as_too_long(self):
        self.counter.counts()
        self.add("j" * (cron.JOB_ID_MAX_LENGTH + 1), "completed", 10)
        self.add("j" * cron.JOB_ID_MAX_LENGTH, "completed", 11)
        self.assertEqual({("other", "succeeded"): 1, ("j" * cron.JOB_ID_MAX_LENGTH, "succeeded"): 1}, self.runs())


class RemovedProfileTest(_VolumeTest):
    def test_a_profile_removed_and_made_again_counts_its_new_runs(self):
        self.counter.counts()
        for minute in (10, 11, 12):
            self.add("stall-watch", "completed", minute, profile="cluster-a")
        self.assertEqual({("stall-watch", "succeeded"): 3}, self.runs())
        shutil.rmtree(self.root / "profiles" / "cluster-a")
        self.assertEqual({("stall-watch", "succeeded"): 3}, self.runs())
        for minute in (20, 21, 22):
            self.add("sa-role-watch", "completed", minute, profile="cluster-a")
        self.assertEqual({("stall-watch", "succeeded"): 3, ("sa-role-watch", "succeeded"): 3}, self.runs())

    def remake(self, profile, runs):
        """Remove ``profile`` and make it again with ``runs`` ended runs, with no read in between.

        The new ledger is written while the old one still exists, so the file
        system cannot give it the old file's inode.
        """
        old = self.ledger(profile)
        staged = self.root / "staged-executions.db"
        with sqlite3.connect(staged) as conn:
            conn.execute(SCHEMA)
            for index in range(runs):
                conn.execute(
                    "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at) VALUES (?, 'sa-role-watch', 'builtin', 'p', 1, 'completed', ?)",
                    (f"new{index:04d}", _at(30 + index)),
                )
        self.assertNotEqual(old.stat().st_ino, staged.stat().st_ino)
        shutil.rmtree(self.root / "profiles" / profile)
        old.parent.mkdir(parents=True)
        staged.rename(old)

    def test_a_profile_remade_between_two_reads_counts_every_new_run(self):
        """Whatever the new ledger's size against the old position: fewer rows, one under it, more."""
        for new_runs in (1, 4, 5, 9):
            with self.subTest(new_runs=new_runs):
                profile = f"cluster-{new_runs}"
                counter = cron.CronRunsCounter(root=lambda: self.root)
                counter.counts()
                for minute in range(10, 15):
                    self.add("stall-watch", "completed", minute, profile=profile)
                self.assertEqual({("stall-watch", "succeeded"): 5}, self.counted(counter.counts()[0]))
                self.assertEqual(6, counter._ledgers[self.ledger(profile)].mark)
                self.remake(profile, new_runs)
                expected = {("stall-watch", "succeeded"): 5, ("sa-role-watch", "succeeded"): new_runs}
                self.assertEqual(expected, self.counted(counter.counts()[0]))
                self.assertEqual(expected, self.counted(counter.counts()[0]))
                shutil.rmtree(self.root / "profiles" / profile)

    def test_a_ledger_missing_while_its_profile_stays_keeps_its_place(self):
        self.counter.counts()
        self.add("stall-watch", "completed", 10, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())
        with mock.patch.object(cron.os, "stat", side_effect=FileNotFoundError):
            self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())


class OrderTest(_VolumeTest):
    """The ledger is followed in the order rows were inserted, whatever their timestamps say."""

    def add_at(self, job, status, claimed_at, profile="platform"):
        self._next_id += 1
        with sqlite3.connect(self.ledger(profile)) as conn:
            conn.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at) VALUES (?, ?, 'builtin', 'p', 1, ?, ?)",
                (f"t{self._next_id:05d}", job, status, claimed_at),
            )

    def test_the_repeated_hour_of_a_clock_change_loses_no_run(self):
        self.add_at("stall-watch", "completed", "2026-11-01T01:30:00-07:00")
        self.counter.counts()
        # Forty minutes later on the clock that fell back: its text sorts first.
        self.add_at("stall-watch", "completed", "2026-11-01T01:10:00-08:00")
        self.add_at("stall-watch", "completed", "2026-11-01T01:50:00-08:00")
        self.assertEqual({("stall-watch", "succeeded"): 2}, self.runs())

    def test_a_change_of_time_zone_does_not_stall_the_count(self):
        self.add_at("stall-watch", "completed", "2026-11-24T16:00:00+00:00")
        self.counter.counts()
        self.add_at("stall-watch", "completed", "2026-11-24T08:01:00-08:00")
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())

    def test_a_row_inserted_later_with_an_earlier_stamp_is_counted(self):
        self.counter.counts()
        self.add("stall-watch", "completed", 20, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.runs())
        late = self.add("compliance-audit", "claimed", 19, profile="platform")
        self.counter.counts()
        self.finish(late, "completed", profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 1, ("compliance-audit", "succeeded"): 1}, self.runs())

    def test_a_row_stamped_far_in_the_future_does_not_freeze_the_ledger(self):
        self.counter.counts()
        self.add_at("stall-watch", "completed", "9999")
        self.add("stall-watch", "completed", 10, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 2}, self.runs())
        self.add("stall-watch", "completed", 11, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 3}, self.runs())

    def test_more_rows_at_one_instant_than_a_read_takes_are_all_passed(self):
        counter = cron.CronRunsCounter(root=lambda: self.root, max_rows=3)
        counter.counts()
        for _ in range(7):
            self.add("stall-watch", "completed", 10, profile="platform")
        self.add("sa-role-watch", "completed", 11, profile="platform")
        for _ in range(6):
            counter.counts()
        self.assertEqual({("stall-watch", "succeeded"): 7, ("sa-role-watch", "succeeded"): 1}, self.counted(counter.counts()[0]))

    def test_a_rebuilt_table_takes_a_new_baseline_and_counts_what_follows(self):
        self.counter.counts()
        for minute in range(10, 16):
            self.add("stall-watch", "completed", minute, profile="platform")
        self.assertEqual({("stall-watch", "succeeded"): 6}, self.runs())
        # A table emptied and filled again numbers its rows from 1, as one rebuilt does.
        with sqlite3.connect(self.ledger("platform")) as conn:
            conn.execute("DELETE FROM executions")
        self.add("stall-watch", "completed", 16, profile="platform")
        self.add("stall-watch", "completed", 17, profile="platform")
        with sqlite3.connect(self.ledger("platform")) as conn:
            self.assertEqual(2, conn.execute("SELECT max(rowid) FROM executions").fetchone()[0])
        self.assertEqual({("stall-watch", "succeeded"): 6}, self.runs())
        self.add("stall-watch", "failed", 20, profile="platform")
        expected = {("stall-watch", "succeeded"): 6, ("stall-watch", "failed"): 1}
        self.assertEqual(expected, self.runs())
        self.assertEqual(expected, self.runs())


class RosterTest(unittest.TestCase):
    """The shipped rosters' job ids are served under their own names."""

    ROSTERS = (
        REPO_ROOT / "agents" / "platform" / "cron" / "jobs.json",
        REPO_ROOT / "agents" / "chat" / "defaults" / "cron" / "jobs.json",
    )

    def test_every_shipped_job_id_is_its_own_label_and_fits_under_the_cap(self):
        ids = []
        for roster in self.ROSTERS:
            with self.subTest(roster=roster.name):
                self.assertTrue(roster.is_file(), roster)
                document = json.loads(roster.read_text(encoding="utf-8"))
                jobs = document if isinstance(document, list) else document["jobs"]
                self.assertGreater(len(jobs), 0)
                ids += [job["id"] for job in jobs]
        for job_id in ids:
            with self.subTest(job=job_id):
                self.assertEqual(job_id, cron.job_label(job_id))
        self.assertLess(len(set(ids)), cron.MAX_JOB_SERIES)


class BoundTest(_VolumeTest):
    def test_jobs_past_the_series_cap_count_as_other(self):
        counter = cron.CronRunsCounter(root=lambda: self.root, max_jobs=2)
        counter.counts()
        for minute, job in enumerate(("one", "two", "three", "one", "four"), start=10):
            self.add(job, "completed", minute)
        self.add("five", "skipped", 20, reason="missed_window")
        runs, skips = self.both(counter)
        self.assertEqual({("one", "succeeded"): 2, ("two", "succeeded"): 1, ("other", "succeeded"): 2}, runs)
        self.assertEqual({("other", "missed_window"): 1}, skips)

    def test_reasons_past_the_series_cap_count_as_other(self):
        counter = cron.CronRunsCounter(root=lambda: self.root, max_reasons=1)
        counter.counts()
        self.add("stall-watch", "skipped", 10, reason="missed_window")
        self.add("stall-watch", "skipped", 11, reason="already_running")
        self.add("stall-watch", "skipped", 12, reason="missed_window")
        self.assertEqual({("stall-watch", "missed_window"): 2, ("stall-watch", "other"): 1}, self.counted(counter.counts()[1]))

    def test_a_read_cut_at_the_row_limit_carries_on_at_the_next_read(self):
        counter = cron.CronRunsCounter(root=lambda: self.root, max_rows=3)
        counter.counts()
        for minute in range(10, 18):
            self.add("stall-watch", "completed", minute)
        seen = [sum(counter.counts()[0].values()) for _ in range(6)]  # zeros add nothing
        self.assertEqual(8, seen[-1])
        self.assertEqual(sorted(seen), seen)
        self.assertLess(seen[0], 8)

    def test_a_baseline_longer_than_the_row_limit_still_counts_no_history(self):
        for minute in range(1, 9):
            self.add("stall-watch", "completed", minute)
        counter = cron.CronRunsCounter(root=lambda: self.root, max_rows=3)
        for _ in range(6):
            self.assertEqual(({}, {}), self.both(counter))
        self.add("stall-watch", "completed", 20)
        self.assertEqual({("stall-watch", "succeeded"): 1}, self.counted(counter.counts()[0]))

    def test_the_skip_ledger_patch_is_where_the_comments_say(self):
        self.assertTrue(SKIP_LEDGER_PATCH.is_file(), SKIP_LEDGER_PATCH)

    def test_a_baseline_of_exactly_the_row_limit_counts_the_first_run_after_it(self):
        for minute in range(1, 4):
            self.add("stall-watch", "completed", minute)
        counter = cron.CronRunsCounter(root=lambda: self.root, max_rows=3)
        self.assertEqual(({}, {}), self.both(counter))
        self.add("stall-watch", "failed", 10)
        self.assertEqual({("stall-watch", "failed"): 1}, self.counted(counter.counts()[0]))
        self.assertEqual({("stall-watch", "failed"): 1}, self.counted(counter.counts()[0]))

    def test_a_baseline_of_a_multiple_of_the_row_limit_counts_the_first_run_after_it(self):
        for minute in range(1, 7):
            self.add("stall-watch", "completed", minute)
        counter = cron.CronRunsCounter(root=lambda: self.root, max_rows=3)
        counter.counts()
        counter.counts()
        self.add("stall-watch", "failed", 10)
        self.assertEqual({("stall-watch", "failed"): 1}, self.counted(counter.counts()[0]))

    def test_a_run_that_ends_while_a_long_baseline_is_still_being_read_counts(self):
        for minute in range(1, 9):
            self.add("stall-watch", "completed", minute)
        counter = cron.CronRunsCounter(root=lambda: self.root, max_rows=3)
        self.assertEqual(({}, {}), self.both(counter))  # the listener's start: the first three rows
        self.add("stall-watch", "failed", 10)
        self.add("stall-watch", "skipped", 11, reason="missed_window")
        for _ in range(6):
            counter.counts()
        runs, skips = self.both(counter)
        self.assertEqual({("stall-watch", "failed"): 1}, runs)
        self.assertEqual({("stall-watch", "missed_window"): 1}, skips)

    def test_a_run_in_flight_at_a_long_baseline_counts_when_it_ends_after_its_chunk_is_read(self):
        self.add("stall-watch", "completed", 1)
        in_flight = self.add("compliance-audit", "running", 2)
        for minute in range(3, 9):
            self.add("stall-watch", "completed", minute)
        counter = cron.CronRunsCounter(root=lambda: self.root, max_rows=20)
        self.assertEqual(({}, {}), self.both(counter))
        self.finish(in_flight, "failed")
        self.assertEqual({("compliance-audit", "failed"): 1}, self.counted(counter.counts()[0]))
        self.assertEqual({("compliance-audit", "failed"): 1}, self.counted(counter.counts()[0]))

    def test_a_full_read_moves_past_a_run_that_never_ends(self):
        counter = cron.CronRunsCounter(root=lambda: self.root, max_rows=3)
        counter.counts()
        self.add("compliance-audit", "running", 10)
        for minute in range(11, 19):
            self.add("stall-watch", "completed", minute)
        for _ in range(8):
            counter.counts()
        self.assertEqual({("stall-watch", "succeeded"): 8}, self.counted(counter.counts()[0]))


class RenderTest(_VolumeTest):
    def registry(self):
        return metrics.ChatMetrics(cron=self.counter)

    def test_the_job_label_does_not_collide_with_the_scrape_jobs_own(self):
        self.assertNotEqual("job", cron.JOB_LABEL)
        self.assertNotIn(cron.JOB_LABEL, ("instance", "job"))

    def test_both_families_are_declared_with_no_series_before_any_run(self):
        text = self.registry().render()
        self.assertIn("# TYPE kubeagents_cron_runs_total counter", text)
        self.assertIn("# TYPE kubeagents_cron_skips_total counter", text)
        self.assertEqual([], _RUN_LINE.findall(text) + _SKIP_LINE.findall(text))

    def test_prime_takes_the_baseline_so_the_first_scrape_counts_what_follows(self):
        self.add("stall-watch", "completed", 1, profile="platform")
        registry = self.registry()
        registry.prime()
        self.add("stall-watch", "failed", 10, profile="platform")
        self.add("stall-watch", "skipped", 11, profile="platform", reason="missed_window")
        text = registry.render()
        self.assertEqual(
            [("stall-watch", "failed", "1"), ("stall-watch", "lost", "0"), ("stall-watch", "succeeded", "0")],
            _RUN_LINE.findall(text),
        )
        self.assertEqual([("stall-watch", "missed_window", "1")], _SKIP_LINE.findall(text))

    def test_series_render_sorted_and_two_scrapes_agree(self):
        registry = self.registry()
        registry.prime()
        self.add("stall-watch", "completed", 10)
        self.add("compliance-audit", "failed", 11)
        self.add("compliance-audit", "completed", 12)
        text = registry.render()
        self.assertEqual(
            [
                ("compliance-audit", "failed", "1"),
                ("compliance-audit", "lost", "0"),
                ("compliance-audit", "succeeded", "1"),
                ("stall-watch", "failed", "0"),
                ("stall-watch", "lost", "0"),
                ("stall-watch", "succeeded", "1"),
            ],
            _RUN_LINE.findall(text),
        )
        self.assertEqual(text, registry.render())

    def test_a_broken_ledger_does_not_take_the_other_families_down(self):
        registry = self.registry()
        registry.prime()
        registry.record_inbound("slack")
        broken = cron.ledger_path(self.root / "profiles" / "broken")
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"\x00" * 4096)
        text = registry.render()
        self.assertIn('kubeagents_chat_messages_inbound_total{platform="slack"} 1', text)
        self.assertIn("# TYPE kubeagents_cron_runs_total counter", text)


if __name__ == "__main__":
    unittest.main()
