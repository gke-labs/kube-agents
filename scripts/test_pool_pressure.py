#!/usr/bin/env python3
"""Tests for scripts/pool_pressure.py."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pool_pressure as pp  # noqa: E402

TESTDATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "pool_pressure")
BREACH_DIR = os.path.join(TESTDATA, "breach")
QUIET_DIR = os.path.join(TESTDATA, "quiet")

# The fixtures are captured from real days: breach is 2026-08-26, the day
# oss-test-infra#2666 was filed about, and quiet is the day after. Each is read
# with --as-of set to the following midnight so the window covers exactly it.
BREACH_AS_OF = datetime(2026, 8, 27, tzinfo=timezone.utc)
QUIET_AS_OF = datetime(2026, 8, 28, tzinfo=timezone.utc)


def run(**kwargs):
    """measure() with its output captured, returning (exit_code, stdout)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = pp.measure(**kwargs)
    return code, buffer.getvalue()


class ParseTimestamps(unittest.TestCase):
    def test_a_trailing_z_is_accepted(self):
        parsed = pp.parse_rfc3339("2026-08-26T14:14:20Z")
        self.assertEqual(datetime(2026, 8, 26, 14, 14, 20, tzinfo=timezone.utc), parsed)

    def test_an_explicit_offset_is_normalised_to_utc(self):
        parsed = pp.parse_rfc3339("2026-08-26T16:14:20+02:00")
        self.assertEqual(datetime(2026, 8, 26, 14, 14, 20, tzinfo=timezone.utc), parsed)

    def test_a_naive_timestamp_is_read_as_utc(self):
        parsed = pp.parse_rfc3339("2026-08-26T14:14:20")
        self.assertEqual(timezone.utc, parsed.tzinfo)

    def test_empty_and_malformed_values_are_none_rather_than_raising(self):
        for value in ("", "   ", "not a date", "2026-13-45T99:99:99Z"):
            self.assertIsNone(pp.parse_rfc3339(value), value)


class Snowflake(unittest.TestCase):
    def test_a_build_id_decodes_to_when_its_pod_started(self):
        """The prefilter is only sound if the ID tracks pendingTime.

        Checked against the fixtures rather than a hardcoded pair, so the
        constant cannot drift away from the data it is used to filter.
        """
        for name in os.listdir(os.path.join(BREACH_DIR, "prowjobs")):
            with open(os.path.join(BREACH_DIR, "prowjobs", name)) as fh:
                prowjob = json.load(fh)
            build_id = int(prowjob["status"]["build_id"])
            pending = pp.parse_rfc3339(prowjob["status"]["pendingTime"])
            drift = abs((pp.snowflake_time(build_id) - pending).total_seconds())
            self.assertLess(drift, 5, f"{build_id} decoded {drift}s from its pendingTime")

    def test_the_window_slack_exceeds_the_longest_wait_the_fixtures_hold(self):
        """The prefilter must not drop a run that waited a long time.

        A build ID encodes the END of the wait, so a run created inside the
        window but dispatched after it encodes outside it. SNOWFLAKE_SLACK is
        what keeps that run in the candidate set.
        """
        source = pp._collect_waits_from_dir(
            BREACH_DIR, BREACH_AS_OF - timedelta(days=1), BREACH_AS_OF
        )
        worst = max(w.queue_seconds for w in source.value.waits)
        self.assertGreater(pp.SNOWFLAKE_SLACK.total_seconds(), worst)


class Percentile(unittest.TestCase):
    def test_an_empty_series_is_zero_rather_than_an_error(self):
        self.assertEqual(0.0, pp.percentile([], 95))

    def test_a_single_sample_is_its_own_percentile(self):
        self.assertEqual(7.0, pp.percentile([7.0], 50))
        self.assertEqual(7.0, pp.percentile([7.0], 95))

    def test_it_interpolates_between_ranks(self):
        self.assertEqual(3.0, pp.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50))
        self.assertAlmostEqual(4.6, pp.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 90))

    def test_it_does_not_depend_on_input_order(self):
        self.assertEqual(
            pp.percentile([5.0, 1.0, 4.0, 2.0, 3.0], 95),
            pp.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 95),
        )


class Banners(unittest.TestCase):
    """The one thing this check reads out of another repository.

    kube-agents-presubmits.yaml prints these lines and nobody editing it knows
    they are parsed here, so the grammar is what is pinned and the wording is
    not.
    """

    LOG = "\n".join((
        "+ set -o pipefail",
        "=== [2026-08-26T17:09:39Z] Leasing GCP Project from Boskos ===",
        "=== Target Cluster Context ===",
        "=== [2026-08-26T17:10:17Z] Cleaning Up GKE Resources ===",
        "=== [2026-08-26T17:12:00Z] Releasing Boskos Project ===",
    ))

    def test_an_untimestamped_banner_is_not_one(self):
        """`=== Target Cluster Context ===` is printed between the pair. Read as
        a banner it would put the acquire at zero seconds every time."""
        self.assertEqual(
            ["Leasing GCP Project from Boskos", "Cleaning Up GKE Resources",
             "Releasing Boskos Project"],
            [label for _, label in pp.banners(self.LOG)],
        )

    def test_the_lease_is_bounded_by_the_banner_that_follows_it(self):
        requested, acquired = pp.lease_window(self.LOG)
        self.assertEqual(pp.parse_rfc3339("2026-08-26T17:09:39Z"), requested)
        self.assertEqual(pp.parse_rfc3339("2026-08-26T17:10:17Z"), acquired)

    def test_the_first_boskos_banner_wins_over_the_release(self):
        """Releasing names Boskos too, three hours later on a slow run."""
        requested, _ = pp.lease_window(self.LOG)
        self.assertEqual(pp.parse_rfc3339("2026-08-26T17:09:39Z"), requested)

    def test_a_log_that_stops_at_the_lease_gives_no_acquire_time(self):
        """None, never zero. A run killed mid-acquire is the case that matters
        most, and counting it as an instant lease hides exactly that."""
        head = "=== [2026-08-26T17:09:39Z] Leasing GCP Project from Boskos ==="
        requested, acquired = pp.lease_window(head)
        self.assertIsNotNone(requested)
        self.assertIsNone(acquired)

    def test_rewording_that_keeps_the_keyword_still_matches(self):
        log = "\n".join((
            "=== [2026-08-26T17:09:39Z] Acquiring a project lease from boskos ===",
            "=== [2026-08-26T17:09:45Z] Next phase ===",
        ))
        requested, acquired = pp.lease_window(log)
        self.assertEqual(6.0, (acquired - requested).total_seconds())

    def test_dropping_the_keyword_goes_quiet_rather_than_wrong(self):
        """The failure mode that is left. It reports the segment as unmeasured,
        which segment_breakdown's coverage floor turns into a visible row."""
        log = "\n".join((
            "=== [2026-08-26T17:09:39Z] Leasing GCP Project ===",
            "=== [2026-08-26T17:09:45Z] Next phase ===",
        ))
        self.assertEqual((None, None), pp.lease_window(log))

    def test_a_build_log_fixture_yields_the_pair(self):
        path = os.path.join(BREACH_DIR, "logs", "worst-queue-stall.txt")
        with open(path) as fh:
            requested, acquired = pp.lease_window(fh.read())
        self.assertIsNotNone(requested)
        self.assertIsNotNone(acquired)


class WaitFromProwjob(unittest.TestCase):
    def _prowjob(self, **status):
        return {
            "metadata": {"creationTimestamp": "2026-08-26T14:00:00Z"},
            "spec": {"max_concurrency": 3, "refs": {"pulls": [{"number": 961}]}},
            "status": {"build_id": "2092660728946233344", **status},
        }

    def test_the_wait_is_creation_to_pending(self):
        wait = pp.wait_from_prowjob(self._prowjob(pendingTime="2026-08-26T14:30:00Z"))
        self.assertEqual(30.0, wait.minutes)
        self.assertEqual("961", wait.pull)
        self.assertEqual(3, wait.max_concurrency)
        self.assertEqual("2026-08-26", wait.day)

    def test_a_record_with_no_pending_time_is_dropped(self):
        self.assertIsNone(pp.wait_from_prowjob(self._prowjob()))

    def test_a_record_with_no_creation_time_is_dropped(self):
        prowjob = self._prowjob(pendingTime="2026-08-26T14:30:00Z")
        prowjob["metadata"]["creationTimestamp"] = ""
        self.assertIsNone(pp.wait_from_prowjob(prowjob))

    def test_a_pending_time_before_creation_clamps_to_zero(self):
        """Prow sets the two stamps from different controllers.

        An instantly-dispatched run can carry a pendingTime a few hundred
        milliseconds before its creationTimestamp. Left negative, it drags the
        median below any wait a run actually experienced.
        """
        wait = pp.wait_from_prowjob(self._prowjob(pendingTime="2026-08-26T13:59:59Z"))
        self.assertEqual(0.0, wait.queue_seconds)

    def test_a_non_integer_max_concurrency_is_dropped_rather_than_trusted(self):
        prowjob = self._prowjob(pendingTime="2026-08-26T14:30:00Z")
        prowjob["spec"]["max_concurrency"] = "ten"
        self.assertIsNone(pp.wait_from_prowjob(prowjob).max_concurrency)

    def test_all_four_segments_come_out_of_the_three_artifacts(self):
        wait = pp.wait_from_prowjob(
            self._prowjob(pendingTime="2026-08-26T14:30:00Z"),
            started={"timestamp": 1787754630},  # 2026-08-26T14:30:30Z
            log_head="\n".join((
                "=== [2026-08-26T14:31:00Z] Leasing GCP Project from Boskos ===",
                "=== [2026-08-26T14:31:12Z] Cleaning Up GKE Resources ===",
            )),
        )
        self.assertEqual(
            {"queue": 1800.0, "pod": 30.0, "setup": 30.0, "lease": 12.0},
            wait.segments,
        )
        self.assertEqual(1872.0, wait.total_seconds)

    def test_a_missing_started_json_costs_two_segments_not_the_run(self):
        """The pod stamp anchors both middle segments. Losing it must not lose
        the queue wait, which is the number the thresholds were written for."""
        wait = pp.wait_from_prowjob(
            self._prowjob(pendingTime="2026-08-26T14:30:00Z"),
            log_head="=== [2026-08-26T14:31:00Z] Leasing GCP Project from Boskos ===\n"
                     "=== [2026-08-26T14:31:12Z] Cleaning Up GKE Resources ===",
        )
        self.assertEqual(1800.0, wait.queue_seconds)
        self.assertIsNone(wait.pod_seconds)
        self.assertIsNone(wait.setup_seconds)
        self.assertEqual(12.0, wait.lease_seconds)
        self.assertEqual(1812.0, wait.total_seconds)

    def test_prowjob_json_alone_still_measures_the_queue(self):
        wait = pp.wait_from_prowjob(self._prowjob(pendingTime="2026-08-26T14:30:00Z"))
        self.assertEqual({"queue", "pod", "setup", "lease"}, set(wait.segments))
        self.assertEqual(1800.0, wait.total_seconds)

    def test_every_captured_fixture_parses(self):
        root = os.path.join(BREACH_DIR, "prowjobs")
        for name in os.listdir(root):
            with open(os.path.join(root, name)) as fh:
                self.assertIsNotNone(pp.wait_from_prowjob(json.load(fh)), name)


class LiveQueueFromDeck(unittest.TestCase):
    def _queue(self, from_dir, now):
        source = pp.fetch_live_queue(now, from_dir=from_dir)
        self.assertTrue(source.ok, source.error)
        return source.value

    def test_a_finished_run_is_not_counted_as_waiting(self):
        """The mistake this guards against invented an outage once already.

        Classifying every job without a pendingTime as "still queued" counts
        aborted runs as live stalls, and aborted is the most common state in
        this job's Deck history by a wide margin.
        """
        queue = self._queue(QUIET_DIR, QUIET_AS_OF)
        self.assertEqual([], queue.waiting)

    def test_running_jobs_are_collected_for_the_lease_cross_reference(self):
        queue = self._queue(QUIET_DIR, QUIET_AS_OF)
        self.assertEqual(10, queue.running)
        self.assertNotIn("", queue.running_build_ids)

    def test_waiting_runs_are_ordered_longest_first(self):
        queue = self._queue(BREACH_DIR, BREACH_AS_OF)
        minutes = [w.minutes for w in queue.waiting]
        self.assertEqual(sorted(minutes, reverse=True), minutes)
        self.assertEqual(4, len(minutes))

    def test_another_tenants_job_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "deck.json"), "w") as fh:
                json.dump({"items": [{
                    "metadata": {"creationTimestamp": "2026-08-26T12:00:00Z"},
                    "spec": {"job": "some-other-repo-test"},
                    "status": {"state": "triggered"},
                }]}, fh)
            self.assertEqual([], self._queue(tmp, BREACH_AS_OF).waiting)

    def test_an_absent_capture_is_an_error_rather_than_an_empty_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = pp.fetch_live_queue(BREACH_AS_OF, from_dir=tmp)
        self.assertFalse(source.ok)


class PoolStateFields(unittest.TestCase):
    def test_a_state_with_no_resources_in_it_is_omitted_rather_than_zero(self):
        """Boskos leaves out a state whose count is zero.

        A fully leased pool has no `free` key at all, so reading it with [] --
        or summing the keys that happen to be present and calling it the total
        -- is the difference between "the pool is full" and a KeyError.
        """
        full = pp.PoolState({"busy": 15}, {})
        self.assertEqual(15, full.busy)
        self.assertEqual(0, full.free)
        self.assertEqual(15, full.total)

    def test_the_unowned_placeholder_is_not_a_lease_holder(self):
        pool = pp.PoolState({"busy": 1, "free": 4},
                            {pp.BOSKOS_NO_OWNER: 4, "pull-kube-agents-smoke-test-111": 1})
        self.assertEqual(["pull-kube-agents-smoke-test-111"], pool.lease_holders())

    def test_a_third_state_is_counted_as_neither_leased_nor_available(self):
        """`cleaning` and `dirty` are projects nothing can lease right now. Read
        as free they make a pool that is out of projects look like it has some.
        """
        pool = pp.PoolState({"busy": 10, "free": 2, "cleaning": 3}, {})
        self.assertEqual(15, pool.total)
        self.assertEqual(3, pool.in_transition)

    def test_a_pool_in_two_states_has_nothing_in_transition(self):
        self.assertEqual(0, pp.PoolState({"busy": 10, "free": 5}, {}).in_transition)


class LeakedLeases(unittest.TestCase):
    def _pool(self, holders):
        return pp.PoolState({"busy": len(holders)},
                            {f"pull-kube-agents-smoke-test-{b}": 1 for b in holders})

    def test_a_lease_held_by_a_running_job_is_not_a_leak(self):
        queue = pp.LiveQueue([], {"111", "222"})
        self.assertEqual([], pp.leaked_leases(self._pool(["111", "222"]), queue))

    def test_a_lease_no_running_job_accounts_for_is_reported(self):
        queue = pp.LiveQueue([], {"111"})
        self.assertEqual(
            ["pull-kube-agents-smoke-test-222"],
            pp.leaked_leases(self._pool(["111", "222"]), queue),
        )

    def test_the_unowned_placeholder_is_not_a_leak(self):
        """Boskos files free resources under the empty-string owner.

        It is a count of nobody. Treated as a holder it would make every idle
        pool report a mystery lease.
        """
        pool = pp.PoolState({"busy": 1, "free": 4}, {"": 4, "pull-kube-agents-smoke-test-111": 1})
        self.assertEqual([], pp.leaked_leases(pool, pp.LiveQueue([], {"111"})))

    def test_nothing_is_reported_when_either_source_is_missing(self):
        """With no list of running jobs, every holder looks orphaned."""
        self.assertEqual([], pp.leaked_leases(self._pool(["111"]), None))
        self.assertEqual([], pp.leaked_leases(None, pp.LiveQueue([], {"111"})))


class LatestMaxConcurrency(unittest.TestCase):
    def _wait(self, created, concurrency):
        moment = pp.parse_rfc3339(created)
        return pp.Wait(1, "1", moment, moment, concurrency)

    def test_the_newest_run_wins(self):
        waits = [self._wait("2026-08-21T00:00:00Z", 2),
                 self._wait("2026-08-30T00:00:00Z", 10),
                 self._wait("2026-08-27T00:00:00Z", 6)]
        self.assertEqual(10, pp.latest_max_concurrency(waits))

    def test_runs_without_a_cap_are_skipped(self):
        waits = [self._wait("2026-08-21T00:00:00Z", 2),
                 self._wait("2026-08-30T00:00:00Z", None)]
        self.assertEqual(2, pp.latest_max_concurrency(waits))

    def test_no_data_is_unknown_rather_than_a_default(self):
        self.assertIsNone(pp.latest_max_concurrency([]))


class Cause(unittest.TestCase):
    """The four verdicts, which decide whether money gets spent."""

    def _pool(self, busy, free):
        counts = {"busy": busy}
        if free:
            counts["free"] = free
        return pp.PoolState(counts, {})

    def test_a_full_pool_is_capacity(self):
        label, text = pp.cause(self._pool(15, 0), pp.LiveQueue([], set()), 15)
        self.assertEqual(pp.CAUSE_CAPACITY, label)
        self.assertIn("Onboard the next project", " ".join(text))

    def test_free_projects_below_the_cap_is_the_cap(self):
        label, text = pp.cause(
            self._pool(10, 5), pp.LiveQueue([], {str(n) for n in range(10)}), 10
        )
        self.assertEqual(pp.CAUSE_CONCURRENCY_CAP, label)
        self.assertIn("Raise the cap", " ".join(text))
        self.assertIn("Deck confirms it: 10 run(s) running", " ".join(text))

    def test_the_deck_confirmation_is_left_out_when_deck_does_not_confirm(self):
        """Running below the cap does not evidence the cap, so the line that
        says it does must not appear."""
        _, text = pp.cause(self._pool(10, 5), pp.LiveQueue([], {"1", "2"}), 10)
        self.assertNotIn("Deck confirms", " ".join(text))

    def test_free_projects_with_room_under_the_cap_is_the_control_plane(self):
        label, text = pp.cause(self._pool(3, 12), pp.LiveQueue([], {"1", "2", "3"}), 15)
        self.assertEqual(pp.CAUSE_CONTROL_PLANE, label)
        self.assertIn("oss-test-infra#2666", " ".join(text))

    def test_an_unreadable_pool_gives_no_verdict_either_way(self):
        label, text = pp.cause(None, None, 10)
        self.assertEqual(pp.CAUSE_UNKNOWN, label)
        self.assertIn("Do not onboard a project", " ".join(text))

    def test_a_full_pool_outranks_the_cap(self):
        """A cap below the pool size is worth saying, but it is not the cause
        when there is nothing free to reach in the first place."""
        label, _ = pp.cause(self._pool(15, 0), pp.LiveQueue([], set()), 10)
        self.assertEqual(pp.CAUSE_CAPACITY, label)

    def test_an_unknown_cap_cannot_be_blamed(self):
        """With no cap read from any run, the cap branch has nothing to test
        against and the verdict must fall through rather than guess."""
        label, _ = pp.cause(self._pool(3, 12), pp.LiveQueue([], {"1"}), None)
        self.assertEqual(pp.CAUSE_CONTROL_PLANE, label)

    def test_a_cap_at_the_pool_size_earns_a_caveat_not_a_label(self):
        """oss-test-infra#2678 put the cap at 15 against a pool of 15, so no
        project is held back for a late pod. The label set stays at four because
        the periodic and any alert policy filter on it."""
        label, text = pp.cause(self._pool(15, 0), pp.LiveQueue([], set()), 15)
        self.assertEqual(pp.CAUSE_CAPACITY, label)
        self.assertIn("boskosctl acquire", " ".join(text))

    def test_a_cap_below_the_pool_does_not_earn_it(self):
        label, text = pp.cause(self._pool(15, 0), pp.LiveQueue([], set()), 6)
        self.assertEqual(pp.CAUSE_CAPACITY, label)
        self.assertNotIn("boskosctl acquire", " ".join(text))

    def test_the_caveat_names_what_is_in_transition(self):
        pool = pp.PoolState({"busy": 12, "cleaning": 3}, {})
        _, text = pp.cause(pool, pp.LiveQueue([], set()), 15)
        self.assertIn("3 project(s) are in neither state", " ".join(text))


class DailyRows(unittest.TestCase):
    def _waits(self, day, minutes):
        created = pp.parse_rfc3339(f"{day}T12:00:00Z")
        return [pp.Wait(n, "1", created, created + timedelta(minutes=m), 10)
                for n, m in enumerate(minutes)]

    def test_rows_come_back_in_date_order(self):
        waits = self._waits("2026-08-27", [1]) + self._waits("2026-08-26", [1])
        self.assertEqual(["2026-08-26", "2026-08-27"], [r.day for r in pp.daily_rows(waits)])

    def test_a_day_over_the_p95_line_breaches(self):
        row = pp.DayRow("2026-08-26", self._waits("2026-08-26", [0, 0, 1, 2, 200]))
        self.assertTrue(row.breached(15, 45))

    def test_a_day_over_the_p50_line_breaches(self):
        row = pp.DayRow("2026-08-26", self._waits("2026-08-26", [20, 21, 22, 23, 24]))
        self.assertTrue(row.breached(15, 45))

    def test_too_few_runs_cannot_trip_the_threshold(self):
        """Two samples put any number at p95.

        A weekend here runs single digits of builds, which is the reason the
        window is a week rather than the rolling day the policy was first
        written for.
        """
        row = pp.DayRow("2026-08-29", self._waits("2026-08-29", [500, 600]))
        self.assertFalse(row.breached(15, 45))
        self.assertEqual(600.0, row.worst)

    def test_the_highest_cap_seen_that_day_is_reported(self):
        created = pp.parse_rfc3339("2026-08-27T12:00:00Z")
        waits = [pp.Wait(1, "1", created, created, 6), pp.Wait(2, "2", created, created, 10)]
        self.assertEqual(10, pp.DayRow("2026-08-27", waits).max_concurrency)


class Outliers(unittest.TestCase):
    def test_they_are_listed_longest_first_and_exclude_the_boundary(self):
        created = pp.parse_rfc3339("2026-08-26T12:00:00Z")
        waits = [pp.Wait(n, "1", created, created + timedelta(minutes=m), 10)
                 for n, m in enumerate((10, 46, 45, 90))]
        self.assertEqual([90.0, 46.0], [w.minutes for w in pp.outliers(waits, 45)])


class SegmentBreakdown(unittest.TestCase):
    def _wait(self, queue_minutes, lease_seconds):
        """One wait with a queue segment and, optionally, a lease segment."""
        created = pp.parse_rfc3339("2026-08-26T12:00:00Z")
        pending = created + timedelta(minutes=queue_minutes)
        requested = acquired = None
        if lease_seconds is not None:
            requested = pending
            acquired = pending + timedelta(seconds=lease_seconds)
        return pp.Wait(1, "1", created, pending, 15,
                       lease_requested=requested, lease_acquired=acquired)

    def _row(self, waits, segment):
        return next(r for r in pp.segment_breakdown(waits) if r["segment"] == segment)

    def test_a_median_is_reported_with_the_count_behind_it(self):
        waits = [self._wait(10, 2), self._wait(20, 4), self._wait(30, 6)]
        row = self._row(waits, pp.SEGMENT_LEASE)
        self.assertEqual(0.1, row["median_minutes"])
        self.assertEqual(3, row["measured"])
        self.assertEqual(3, row["runs"])

    def test_coverage_below_the_floor_withholds_the_median(self):
        """One run in five parsing is a source that has moved, and its median
        reads exactly like a healthy pool. Withheld, the row says so instead."""
        waits = [self._wait(10, 2)] + [self._wait(10, None) for _ in range(4)]
        row = self._row(waits, pp.SEGMENT_LEASE)
        self.assertIsNone(row["median_minutes"])
        self.assertEqual(1, row["measured"])
        self.assertEqual(5, row["runs"])

    def test_a_segment_no_run_measured_is_withheld_rather_than_zero(self):
        row = self._row([self._wait(10, None)], pp.SEGMENT_LEASE)
        self.assertIsNone(row["median_minutes"])
        self.assertEqual(0, row["measured"])

    def test_the_queue_segment_is_always_covered(self):
        """prowjob.json is the one artifact a Wait cannot exist without."""
        waits = [self._wait(10, None) for _ in range(5)]
        self.assertEqual(5, self._row(waits, pp.SEGMENT_QUEUE)["measured"])

    def test_every_segment_gets_a_row_in_a_fixed_order(self):
        rows = pp.segment_breakdown([self._wait(10, 2)])
        self.assertEqual(
            [pp.SEGMENT_QUEUE, pp.SEGMENT_POD, pp.SEGMENT_SETUP, pp.SEGMENT_LEASE],
            [r["segment"] for r in rows],
        )


class Percentiles(unittest.TestCase):
    """Percentiles run over the whole setup time, not the queue wait alone.

    The runbook thresholds were written about the queue, and total equals queue
    today because the other three segments are seconds. A lease that starts
    taking minutes has to move the number the thresholds read.
    """

    def _wait(self, queue_minutes, lease_minutes):
        created = pp.parse_rfc3339("2026-08-26T12:00:00Z")
        pending = created + timedelta(minutes=queue_minutes)
        return pp.Wait(1, "1", created, pending, 15, lease_requested=pending,
                       lease_acquired=pending + timedelta(minutes=lease_minutes))

    def test_a_slow_lease_moves_the_day(self):
        row = pp.DayRow("2026-08-26", [self._wait(2, 9) for _ in range(5)])
        self.assertEqual(11.0, row.p50)
        self.assertEqual(11.0, row.worst)

    def test_a_run_slow_only_in_the_lease_is_still_an_outlier(self):
        over = pp.outliers([self._wait(1, 50), self._wait(1, 1)], 45)
        self.assertEqual([51.0], [w.minutes for w in over])


class SweepDeadline(unittest.TestCase):
    """The sweep stops itself rather than being killed by the job timeout.

    A killed periodic reds TestGrid with no output, which says nothing about
    the queue. Build volume is what makes this reachable: the smoke test now
    runs on every pull request and more of them land at once.
    """

    # One build ID from each of the two fixture days, used only for the day
    # their snowflakes decode to.
    OLD_BUILD = 2092660728946233344  # 2026-08-26
    NEW_BUILD = 2092904488661684224  # 2026-08-27
    AS_OF = datetime(2026, 8, 28, tzinfo=timezone.utc)

    # Dispatched on the newer day, created on the older one -- a run that waited
    # across midnight, which is the #2666 shape. Its snowflake day is read, so
    # the deadline never drops it; only the window filter can.
    STRADDLING_BUILD = 2092904488661684225
    STRADDLING_CREATED = datetime(2026, 8, 26, 22, 0, tzinfo=timezone.utc)

    def _sweep(self, clock):
        """collect_waits over two days, with monotonic() reading from `clock`."""
        builds = (self.OLD_BUILD, self.NEW_BUILD, self.STRADDLING_BUILD)
        entries = {build: str(build) for build in builds}

        def wait_for(path):
            build = int(path)
            moment = pp.snowflake_time(build)
            created = (
                self.STRADDLING_CREATED if build == self.STRADDLING_BUILD else moment
            )
            return pp.Wait(build, "1", created, moment, 15)

        with unittest.mock.patch.object(pp.shutil, "which", return_value="/usr/bin/gcloud"), \
                unittest.mock.patch.object(pp, "_index_entries_from_gcs",
                                           return_value=(entries, None)), \
                unittest.mock.patch.object(pp, "_wait_from_gcs", side_effect=wait_for), \
                unittest.mock.patch.object(pp.time, "monotonic", side_effect=clock):
            return pp.collect_waits(
                self.AS_OF - timedelta(days=2), self.AS_OF, deadline_seconds=10
            )

    def test_a_sweep_inside_the_deadline_covers_the_window_it_was_asked_for(self):
        sweep = self._sweep([0, 0, 0, 5]).value
        self.assertFalse(sweep.truncated)
        self.assertEqual(3, sweep.builds_read)
        self.assertEqual(self.AS_OF - timedelta(days=2), sweep.window_start)
        self.assertEqual(3, len(sweep.waits))

    def test_the_deadline_shortens_the_window_to_a_whole_day(self):
        """Newest day first, so what is lost is the oldest context rather than
        today's numbers -- and the boundary is a date, not a ragged edge."""
        sweep = self._sweep([0, 0, 99, 99]).value
        self.assertTrue(sweep.truncated)
        self.assertEqual(2, sweep.builds_read)
        self.assertEqual(datetime(2026, 8, 27, tzinfo=timezone.utc), sweep.window_start)

    def test_a_run_created_below_the_shortened_window_is_dropped_not_mixed_in(self):
        """The straddling run was read, because its build ID lands on the day
        that was swept. Counting it would put one whole day and the tail of a
        day nobody finished reading into a single percentile."""
        sweep = self._sweep([0, 0, 99, 99]).value
        self.assertEqual([self.NEW_BUILD], [w.build_id for w in sweep.waits])

    def test_the_truncation_is_stated_rather_than_left_to_a_smaller_count(self):
        sweep = self._sweep([0, 0, 99, 99])
        summary = pp.summarise(
            self.AS_OF - timedelta(days=2), self.AS_OF, 15, 45, 45,
            sweep, pp.Source(error="not read"), pp.Source(error="not read"),
        )
        self.assertTrue(summary["trend"]["truncated"])
        self.assertEqual("2026-08-27", summary["trend"]["window_start"][:10])
        self.assertIn("ran out of time", pp.render(summary))


class EndToEnd(unittest.TestCase):
    def test_the_2666_day_goes_red_and_names_the_runs(self):
        """The requirement from issue #1069: a breach goes red.

        Run against the real artifacts of the day the incident was filed
        about, rather than a synthetic stall, so the check is shown catching
        the thing it was built for.
        """
        code, out = run(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        self.assertEqual(pp.EXIT_BREACH, code)
        self.assertIn("2026-08-26", out)
        self.assertIn("175.9 min", out)
        self.assertIn("queue 175.2", out)
        self.assertIn("CAPACITY", out)

    def test_a_quiet_day_is_green_and_reports_no_leaks(self):
        code, out = run(from_dir=QUIET_DIR, as_of=QUIET_AS_OF, window_days=1)
        self.assertEqual(pp.EXIT_OK, code)
        self.assertIn("WITHIN THRESHOLD", out)
        self.assertNotIn("lease(s) held by runs Deck does not know about", out)

    def test_raising_the_thresholds_clears_the_breach(self):
        """Proves the verdict follows the thresholds rather than the fixture."""
        code, out = run(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1,
                        p50_limit=600, p95_limit=600, outlier_limit=600)
        self.assertEqual(pp.EXIT_OK, code)
        self.assertIn("WITHIN THRESHOLD", out)

    def test_an_unmeasurable_window_is_not_green(self):
        """Exit 2, not 0. A gauge that could not read must not read as healthy,
        which is the whole failure this check exists to remove."""
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run(from_dir=tmp, as_of=QUIET_AS_OF, window_days=1)
        self.assertEqual(pp.EXIT_UNMEASURED, code)
        self.assertIn("COULD NOT MEASURE", out)

    def test_a_breach_still_reports_when_the_pool_cannot_be_read(self):
        """The trend is enough to go red; only the verdict needs Boskos."""
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(os.path.join(BREACH_DIR, "prowjobs"), os.path.join(tmp, "prowjobs"))
            code, out = run(from_dir=tmp, as_of=BREACH_AS_OF, window_days=1)
        self.assertEqual(pp.EXIT_BREACH, code)
        self.assertIn("Cause unknown", out)

    def _quiet_day_with_a_live_queue(self, tmp, waiting_minutes):
        """A green day, with a Deck payload holding one run still queued.

        Everything but deck.json comes from `quiet/`, so the trend is the same
        green trend and the live queue is the only thing that can change the
        verdict.
        """
        for name in ("prowjobs", "started", "logs", "boskos.json"):
            os.symlink(os.path.join(QUIET_DIR, name), os.path.join(tmp, name))
        created = QUIET_AS_OF - timedelta(minutes=waiting_minutes)
        deck = {"items": [{
            "metadata": {"creationTimestamp": created.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "spec": {"job": pp.JOB_NAME, "refs": {"pulls": [{"number": 1234}]}},
            "status": {"build_id": "2092900000000000000", "state": "triggered"},
        }]}
        with open(os.path.join(tmp, "deck.json"), "w") as fh:
            json.dump(deck, fh)

    def test_one_run_queued_past_p95_breaches_on_its_own(self):
        """The half GCS cannot see. A run still in `triggered` has written no
        artifacts, so during a stall the trend goes quiet as the queue grows --
        which is why the live queue trips the verdict without it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._quiet_day_with_a_live_queue(tmp, waiting_minutes=90)
            code, out = run(from_dir=tmp, as_of=QUIET_AS_OF, window_days=1)
        self.assertEqual(pp.EXIT_BREACH, code)
        self.assertIn("Queued right now", out)

    def test_a_live_queue_under_p95_leaves_the_day_green(self):
        """Pins the previous test to the threshold rather than to the presence
        of a queued run at all."""
        with tempfile.TemporaryDirectory() as tmp:
            self._quiet_day_with_a_live_queue(tmp, waiting_minutes=5)
            code, out = run(from_dir=tmp, as_of=QUIET_AS_OF, window_days=1)
        self.assertEqual(pp.EXIT_OK, code)
        self.assertIn("WITHIN THRESHOLD", out)

    def test_an_unmeasured_segment_stays_in_its_own_column(self):
        """"container start -> lease requested" is exactly as wide as "not
        measured", so a label column sized to the longest label leaves no gap
        and the two render as one word. Reached whenever a run has no log --
        here, every one of them.
        """
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(os.path.join(BREACH_DIR, "prowjobs"), os.path.join(tmp, "prowjobs"))
            _, out = run(from_dir=tmp, as_of=BREACH_AS_OF, window_days=1)
        self.assertIn("not measured", out)
        for _, label in pp.SEGMENT_LABELS:
            self.assertIn(f"{label} ", out)

    def test_a_window_with_no_runs_is_green_rather_than_unmeasured(self):
        code, out = run(from_dir=QUIET_DIR, as_of=datetime(2020, 1, 1, tzinfo=timezone.utc),
                        window_days=1)
        self.assertEqual(pp.EXIT_OK, code)
        self.assertIn("No runs were created", out)


class JsonOutput(unittest.TestCase):
    """The --json payload is what the alert sender reads, so it is an interface.

    Everything asserted here is a field something downstream branches on. The
    rendered report is checked to be present but not for its wording, which the
    EndToEnd cases cover.
    """

    def _payload(self, **kwargs):
        _, out = run(as_json=True, **kwargs)
        return json.loads(out)

    def test_a_breach_payload_carries_the_numbers_and_the_cause(self):
        payload = self._payload(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        self.assertEqual("BREACH", payload["verdict"])
        self.assertEqual(pp.EXIT_BREACH, payload["exit_code"])
        self.assertEqual(pp.CAUSE_CAPACITY, payload["cause"])
        self.assertEqual(["2026-08-26"], payload["trend"]["breached_days"])
        self.assertEqual(175.9, payload["trend"]["worst_minutes"])
        self.assertEqual(5, payload["trend"]["runs"])
        self.assertEqual(0, payload["pool"]["free"])
        self.assertEqual([], payload["leaked_leases"])

    def test_a_quiet_payload_says_so_without_a_cause_of_capacity(self):
        payload = self._payload(from_dir=QUIET_DIR, as_of=QUIET_AS_OF, window_days=1)
        self.assertEqual("OK", payload["verdict"])
        self.assertEqual(pp.EXIT_OK, payload["exit_code"])
        self.assertEqual([], payload["trend"]["breached_days"])
        self.assertEqual([], payload["outliers"])
        self.assertEqual(5, payload["pool"]["free"])

    def test_a_source_that_could_not_be_read_says_which_and_why(self):
        """`read: false` and a reason, not a zero. A consumer that cannot tell
        "no leaks" from "never looked" reports the second as the first."""
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(os.path.join(BREACH_DIR, "prowjobs"), os.path.join(tmp, "prowjobs"))
            payload = self._payload(from_dir=tmp, as_of=BREACH_AS_OF, window_days=1)
        self.assertTrue(payload["trend"]["read"])
        self.assertFalse(payload["pool"]["read"])
        self.assertIsNone(payload["pool"]["free"])
        self.assertIn("boskos.json", payload["pool"]["error"])
        self.assertEqual(pp.CAUSE_UNKNOWN, payload["cause"])

    def test_the_unmeasured_verdict_reaches_the_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(from_dir=tmp, as_of=QUIET_AS_OF, window_days=1)
        self.assertEqual("UNMEASURED", payload["verdict"])
        self.assertEqual(pp.EXIT_UNMEASURED, payload["exit_code"])
        self.assertFalse(payload["trend"]["read"])

    def test_the_rendered_report_travels_inside_the_payload(self):
        """So a sender gets both from one run. A seven-day window costs about a
        minute, and running it twice to get two formats doubles that."""
        payload = self._payload(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        self.assertIn("THRESHOLD BREACHED", payload["report"])

    def test_the_payload_and_the_table_agree(self):
        """Two renderings of one run. If they disagree, one of them is lying."""
        _, text = run(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        payload = self._payload(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        self.assertEqual(text.strip(), payload["report"].strip())

    def test_every_documented_cause_label_is_a_constant_the_payload_can_hold(self):
        labels = {pp.CAUSE_CAPACITY, pp.CAUSE_CONCURRENCY_CAP,
                  pp.CAUSE_CONTROL_PLANE, pp.CAUSE_UNKNOWN}
        self.assertEqual(4, len(labels), "two cause labels collide")
        breach = self._payload(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        self.assertIn(breach["cause"], labels)

    def test_a_green_run_diagnoses_nothing(self):
        """`cause` answers "why are runs waiting". On a green run they are not,
        and a label there reads to a consumer as a live problem."""
        payload = self._payload(from_dir=QUIET_DIR, as_of=QUIET_AS_OF, window_days=1)
        self.assertEqual(pp.VERDICT_OK, payload["verdict"])
        self.assertIsNone(payload["cause"])
        self.assertEqual([], payload["cause_text"])


class JunitOutput(unittest.TestCase):
    """The --junit file is what the TestGrid tab reads, so it is an interface.

    The row design and the reasons for it are with the JUNIT_* constants in
    pool_pressure.py; every shape they argue for is pinned here.
    """

    def _junit(self, **kwargs):
        """measure() with --junit, returning (exit_code, parsed <testsuite>)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "junit_pool_pressure.xml")
            code, _ = run(junit_path=path, **kwargs)
            root = ET.parse(path).getroot()
        return code, root

    @staticmethod
    def _rows(root):
        return {case.get("name"): case for case in root.findall("testcase")}

    @staticmethod
    def _value(case):
        prop = case.find(f"properties/property[@name='{pp.JUNIT_METRIC_PROPERTY}']")
        return None if prop is None else prop.get("value")

    def test_the_file_is_a_testsuite_of_the_five_rows_in_order(self):
        _, root = self._junit(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        self.assertEqual("testsuite", root.tag)
        self.assertEqual(pp.JUNIT_SUITE_NAME, root.get("name"))
        self.assertEqual(list(pp.JUNIT_ROW_NAMES),
                         [case.get("name") for case in root.findall("testcase")])
        self.assertEqual("5", root.get("tests"))

    def test_a_breach_fails_the_verdict_row_and_carries_the_numbers(self):
        code, root = self._junit(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        rows = self._rows(root)
        self.assertEqual(pp.EXIT_BREACH, code)
        failure = rows[pp.JUNIT_ROW_VERDICT].find("failure")
        self.assertIsNotNone(failure)
        self.assertEqual("BREACH (CAPACITY)", failure.get("message"))
        self.assertIn("Every project was leased", failure.text)
        self.assertEqual("9.9", self._value(rows[pp.JUNIT_ROW_P50]))
        self.assertEqual("168.7", self._value(rows[pp.JUNIT_ROW_P95]))
        self.assertEqual("150.0", self._value(rows[pp.JUNIT_ROW_QUEUE]))
        self.assertEqual("0", self._value(rows[pp.JUNIT_ROW_FREE]))
        self.assertEqual("1", root.get("failures"))

    def test_the_numbers_are_the_json_payloads(self):
        """One run, two renderings. The tab and the alert must not disagree."""
        _, out = run(as_json=True, from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        payload = json.loads(out)
        _, root = self._junit(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        rows = self._rows(root)
        self.assertEqual(str(payload["trend"]["p50_minutes"]), self._value(rows[pp.JUNIT_ROW_P50]))
        self.assertEqual(str(payload["trend"]["p95_minutes"]), self._value(rows[pp.JUNIT_ROW_P95]))
        self.assertEqual(str(max(r["minutes"] for r in payload["queue"]["waiting_runs"])),
                         self._value(rows[pp.JUNIT_ROW_QUEUE]))
        self.assertEqual(str(payload["pool"]["free"]), self._value(rows[pp.JUNIT_ROW_FREE]))

    def test_only_the_verdict_row_can_fail(self):
        _, root = self._junit(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        for name, case in self._rows(root).items():
            if name != pp.JUNIT_ROW_VERDICT:
                self.assertIsNone(case.find("failure"), name)
                self.assertIsNone(case.find("error"), name)

    def test_a_quiet_day_passes_with_an_empty_queue_as_a_measured_zero(self):
        code, root = self._junit(from_dir=QUIET_DIR, as_of=QUIET_AS_OF, window_days=1)
        rows = self._rows(root)
        self.assertEqual(pp.EXIT_OK, code)
        self.assertIsNone(rows[pp.JUNIT_ROW_VERDICT].find("failure"))
        self.assertEqual("0", root.get("failures"))
        self.assertEqual("5", self._value(rows[pp.JUNIT_ROW_FREE]))
        # Deck was read and nothing was waiting: a number, not a skip.
        self.assertIsNone(rows[pp.JUNIT_ROW_QUEUE].find("skipped"))
        self.assertEqual(0.0, float(self._value(rows[pp.JUNIT_ROW_QUEUE])))

    def test_an_unmeasurable_run_fails_the_verdict_and_skips_the_setup_rows(self):
        """Exit 2 is red on the tab too, and the percentiles of a sweep that
        could not read are skipped rather than graphed as zero."""
        with tempfile.TemporaryDirectory() as tmp:
            code, root = self._junit(from_dir=tmp, as_of=QUIET_AS_OF, window_days=1)
        rows = self._rows(root)
        self.assertEqual(pp.EXIT_UNMEASURED, code)
        failure = rows[pp.JUNIT_ROW_VERDICT].find("failure")
        self.assertIsNotNone(failure)
        self.assertEqual(pp.VERDICT_UNMEASURED, failure.get("message"))
        self.assertTrue(failure.text)
        for name in (pp.JUNIT_ROW_P50, pp.JUNIT_ROW_P95):
            self.assertIsNotNone(rows[name].find("skipped"), name)
            self.assertIsNone(self._value(rows[name]), name)

    def test_an_unread_source_skips_its_rows_with_the_reason(self):
        """`prowjobs/` alone: the trend reads, Deck and Boskos do not."""
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(os.path.join(BREACH_DIR, "prowjobs"), os.path.join(tmp, "prowjobs"))
            code, root = self._junit(from_dir=tmp, as_of=BREACH_AS_OF, window_days=1)
        rows = self._rows(root)
        self.assertEqual(pp.EXIT_BREACH, code)
        self.assertIsNone(rows[pp.JUNIT_ROW_P50].find("skipped"))
        self.assertIsNotNone(self._value(rows[pp.JUNIT_ROW_P50]))
        queue_skip = rows[pp.JUNIT_ROW_QUEUE].find("skipped")
        pool_skip = rows[pp.JUNIT_ROW_FREE].find("skipped")
        self.assertIsNotNone(queue_skip)
        self.assertIsNotNone(pool_skip)
        self.assertIn("deck.json", queue_skip.get("message"))
        self.assertIn("boskos.json", pool_skip.get("message"))
        self.assertIsNone(self._value(rows[pp.JUNIT_ROW_QUEUE]))
        self.assertIsNone(self._value(rows[pp.JUNIT_ROW_FREE]))
        self.assertEqual("2", root.get("skipped"))
        self.assertEqual("BREACH (UNKNOWN)",
                         rows[pp.JUNIT_ROW_VERDICT].find("failure").get("message"))

    def test_a_window_with_no_runs_skips_the_setup_rows_rather_than_writing_zero(self):
        code, root = self._junit(from_dir=QUIET_DIR,
                                 as_of=datetime(2020, 1, 1, tzinfo=timezone.utc),
                                 window_days=1)
        rows = self._rows(root)
        self.assertEqual(pp.EXIT_OK, code)
        self.assertIsNone(rows[pp.JUNIT_ROW_VERDICT].find("failure"))
        for name in (pp.JUNIT_ROW_P50, pp.JUNIT_ROW_P95):
            skipped = rows[name].find("skipped")
            self.assertIsNotNone(skipped, name)
            self.assertEqual(pp.JUNIT_NO_RUNS_MESSAGE, skipped.get("message"))
            self.assertIsNone(self._value(rows[name]), name)

    def _payload(self, **kwargs):
        _, out = run(as_json=True, **kwargs)
        return json.loads(out)

    def test_a_sweep_the_deadline_cut_short_skips_the_setup_rows(self):
        """The text report and --json both flag a truncated sweep; the graph
        cannot, so a percentile over fewer days than the row names is not
        written. `--from-dir` never truncates, so the summary is edited."""
        payload = self._payload(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        payload["trend"]["truncated"] = True
        payload["trend"]["window_start"] = "2026-08-27T00:00:00Z"
        rows = self._rows(ET.fromstring(pp.junit_report(payload)))
        for name in (pp.JUNIT_ROW_P50, pp.JUNIT_ROW_P95):
            skipped = rows[name].find("skipped")
            self.assertIsNotNone(skipped, name)
            self.assertIn("2026-08-27T00:00:00Z", skipped.get("message"))
            self.assertIn("ran out of time", skipped.get("message"))
            self.assertIsNone(self._value(rows[name]), name)
        # The other rows do not depend on the sweep's window.
        self.assertEqual("150.0", self._value(rows[pp.JUNIT_ROW_QUEUE]))
        self.assertIsNotNone(rows[pp.JUNIT_ROW_VERDICT].find("failure"))

    def test_a_control_character_in_a_source_error_does_not_break_the_file(self):
        """Skip messages carry kubectl and gcloud stderr verbatim. ElementTree
        writes a C0 byte through, and one escape sequence would drop every row
        on the run that had a failure to report."""
        payload = self._payload(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1)
        payload["queue"]["read"] = False
        payload["queue"]["error"] = "Deck: \x1b[31mgone\x1b[0m\x00"
        payload["cause_text"] = ["bell \x07 here"]
        root = ET.fromstring(pp.junit_report(payload))
        rows = self._rows(root)
        skipped = rows[pp.JUNIT_ROW_QUEUE].find("skipped")
        self.assertEqual("Deck: [31mgone[0m", skipped.get("message"))
        self.assertEqual("bell  here", rows[pp.JUNIT_ROW_VERDICT].find("failure").text)

    def test_the_skip_reason_is_in_the_attribute_and_the_text(self):
        """JUnit readers differ on which one they show."""
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(os.path.join(BREACH_DIR, "prowjobs"), os.path.join(tmp, "prowjobs"))
            _, root = self._junit(from_dir=tmp, as_of=BREACH_AS_OF, window_days=1)
        skipped = self._rows(root)[pp.JUNIT_ROW_FREE].find("skipped")
        self.assertEqual(skipped.get("message"), skipped.text)
        self.assertIn("boskos.json", skipped.text)

    def test_the_verdict_row_names_the_cause_when_a_breach_has_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("prowjobs", "started", "logs", "deck.json"):
                os.symlink(os.path.join(BREACH_DIR, name), os.path.join(tmp, name))
            _, root = self._junit(from_dir=tmp, as_of=BREACH_AS_OF, window_days=1)
        failure = self._rows(root)[pp.JUNIT_ROW_VERDICT].find("failure")
        self.assertEqual(f"{pp.VERDICT_BREACH} ({pp.CAUSE_UNKNOWN})", failure.get("message"))
        self.assertIn("Cause unknown", failure.text)

    def test_without_the_flag_no_file_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(from_dir=QUIET_DIR, as_of=QUIET_AS_OF, window_days=1)
            self.assertEqual([], os.listdir(tmp))

    def test_a_write_that_fails_is_reported_and_leaves_the_exit_code_alone(self):
        """The exit code is the verdict, and the tab still shows pass/fail
        from the job status when the file is missing."""
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "not-a-directory")
            with open(blocker, "w") as fh:
                fh.write("")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, out = run(from_dir=BREACH_DIR, as_of=BREACH_AS_OF, window_days=1,
                                junit_path=os.path.join(blocker, "junit.xml"))
        self.assertEqual(pp.EXIT_BREACH, code)
        self.assertIn("THRESHOLD BREACHED", out)
        self.assertIn("could not write --junit file", err.getvalue())

    def test_the_parent_directory_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "artifacts", "junit_pool_pressure.xml")
            run(from_dir=QUIET_DIR, as_of=QUIET_AS_OF, window_days=1, junit_path=path)
            self.assertEqual("testsuite", ET.parse(path).getroot().tag)


class CommandLine(unittest.TestCase):
    def _main(self, argv):
        """main() with argv patched, returning (exit_code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with unittest.mock.patch.object(sys, "argv", ["pool_pressure.py"] + argv):
                try:
                    code = pp.main()
                except SystemExit as exit_request:
                    code = exit_request.code
        return code, out.getvalue(), err.getvalue()

    def test_a_bad_flag_exits_64_not_2(self):
        """argparse's own 2 would be indistinguishable from "could not measure",
        so a typo in a cron entry would read as a run that completed."""
        code, _, _ = self._main(["--no-such-flag"])
        self.assertEqual(pp.EXIT_USAGE, code)

    def test_an_unparseable_as_of_exits_64(self):
        code, _, err = self._main(["--as-of", "last tuesday"])
        self.assertEqual(pp.EXIT_USAGE, code)
        self.assertIn("--as-of", err)

    def test_a_zero_day_window_exits_64(self):
        code, _, _ = self._main(["--window-days", "0"])
        self.assertEqual(pp.EXIT_USAGE, code)

    def test_zero_workers_exits_64(self):
        """ThreadPoolExecutor rejects it and the sweep catches its own errors,
        so unguarded this exits 2 -- a typo wearing "could not measure"."""
        code, _, err = self._main(["--workers", "0"])
        self.assertEqual(pp.EXIT_USAGE, code)
        self.assertIn("--workers", err)

    def test_a_bare_date_is_accepted_as_of(self):
        code, out, _ = self._main(
            ["--from-dir", QUIET_DIR, "--as-of", "2026-08-28", "--window-days", "1"]
        )
        self.assertEqual(pp.EXIT_OK, code)
        self.assertIn("2026-08-28T00:00:00Z", out)

    def test_an_rfc3339_as_of_is_accepted_too(self):
        code, out, _ = self._main(
            ["--from-dir", QUIET_DIR, "--as-of", "2026-08-28T00:00:00Z", "--window-days", "1"]
        )
        self.assertEqual(pp.EXIT_OK, code)
        self.assertIn("2026-08-28T00:00:00Z", out)

    def test_the_thresholds_are_reachable_from_the_command_line(self):
        code, out, _ = self._main(
            ["--from-dir", BREACH_DIR, "--as-of", "2026-08-27", "--window-days", "1",
             "--p95-threshold-minutes", "600", "--p50-threshold-minutes", "600",
             "--outlier-threshold-minutes", "600"]
        )
        self.assertEqual(pp.EXIT_OK, code)
        self.assertIn("p95 > 600.0", out)

    def test_junit_is_reachable_from_the_command_line(self):
        """The periodic's job line passes it, and the exit code it reads is
        the same with the flag as without."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "junit_pool_pressure.xml")
            argv = ["--from-dir", BREACH_DIR, "--as-of", "2026-08-27", "--window-days", "1"]
            without, _, _ = self._main(argv)
            code, out, _ = self._main(argv + ["--junit", path, "--json"])
            root = ET.parse(path).getroot()
        self.assertEqual(pp.EXIT_BREACH, code)
        self.assertEqual(without, code)
        self.assertEqual("BREACH", json.loads(out)["verdict"])
        self.assertEqual(pp.JUNIT_SUITE_NAME, root.get("name"))


class PublishedInterface(unittest.TestCase):
    """The values other things hardcode.

    Every assertion here is a literal on purpose. `hack/pool_pressure_cron.sh`,
    the oss-test-infra periodic and the runbook each write these out by hand, so
    asserting against the module's own constant would pass through a rename that
    silently breaks all three.
    """

    def test_the_exit_codes(self):
        self.assertEqual(0, pp.EXIT_OK)
        self.assertEqual(1, pp.EXIT_BREACH)
        self.assertEqual(2, pp.EXIT_UNMEASURED)
        self.assertEqual(64, pp.EXIT_USAGE)

    def test_the_verdicts(self):
        self.assertEqual("OK", pp.VERDICT_OK)
        self.assertEqual("BREACH", pp.VERDICT_BREACH)
        self.assertEqual("UNMEASURED", pp.VERDICT_UNMEASURED)

    def test_the_causes(self):
        self.assertEqual("CAPACITY", pp.CAUSE_CAPACITY)
        self.assertEqual("CONCURRENCY_CAP", pp.CAUSE_CONCURRENCY_CAP)
        self.assertEqual("CONTROL_PLANE", pp.CAUSE_CONTROL_PLANE)
        self.assertEqual("UNKNOWN", pp.CAUSE_UNKNOWN)

    def test_the_thresholds_are_the_runbook_numbers(self):
        """Section 10 of the pool runbook: p50 over 15 minutes or p95 over 45
        onboards the next project. Changing either is a policy change."""
        self.assertEqual(15, pp.DEFAULT_P50_THRESHOLD_MINUTES)
        self.assertEqual(45, pp.DEFAULT_P95_THRESHOLD_MINUTES)

    def test_the_junit_row_names(self):
        """TestGrid keys a row's history on its name, so renaming one starts a
        new row and abandons the old one's. The property name is the periodic's
        `testgrid-in-cell-metric` annotation, set in oss-test-infra."""
        self.assertEqual("pool-pressure", pp.JUNIT_SUITE_NAME)
        self.assertEqual("value", pp.JUNIT_METRIC_PROPERTY)
        self.assertEqual("pool pressure within threshold", pp.JUNIT_ROW_VERDICT)
        self.assertEqual("setup p50 minutes", pp.JUNIT_ROW_P50)
        self.assertEqual("setup p95 minutes", pp.JUNIT_ROW_P95)
        self.assertEqual("longest live queue minutes", pp.JUNIT_ROW_QUEUE)
        self.assertEqual("free pool projects", pp.JUNIT_ROW_FREE)
        self.assertEqual(
            ("pool pressure within threshold", "setup p50 minutes", "setup p95 minutes",
             "longest live queue minutes", "free pool projects"),
            pp.JUNIT_ROW_NAMES,
        )


if __name__ == "__main__":
    unittest.main()
