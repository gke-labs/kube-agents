#!/usr/bin/env python3
"""Tests for cron_idle_policy.py: the idle-install hold on model-run cron jobs.

Every case drives :func:`cron_idle_policy.apply_profile` over a scratch
profile with a stubbed signal reader; the shape of a store entry is Hermes'
(``schedule``, ``next_run_at``, ``last_run_at``, ``no_agent``). The signal
reader itself is covered at the end against a stubbed ``sandbox_exec``.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import cron_idle_policy as cip  # noqa: E402

NOW = datetime(2026, 8, 6, 6, 20, 30, tzinfo=timezone.utc)
HUMAN = cip.SIGNAL_SOURCE_HUMAN
CREATED = cip.SIGNAL_SOURCE_CREATED


def iso(when: datetime) -> str:
    return when.isoformat()


def days_ago(n: float, since: datetime = NOW) -> datetime:
    return since - timedelta(days=n)


def due(expr: str, before: datetime = NOW) -> datetime:
    """The expression's newest slot at or before ``before``: a due timestamp
    Hermes itself would have written, on the schedule's own grid."""
    from croniter import croniter

    return croniter(expr, before + timedelta(seconds=1)).get_prev(datetime)


def prompt_job(job_id, expr, next_run_at, last_run_at=None, **extra):
    entry = {
        "id": job_id,
        "enabled": True,
        "schedule": {"kind": "cron", "expr": expr},
        "next_run_at": iso(next_run_at) if isinstance(next_run_at, datetime) else next_run_at,
        "max_turns": 250,
    }
    if last_run_at is not None:
        entry["last_run_at"] = iso(last_run_at) if isinstance(last_run_at, datetime) else last_run_at
    entry.update(extra)
    return entry


def interval_job(job_id, minutes, next_run_at, last_run_at=None, **extra):
    entry = {
        "id": job_id,
        "enabled": True,
        "schedule": {"kind": "interval", "minutes": minutes},
        "next_run_at": iso(next_run_at),
        "max_turns": 250,
    }
    if last_run_at is not None:
        entry["last_run_at"] = iso(last_run_at)
    entry.update(extra)
    return entry


def marked(job: dict) -> dict:
    """The job as ``hermes cron run`` leaves it: ``manual_run_at`` stamped
    with the same string as ``next_run_at``."""
    job[cip.MANUAL_RUN_FIELD] = job["next_run_at"]
    return job


def script_job(job_id, expr, next_run_at):
    return {
        "id": job_id,
        "enabled": True,
        "no_agent": True,
        "script": f"{job_id}.py",
        "schedule": {"kind": "cron", "expr": expr},
        "next_run_at": iso(next_run_at),
    }


class Reader:
    """A signal reader that counts its calls and can be told to fail."""

    def __init__(self, when=None, source=HUMAN, error=None):
        self.when = when
        self.source = source
        self.error = error
        self.calls = 0

    def __call__(self, environ=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.when, self.source


class PolicyCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profile = Path(self._tmp.name) / "profiles" / "platform"
        (self.profile / "cron").mkdir(parents=True)
        self.store = self.profile / "cron" / "jobs.json"
        self.state_path = self.profile / "cron" / cip.STATE_FILE_NAME

    def write(self, *jobs, raw=None):
        self.store.write_text(raw if raw is not None else json.dumps({"jobs": list(jobs)}), encoding="utf-8")

    def jobs(self) -> dict[str, dict]:
        return {j["id"]: j for j in json.loads(self.store.read_text(encoding="utf-8"))["jobs"]}

    def next_run(self, job_id) -> str:
        return self.jobs()[job_id]["next_run_at"]

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def run_policy(self, reader, now=NOW, env=None):
        return cip.apply_profile(self.profile, now, {} if env is None else env, reader)


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(cip.settings({}), (7, 14))

    def test_values_are_read_as_whole_days(self):
        self.assertEqual(cip.settings({cip.THROTTLE_DAYS_ENV: "3", cip.HIBERNATE_DAYS_ENV: " 10 "}), (3, 10))

    def test_zero_throttle_disables_both_stages(self):
        self.assertEqual(cip.settings({cip.THROTTLE_DAYS_ENV: "0", cip.HIBERNATE_DAYS_ENV: "14"}), (0, 0))

    def test_zero_hibernate_disables_that_stage_alone(self):
        self.assertEqual(cip.settings({cip.HIBERNATE_DAYS_ENV: "0"}), (7, 0))

    def test_unparsable_and_negative_mean_the_default(self):
        self.assertEqual(cip.settings({cip.THROTTLE_DAYS_ENV: "week", cip.HIBERNATE_DAYS_ENV: "-2"}), (7, 14))
        self.assertEqual(cip.settings({cip.THROTTLE_DAYS_ENV: "1.5"}), (7, 14))

    def test_stage_thresholds(self):
        self.assertEqual(cip.stage_for(timedelta(days=6, hours=23), 7, 14), cip.STAGE_ACTIVE)
        self.assertEqual(cip.stage_for(timedelta(days=7), 7, 14), cip.STAGE_THROTTLE)
        self.assertEqual(cip.stage_for(timedelta(days=13, hours=23), 7, 14), cip.STAGE_THROTTLE)
        self.assertEqual(cip.stage_for(timedelta(days=14), 7, 14), cip.STAGE_HIBERNATE)
        self.assertEqual(cip.stage_for(timedelta(days=100), 7, 0), cip.STAGE_THROTTLE)
        self.assertEqual(cip.stage_for(timedelta(days=100), 0, 0), cip.STAGE_ACTIVE)


class TimestampTest(unittest.TestCase):
    def test_kubernetes_and_hermes_shapes(self):
        self.assertEqual(cip.parse_timestamp("2026-08-06T06:20:30Z"), NOW)
        self.assertEqual(cip.parse_timestamp("2026-08-06T06:20:30+00:00"), NOW)
        self.assertEqual(cip.parse_timestamp("2026-08-06T08:20:30+02:00"), NOW)

    def test_anything_else_is_none(self):
        for value in (None, "", "soon", 1754461230, {"at": "x"}):
            self.assertIsNone(cip.parse_timestamp(value), value)

    def test_naive_timestamps_are_read_as_local_time(self):
        local = NOW.astimezone().replace(tzinfo=None)
        self.assertEqual(cip.parse_timestamp(local.isoformat()), NOW)


class NextOccurrenceTest(unittest.TestCase):
    def test_inclusive_lands_on_the_horizon_minute(self):
        horizon = NOW + timedelta(days=1)  # 06:20:30 tomorrow
        self.assertEqual(
            cip.next_occurrence("20 6 * * *", horizon, inclusive=True),
            datetime(2026, 8, 7, 6, 20, tzinfo=timezone.utc),
        )

    def test_exclusive_steps_past_the_horizon(self):
        self.assertEqual(
            cip.next_occurrence("20 6 * * *", NOW, inclusive=False),
            datetime(2026, 8, 7, 6, 20, tzinfo=timezone.utc),
        )

    def test_weekly_expression_keeps_its_weekday(self):
        # 2026-08-10 is a Monday.
        self.assertEqual(
            cip.next_occurrence("20 7 * * 1", NOW + timedelta(days=1), inclusive=True),
            datetime(2026, 8, 10, 7, 20, tzinfo=timezone.utc),
        )

    def test_bad_expression_is_none(self):
        self.assertIsNone(cip.next_occurrence("not cron", NOW, inclusive=False))


class ActiveInstallTest(PolicyCase):
    def test_active_install_is_left_alone(self):
        self.write(
            prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)),
            script_job("stall-watch", "*/30 * * * *", NOW),
        )
        before = self.store.read_text()
        reader = Reader(days_ago(1))
        self.assertEqual(self.run_policy(reader), [])
        self.assertEqual(self.store.read_text(), before)
        self.assertEqual(reader.calls, 1)
        self.assertEqual(self.state()["stage"], cip.STAGE_ACTIVE)

    def test_quiet_minute_reads_no_signal(self):
        self.write(
            prompt_job("compliance-audit", "20 6 * * *", NOW + timedelta(days=1), days_ago(1)),
            script_job("stall-watch", "*/30 * * * *", NOW),
        )
        reader = Reader(days_ago(30))
        self.assertEqual(self.run_policy(reader), [])
        self.assertEqual(reader.calls, 0)
        self.assertFalse(self.state_path.exists())

    def test_no_store_is_nothing(self):
        self.assertFalse(self.store.exists())
        self.assertEqual(self.run_policy(Reader(days_ago(30))), [])

    def test_unreadable_store_is_nothing(self):
        self.write(raw="{not json")
        self.assertEqual(self.run_policy(Reader(days_ago(30))), [])


class ThrottleTest(PolicyCase):
    def test_eight_days_idle_spaces_a_recent_job_and_fires_an_old_one(self):
        self.write(
            prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(2)),
            prompt_job("obtainability-audit", "20 6 * * *", NOW, days_ago(8)),
            script_job("stall-watch", "*/30 * * * *", NOW),
        )
        lines = self.run_policy(Reader(days_ago(8)))
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("stage unset -> throttle", lines[0])
        self.assertIn("held compliance-audit until 2026-08-11T06:20:00+00:00 (throttle)", lines[1])
        # First occurrence at or after last_run_at + 7d (06:20:30 on the 11th → 06:20 that day).
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-11T06:20:00+00:00")
        self.assertEqual(self.next_run("obtainability-audit"), iso(NOW))
        self.assertEqual(self.next_run("stall-watch"), iso(NOW))
        self.assertEqual(
            self.state()["held"],
            {"compliance-audit": {"next_run_at": "2026-08-11T06:20:00+00:00", "stage": "throttle"}},
        )

    def test_a_job_that_never_ran_fires(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW))
        self.run_policy(Reader(days_ago(8)))
        self.assertEqual(self.next_run("compliance-audit"), iso(NOW))

    def test_held_timestamp_sits_on_the_expressions_lattice(self):
        # Last ran at 09:41 on a 09:20 schedule: the window ends 09:41 on the
        # 11th, so the hold lands on the 12th at 09:20, not on an off-grid time.
        last = datetime(2026, 8, 4, 9, 41, 12, tzinfo=timezone.utc)
        self.write(prompt_job("stockout-prevention", "20 9 * * *", due("20 9 * * *"), last))
        self.run_policy(Reader(days_ago(8)))
        self.assertEqual(self.next_run("stockout-prevention"), "2026-08-12T09:20:00+00:00")

    def test_a_shorter_window_is_honoured(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)))
        self.run_policy(Reader(days_ago(3)), env={cip.THROTTLE_DAYS_ENV: "2"})
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-07T06:20:00+00:00")

    def test_hold_is_only_written_for_a_due_job(self):
        tomorrow = NOW + timedelta(days=1)
        self.write(
            prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(2)),
            prompt_job("ai-security-audit", "50 8 * * *", tomorrow, days_ago(2)),
        )
        self.run_policy(Reader(days_ago(8)))
        self.assertEqual(self.next_run("ai-security-audit"), iso(tomorrow))
        self.assertEqual(list(self.state()["held"]), ["compliance-audit"])

    def test_an_unscheduled_job_is_left_to_hermes(self):
        self.write(prompt_job("fresh", "20 6 * * *", None))
        before = self.store.read_text()
        self.run_policy(Reader(days_ago(8)))
        self.assertEqual(self.store.read_text(), before)

    def test_an_interval_job_is_held_by_its_period(self):
        # `cronjob(action='create', schedule='every 2h')` stores an interval;
        # Hermes writes next_run_at as last_run_at plus the period.
        last = days_ago(2)
        self.write(interval_job("two-hourly", 120, last + timedelta(hours=2), last))
        lines = self.run_policy(Reader(days_ago(8)))
        self.assertIn("held two-hourly until 2026-08-11T06:20:30+00:00 (throttle)", "\n".join(lines))
        self.assertEqual(self.next_run("two-hourly"), iso(last + timedelta(days=7)))

    def test_a_run_asked_for_by_hand_is_not_held(self):
        # `hermes cron run` marks a job due by stamping one timestamp into
        # next_run_at and manual_run_at; that run is somebody's request.
        asked = NOW - timedelta(minutes=3, seconds=17)
        self.write(
            marked(prompt_job("compliance-audit", "20 6 * * *", asked, days_ago(2))),
            prompt_job("ai-security-audit", "50 8 * * *", due("50 8 * * *"), days_ago(2)),
        )
        lines = self.run_policy(Reader(days_ago(8)))
        self.assertEqual(self.next_run("compliance-audit"), iso(asked))
        self.assertNotEqual(self.next_run("ai-security-audit"), iso(due("50 8 * * *")))
        self.assertEqual([line for line in lines if "compliance-audit" in line], [])

    def test_a_hand_mark_inside_the_slot_minute_is_not_held(self):
        # A mark written at 06:20:45 on `20 6 * * *` sits inside a slot minute,
        # on a job that already ran this morning: still a request.
        ran = NOW - timedelta(seconds=90)
        asked = NOW + timedelta(seconds=15)
        self.write(marked(prompt_job("compliance-audit", "20 6 * * *", asked, ran)))
        for signal in (days_ago(8), days_ago(15)):
            self.state_path.unlink(missing_ok=True)  # a fresh reading, not the cached one
            lines = self.run_policy(Reader(signal), now=asked + timedelta(seconds=5))
            self.assertEqual([line for line in lines if "held" in line], [])
            self.assertEqual(self.next_run("compliance-audit"), iso(asked))

    def test_a_hand_run_of_an_interval_job_is_not_held(self):
        last = days_ago(2)
        self.write(marked(interval_job("two-hourly", 120, NOW - timedelta(minutes=1), last)))
        self.run_policy(Reader(days_ago(15)))
        self.assertEqual(self.next_run("two-hourly"), iso(NOW - timedelta(minutes=1)))

    def test_a_hand_run_of_an_interval_job_that_never_ran_is_not_held(self):
        # No last_run_at to be on-period with: the mark alone says so, under
        # hibernate as under throttle.
        self.write(marked(interval_job("two-hourly", 120, NOW - timedelta(minutes=1))))
        for signal in (days_ago(8), days_ago(15)):
            self.state_path.unlink(missing_ok=True)  # a fresh reading, not the cached one
            lines = self.run_policy(Reader(signal))
            self.assertEqual([line for line in lines if "held" in line], [])
            self.assertEqual(self.next_run("two-hourly"), iso(NOW - timedelta(minutes=1)))

    def test_a_stale_mark_hermes_advanced_past_is_not_a_request(self):
        # manual_run_at left from an earlier hand run, next_run_at since moved
        # on by Hermes: the pair differs, so the slot is held like any other.
        job = prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(2))
        job[cip.MANUAL_RUN_FIELD] = iso(days_ago(2, NOW))
        self.write(job)
        self.run_policy(Reader(days_ago(15)))
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-07T06:20:00+00:00")

    def test_a_period_no_timedelta_can_hold_costs_no_other_job_its_hold(self):
        # A `minutes` the JSON grammar admits but a timedelta cannot hold
        # (`1e400` reads as inf; an integer wider than a float; a finite value
        # past the timedelta range) is no period: the entry is left to Hermes
        # and the rest of the store is still held on the same tick.
        audit = prompt_job("compliance-audit", "20 6 * * *", due("20 6 * * *"), days_ago(2))
        store = json.dumps({"jobs": [interval_job("wide", 0, NOW - timedelta(minutes=1), days_ago(2)), audit]})
        for minutes in ("1e400", str(10**400), "2e12"):
            self.write(raw=store.replace('"minutes": 0', f'"minutes": {minutes}'))
            lines = self.run_policy(Reader(days_ago(8)))
            self.assertIn("held compliance-audit", "\n".join(lines), minutes)
            self.assertEqual(self.next_run("wide"), iso(NOW - timedelta(minutes=1)), minutes)
            self.assertNotIn("wide", self.state()["held"], minutes)

    def test_a_period_below_a_microsecond_is_no_period_and_costs_no_release(self):
        # `1e-9` passes a `> 0` check on the number, but the timedelta it makes
        # is zero: held by it, the job's release divides by that period and
        # aborts the store's release. No period, so never held, and the rest
        # of the store releases.
        self.write(
            interval_job("sub-micro", 1e-9, NOW - timedelta(minutes=1), days_ago(2)),
            interval_job("two-hourly", 120, NOW, NOW - timedelta(hours=2)),
        )
        lines = self.run_policy(Reader(days_ago(15)))
        self.assertEqual([line for line in lines if "held" in line], ["platform: held two-hourly until 2026-08-07T06:20:30+00:00 (hibernate)"])
        self.assertEqual(self.next_run("sub-micro"), iso(NOW - timedelta(minutes=1)))
        later = NOW + timedelta(minutes=50)
        lines = self.run_policy(Reader(later), now=later)
        self.assertIn("released two-hourly", "\n".join(lines))
        self.assertEqual(self.state()["held"], {})

    def test_a_period_past_the_calendars_end_releases_without_a_next_slot(self):
        # The release adds the period to the last run; a period that lands past
        # year 9999 has no next slot, so the job keeps the hold's timestamp and
        # the release of every other job goes through.
        self.write(interval_job("aeon", 1e10, NOW), interval_job("two-hourly", 120, NOW, NOW - timedelta(hours=2)))
        self.run_policy(Reader(days_ago(15)))
        self.assertEqual(self.next_run("aeon"), iso(NOW + timedelta(days=1)))
        later = NOW + timedelta(minutes=50)
        lines = self.run_policy(Reader(later), now=later)
        self.assertIn("released two-hourly", "\n".join(lines))
        self.assertEqual(self.next_run("aeon"), iso(NOW + timedelta(days=1)))
        self.assertEqual(self.state()["held"], {})

    def test_disabled_job_is_not_held(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(2), enabled=False))
        self.run_policy(Reader(days_ago(8)))
        self.assertEqual(self.next_run("compliance-audit"), iso(NOW))


class HibernateTest(PolicyCase):
    def roster(self):
        return (
            prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)),
            prompt_job("security-patch-orchestrator", "20 7 * * 1", due("20 7 * * 1"), days_ago(20)),
            prompt_job("never-ran", "0 8 * * *", due("0 8 * * *")),
            script_job("stall-watch", "*/30 * * * *", NOW),
            script_job("github-repo-watcher", "*/10 * * * *", NOW),
        )

    def test_fifteen_days_idle_holds_every_prompt_job_and_no_script(self):
        self.write(*self.roster())
        lines = self.run_policy(Reader(days_ago(15), CREATED))
        self.assertIn("stage unset -> hibernate (idle 15d by creationTimestamp", lines[0])
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-07T06:20:00+00:00")
        # 2026-08-10 is the first Monday at or after tomorrow.
        self.assertEqual(self.next_run("security-patch-orchestrator"), "2026-08-10T07:20:00+00:00")
        self.assertEqual(self.next_run("never-ran"), "2026-08-07T08:00:00+00:00")
        self.assertEqual(self.next_run("stall-watch"), iso(NOW))
        self.assertEqual(self.next_run("github-repo-watcher"), iso(NOW))
        self.assertEqual(
            sorted(self.state()["held"]), ["compliance-audit", "never-ran", "security-patch-orchestrator"]
        )
        self.assertEqual(len(lines), 4, lines)

    def test_a_hibernating_job_is_held_again_each_day(self):
        self.write(*self.roster())
        reader = Reader(days_ago(15), CREATED)
        self.run_policy(reader)
        day_two = NOW + timedelta(days=1)
        lines = self.run_policy(reader, now=day_two)
        self.assertEqual([line for line in lines if "stage" in line], [])
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-08T06:20:00+00:00")
        # Not due until 08:00 that day, so not touched on this tick.
        self.assertEqual(self.next_run("never-ran"), "2026-08-07T08:00:00+00:00")
        self.assertEqual(self.next_run("security-patch-orchestrator"), "2026-08-10T07:20:00+00:00")

    def test_an_interval_job_hibernates_and_releases_on_its_period(self):
        last = NOW - timedelta(hours=2)
        self.write(interval_job("two-hourly", 120, NOW, last))
        self.run_policy(Reader(days_ago(15)))
        self.assertEqual(self.next_run("two-hourly"), iso(NOW + timedelta(days=1)))
        later = NOW + timedelta(minutes=50)
        lines = self.run_policy(Reader(later), now=later)
        self.assertIn("released two-hourly", "\n".join(lines))
        # The first multiple of the period past now, counted from the last run.
        self.assertEqual(self.next_run("two-hourly"), iso(last + timedelta(hours=4)))

    def test_a_hibernating_interval_job_is_held_again_each_day(self):
        # Its hold sits a day on, off its period, and carries no manual mark,
        # so day two holds it again instead of firing it.
        last = NOW - timedelta(hours=2)
        self.write(interval_job("two-hourly", 120, NOW, last))
        reader = Reader(days_ago(15))
        self.run_policy(reader)
        day_two = NOW + timedelta(days=1)
        lines = self.run_policy(reader, now=day_two)
        self.assertEqual(lines, [f"platform: held two-hourly until {iso(day_two + timedelta(days=1))} (hibernate)"])
        self.assertEqual(self.next_run("two-hourly"), iso(day_two + timedelta(days=1)))
        self.assertEqual(self.state()["held"]["two-hourly"]["next_run_at"], iso(day_two + timedelta(days=1)))
        # A hand-marked run on the same job is still a request.
        jobs = json.loads(self.store.read_text())
        jobs["jobs"][0]["next_run_at"] = iso(day_two + timedelta(hours=1))
        marked(jobs["jobs"][0])
        self.store.write_text(json.dumps(jobs))
        self.assertEqual(self.run_policy(reader, now=day_two + timedelta(hours=1)), [])
        self.assertEqual(self.next_run("two-hourly"), iso(day_two + timedelta(hours=1)))

    def test_an_overdue_job_after_a_restart_is_held_a_day_from_now(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", days_ago(3), days_ago(4)))
        late = NOW + timedelta(hours=5)
        self.run_policy(Reader(days_ago(15)), now=late)
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-08T06:20:00+00:00")


class WakeTest(PolicyCase):
    def hibernate(self):
        self.write(
            prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)),
            prompt_job("obtainability-audit", "50 6 * * *", due("50 6 * * *"), days_ago(1)),
            script_job("stall-watch", "*/30 * * * *", NOW),
        )
        self.run_policy(Reader(days_ago(15)))
        self.assertEqual(self.state()["stage"], cip.STAGE_HIBERNATE)

    def test_a_fresh_signal_rewinds_held_jobs_to_their_next_occurrence(self):
        self.hibernate()
        later = NOW + timedelta(minutes=6)
        lines = self.run_policy(Reader(NOW + timedelta(minutes=1)), now=later)
        self.assertIn("stage hibernate -> active", lines[0])
        self.assertIn("released compliance-audit; next run 2026-08-07T06:20:00+00:00", lines[1])
        self.assertIn("released obtainability-audit; next run 2026-08-06T06:50:00+00:00", lines[2])
        # Not `now`: the next natural slot, so a returning operator does not get
        # every audit at once.
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-07T06:20:00+00:00")
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-06T06:50:00+00:00")
        self.assertEqual(self.state()["held"], {})

    def test_a_held_slot_that_has_arrived_is_left_for_hermes_to_fire(self):
        # The stage drops on the first tick at or after the held slot (a
        # message read in the minutes before it, or a ticker outage across
        # it): the slot is the job's own occurrence, so it stays and Hermes
        # fires it, rather than moving a day past the run it was due for.
        self.hibernate()
        held_at = self.next_run("compliance-audit")  # 2026-08-07T06:20:00+00:00
        later = NOW + timedelta(days=1)  # 06:20:30 the next day
        lines = self.run_policy(Reader(later - timedelta(minutes=3)), now=later)
        self.assertIn(f"released compliance-audit; due at its held slot {held_at}", lines[1])
        self.assertEqual(self.next_run("compliance-audit"), held_at)
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-07T06:50:00+00:00")
        self.assertEqual(self.state()["held"], {})

    def test_a_job_hermes_advanced_meanwhile_is_left_alone(self):
        self.hibernate()
        jobs = json.loads(self.store.read_text())
        for job in jobs["jobs"]:
            if job["id"] == "compliance-audit":
                job["next_run_at"] = "2026-08-20T06:20:00+00:00"  # someone ran it by hand
        self.store.write_text(json.dumps(jobs))
        self.run_policy(Reader(NOW + timedelta(minutes=1)), now=NOW + timedelta(minutes=6))
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-20T06:20:00+00:00")
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-06T06:50:00+00:00")
        self.assertEqual(self.state()["held"], {})

    def test_hibernate_to_throttle_releases_and_lets_the_window_decide(self):
        self.hibernate()
        env = {cip.HIBERNATE_DAYS_ENV: "30"}
        lines = self.run_policy(Reader(days_ago(15)), now=NOW + timedelta(minutes=6), env=env)
        self.assertIn("stage hibernate -> throttle", lines[0])
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-07T06:20:00+00:00")
        self.assertEqual(self.state()["held"], {})

    def test_a_record_for_a_job_that_left_the_roster_is_dropped(self):
        self.hibernate()
        jobs = json.loads(self.store.read_text())
        jobs["jobs"] = [j for j in jobs["jobs"] if j["id"] != "obtainability-audit"]
        self.store.write_text(json.dumps(jobs))
        # Pruned on the next tick that opens the store: the day after, when the
        # remaining audit comes due again.
        self.run_policy(Reader(days_ago(15)), now=NOW + timedelta(days=1))
        self.assertEqual(list(self.state()["held"]), ["compliance-audit"])


class DisabledTest(PolicyCase):
    def test_zero_throttle_days_disables_both_stages(self):
        self.write(
            prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)),
            script_job("stall-watch", "*/30 * * * *", NOW),
        )
        before = self.store.read_text()
        reader = Reader(days_ago(400), CREATED)
        lines = self.run_policy(reader, env={cip.THROTTLE_DAYS_ENV: "0"})
        self.assertEqual(self.store.read_text(), before)
        self.assertEqual(reader.calls, 0)
        self.assertEqual([line for line in lines if "held" in line], [])

    def test_disabling_releases_what_was_held(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)))
        self.run_policy(Reader(days_ago(15)))
        self.assertEqual(self.state()["held"].keys(), {"compliance-audit"})
        lines = self.run_policy(Reader(days_ago(15)), now=NOW + timedelta(minutes=1), env={cip.THROTTLE_DAYS_ENV: "0"})
        self.assertIn("released compliance-audit", "\n".join(lines))
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-07T06:20:00+00:00")
        self.assertEqual(self.state()["held"], {})


class ScheduleZoneTest(PolicyCase):
    def test_defaults_to_utc(self):
        self.assertEqual(cip.schedule_zone(self.profile, {}), timezone.utc)

    def test_variable_then_profile_config_then_utc(self):
        (self.profile / "config.yaml").write_text("timezone: Europe/Brussels\n", encoding="utf-8")
        self.assertEqual(str(cip.schedule_zone(self.profile, {})), "Europe/Brussels")
        self.assertEqual(str(cip.schedule_zone(self.profile, {cip.TIMEZONE_ENV: "Asia/Tokyo"})), "Asia/Tokyo")
        self.assertEqual(cip.schedule_zone(self.profile, {cip.TIMEZONE_ENV: "Mars/Olympus"}), timezone.utc)

    def test_a_hold_lands_on_the_zones_slot(self):
        # 06:20 Brussels in August is 04:20 UTC; the due slot and the hold both
        # sit on that clock, and the stored instant stays UTC.
        (self.profile / "config.yaml").write_text("timezone: Europe/Brussels\n", encoding="utf-8")
        local_due = datetime(2026, 8, 6, 4, 20, tzinfo=timezone.utc)
        self.write(prompt_job("compliance-audit", "20 6 * * *", local_due, days_ago(1)))
        self.run_policy(Reader(days_ago(15)), now=local_due + timedelta(seconds=30))
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-07T04:20:00+00:00")

    def test_a_hand_mark_is_read_the_same_on_any_clock(self):
        # The mark, not the timestamp's position on the zone's grid, is what
        # says a run was asked for.
        (self.profile / "config.yaml").write_text("timezone: Europe/Brussels\n", encoding="utf-8")
        self.write(marked(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1))))
        self.run_policy(Reader(days_ago(15)))
        self.assertEqual(self.next_run("compliance-audit"), iso(NOW))


class CroniterMissingTest(PolicyCase):
    def test_says_so_once_and_holds_nothing(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)))
        self._patch(cip, "croniter_available", lambda: False)
        lines = self.run_policy(Reader(days_ago(15)))
        self.assertEqual([line for line in lines if "cannot import croniter" in line][0],
                         "platform: cron idle policy cannot import croniter; nothing held")
        self.assertEqual(self.next_run("compliance-audit"), iso(NOW))
        again = self.run_policy(Reader(days_ago(15)), now=NOW + timedelta(minutes=1))
        self.assertEqual([line for line in again if "croniter" in line], [])
        self.assertTrue(self.state()["croniter_missing"])

    def _patch(self, obj, attr, value):
        original = getattr(obj, attr)
        setattr(obj, attr, value)
        self.addCleanup(setattr, obj, attr, original)


class SignalCacheTest(PolicyCase):
    def setUp(self):
        super().setUp()
        self.write(
            prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)),
            prompt_job("obtainability-audit", "50 6 * * *", NOW + timedelta(minutes=30), days_ago(1)),
        )

    def test_the_reading_is_cached_for_five_minutes(self):
        reader = Reader(days_ago(15))
        self.run_policy(reader)
        self.run_policy(reader, now=NOW + timedelta(minutes=4))
        self.assertEqual(reader.calls, 1)
        self.run_policy(reader, now=NOW + timedelta(minutes=5))
        self.assertEqual(reader.calls, 2)

    def test_a_failing_read_keeps_the_last_reading_for_a_day(self):
        good = Reader(days_ago(15))
        self.run_policy(good)
        failing = Reader(error=RuntimeError("kubectl exited 1: forbidden"))
        later = NOW + timedelta(minutes=30)  # the second audit's due minute
        lines = self.run_policy(failing, now=later)
        self.assertEqual(failing.calls, 1)
        self.assertEqual(len([line for line in lines if "cannot read the PlatformAgent" in line]), 1)
        # Still hibernating on the cached reading: the second audit, due now,
        # is held.
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-07T06:50:00+00:00")
        # Said once, not every tick.
        again = self.run_policy(failing, now=later + timedelta(minutes=6))
        self.assertEqual([line for line in again if "cannot read" in line], [])

    def test_a_counted_but_absent_field_replaces_the_cached_reading(self):
        # The upgrade and rollback paths: the install hibernated on a reading,
        # a person wrote, and the CR now counts the message without carrying
        # the field. That is a reading that says "unknown", not a failed read,
        # so the cached one goes at once: nothing new is held from this tick,
        # and the hold already written stands at its pushed slot.
        self.run_policy(Reader(days_ago(15)))
        held_at = self.next_run("compliance-audit")
        unknown = Reader(error=cip.SignalUnknown("lastHumanActiveTime is absent while chatMessagesInboundTotal counts 1"))
        later = NOW + timedelta(minutes=30)  # the second audit's due minute
        lines = self.run_policy(unknown, now=later)
        self.assertEqual(len(lines), 1)
        self.assertIn("cannot read the PlatformAgent (lastHumanActiveTime is absent", lines[0])
        self.assertIn("nothing held", lines[0])
        self.assertEqual(self.next_run("obtainability-audit"), iso(later))
        self.assertEqual(self.next_run("compliance-audit"), held_at)
        self.assertNotIn("at", self.state()["signal"])
        # A failed read after it has no reading left to stand on either.
        failing = Reader(error=RuntimeError("kubectl exited 1: forbidden"))
        self.run_policy(failing, now=later + timedelta(minutes=6))
        self.assertEqual(self.next_run("obtainability-audit"), iso(later))

    def test_a_day_without_a_reading_fails_open(self):
        self.run_policy(Reader(days_ago(15)))
        held_at = self.next_run("compliance-audit")
        failing = Reader(error=RuntimeError("kubectl exited 1: forbidden"))
        self.run_policy(failing, now=NOW + timedelta(days=1, minutes=1))
        # Nothing new held, nothing rewound: the hold already written stands
        # until its own time, and the stage stays what it was.
        self.assertEqual(self.next_run("obtainability-audit"), iso(NOW + timedelta(minutes=30)))
        self.assertEqual(self.next_run("compliance-audit"), held_at)
        self.assertEqual(self.state()["stage"], cip.STAGE_HIBERNATE)

    def test_a_failing_first_read_holds_nothing(self):
        failing = Reader(error=RuntimeError("no PlatformAgent in namespace kubeagents-system"))
        lines = self.run_policy(failing)
        self.assertEqual(self.next_run("compliance-audit"), iso(NOW))
        self.assertIn("no PlatformAgent in namespace kubeagents-system", lines[0])
        self.assertIsNone(self.state()["stage"])


class StoreLockTest(PolicyCase):
    def test_a_lock_that_falls_open_skips_the_write(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)))

        class FallenOpen:
            @staticmethod
            def cron_jobs_lock(cron_dir, timeout=None, *, warn=None):
                import contextlib

                warn(f"WARN: timed out after 15.0s waiting for {cron_dir / '.jobs.lock'}; proceeding without lock")
                return contextlib.nullcontext()

        original = cip._platform_module
        cip._platform_module = lambda name: FallenOpen() if name == cip.LOCK_MODULE else original(name)
        self.addCleanup(setattr, cip, "_platform_module", original)
        lines = self.run_policy(Reader(days_ago(15)))
        self.assertTrue(any("the store lock is held" in line for line in lines), lines)
        self.assertEqual(self.next_run("compliance-audit"), iso(NOW))
        self.assertEqual(self.state()["held"], {})

    def test_the_write_happens_under_hermes_lock(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)))
        taken = []
        scaffold = cip._platform_module(cip.LOCK_MODULE)
        real = scaffold.cron_jobs_lock

        def recording(cron_dir, timeout=None, *, warn=None):
            taken.append(cron_dir)
            return real(cron_dir, timeout, warn=warn)

        scaffold.cron_jobs_lock = recording
        self.addCleanup(setattr, scaffold, "cron_jobs_lock", real)
        self.run_policy(Reader(days_ago(15)))
        self.assertEqual(taken, [self.profile / "cron"])
        self.assertTrue((self.profile / "cron" / ".jobs.lock").exists())


class WriteOrderTest(PolicyCase):
    """The record reaches disk before the store does, so a crash between the
    two never leaves a hold no wake can rewind."""

    def writes(self, fail_on=None):
        order = []
        real = cip._write_json

        def recording(path, payload):
            order.append(path.name)
            if fail_on is not None and path.name == fail_on:
                raise OSError(28, "No space left on device")
            real(path, payload)

        cip._write_json = recording
        self.restore_writes = lambda: setattr(cip, "_write_json", real)
        self.addCleanup(self.restore_writes)
        return order

    def test_the_record_is_saved_before_the_store(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)))
        order = self.writes()
        self.run_policy(Reader(days_ago(15)))
        self.assertEqual(order, [cip.STATE_FILE_NAME, cip.JOBS_FILE_NAME])
        self.assertEqual(self.state()["held"]["compliance-audit"]["next_run_at"], self.next_run("compliance-audit"))

    def test_a_crash_before_the_store_write_leaves_a_record_the_next_tick_overwrites(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)))
        self.writes(fail_on=cip.JOBS_FILE_NAME)
        with self.assertRaises(OSError):
            self.run_policy(Reader(days_ago(15)))
        self.assertEqual(self.next_run("compliance-audit"), iso(NOW))
        self.assertEqual(self.state()["held"]["compliance-audit"]["next_run_at"], "2026-08-07T06:20:00+00:00")
        self.restore_writes()
        lines = self.run_policy(Reader(days_ago(15)), now=NOW + timedelta(seconds=10))
        self.assertEqual(lines, ["platform: held compliance-audit until 2026-08-07T06:20:00+00:00 (hibernate)"])
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-07T06:20:00+00:00")

    def test_a_crash_before_the_store_write_on_release_keeps_the_record(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)))
        self.run_policy(Reader(days_ago(15)))
        held = self.next_run("compliance-audit")
        self.writes(fail_on=cip.JOBS_FILE_NAME)
        with self.assertRaises(OSError):
            self.run_policy(Reader(NOW + timedelta(minutes=1)), now=NOW + timedelta(minutes=6))
        self.assertEqual(self.next_run("compliance-audit"), held)
        self.assertEqual(self.state()["held"]["compliance-audit"]["next_run_at"], held)
        self.restore_writes()
        lines = self.run_policy(Reader(NOW + timedelta(minutes=1)), now=NOW + timedelta(minutes=12))
        self.assertIn("released compliance-audit", "\n".join(lines))
        self.assertEqual(self.state()["held"], {})

    def test_a_state_that_cannot_be_saved_holds_nothing(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)))
        self.writes(fail_on=cip.STATE_FILE_NAME)
        lines = self.run_policy(Reader(days_ago(15)))
        self.assertEqual(
            [line for line in lines if "could not save its state" in line],
            ["platform: cron idle policy could not save its state ([Errno 28] No space left on device); nothing held"],
        )
        self.assertEqual([line for line in lines if "held compliance-audit" in line], [])
        self.assertEqual(self.next_run("compliance-audit"), iso(NOW))
        self.assertFalse(self.state_path.exists())


class ApplyTest(PolicyCase):
    def test_apply_visits_every_profile_with_a_store_and_survives_a_fault(self):
        home = Path(self._tmp.name)
        other = home / "profiles" / "cluster-a"
        (other / "cron" / "output").mkdir(parents=True)  # no roster: skipped
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)))
        original = cip.read_signal
        cip.read_signal = Reader(days_ago(15))
        self.addCleanup(setattr, cip, "read_signal", original)
        lines = cip.apply(home, NOW, {})
        self.assertEqual(len([line for line in lines if line.startswith("platform: held")]), 1)

        boom = cip.apply_profile

        def failing(profile, now, environ=None, reader=None):
            if profile.name == "platform":
                raise ValueError("torn")
            return boom(profile, now, environ, reader)

        cip.apply_profile = failing
        self.addCleanup(setattr, cip, "apply_profile", boom)
        self.assertEqual(cip.apply(home, NOW, {}), ["platform: cron idle policy failed (ValueError: torn)"])

    def test_no_profiles_dir_is_nothing(self):
        self.assertEqual(cip.apply(Path(self._tmp.name) / "nope", NOW, {}), [])

    def test_one_reading_serves_every_profile_in_a_tick(self):
        home = Path(self._tmp.name)
        other = home / "profiles" / "cluster-a"
        (other / "cron").mkdir(parents=True)
        for store in (self.store, other / "cron" / "jobs.json"):
            store.write_text(json.dumps({"jobs": [prompt_job("audit", "20 6 * * *", NOW, days_ago(1))]}))
        reader = Reader(days_ago(15))
        original = cip.read_signal
        cip.read_signal = reader
        self.addCleanup(setattr, cip, "read_signal", original)
        lines = cip.apply(home, NOW, {})
        self.assertEqual(reader.calls, 1)
        self.assertEqual(sorted(line.split(":")[0] for line in lines if "held audit" in line), ["cluster-a", "platform"])
        # A failure is shared the same way: one kubectl, one timeout, both told.
        failing = Reader(error=RuntimeError("kubectl exited 1: forbidden"))
        cip.read_signal = failing
        lines = cip.apply(home, NOW + timedelta(minutes=6), {})
        self.assertEqual(failing.calls, 1)
        self.assertEqual(len([line for line in lines if "cannot read the PlatformAgent" in line]), 2)


class ReadSignalTest(unittest.TestCase):
    """The reader against a stubbed sandbox: which field wins, and what fails."""

    def stub(self, stdout="", returncode=0, stderr="", raises=None):
        calls = []

        class Sandbox:
            @staticmethod
            def run(argv, **kwargs):
                calls.append((argv, kwargs))
                if raises is not None:
                    raise raises
                return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        original = cip._platform_module
        cip._platform_module = lambda name: Sandbox() if name == cip.SANDBOX_MODULE else original(name)
        self.addCleanup(setattr, cip, "_platform_module", original)
        return calls

    @staticmethod
    def listing(status=None, created="2026-07-01T00:00:00Z"):
        agent = {"metadata": {"name": "platform-agent", "creationTimestamp": created}}
        if status is not None:
            agent["status"] = status
        return json.dumps({"items": [agent]})

    def test_last_human_active_time_wins(self):
        calls = self.stub(self.listing({"usage": {"lastHumanActiveTime": "2026-08-01T12:00:00Z"}}))
        self.assertEqual(
            cip.read_signal({}),
            (datetime(2026, 8, 1, 12, tzinfo=timezone.utc), HUMAN),
        )
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["kubectl", "get", cip.PLATFORM_AGENT_RESOURCE, "-n", "kubeagents-system", "-o", "json"])
        self.assertEqual(kwargs["timeout"], cip.KUBECTL_TIMEOUT_SECONDS)

    def test_absent_field_falls_back_to_creation_time(self):
        self.stub(self.listing({"usage": {"lastActiveTime": "2026-08-06T06:00:00Z"}}))
        self.assertEqual(cip.read_signal({}), (datetime(2026, 7, 1, tzinfo=timezone.utc), CREATED))
        self.stub(self.listing())
        self.assertEqual(cip.read_signal({}), (datetime(2026, 7, 1, tzinfo=timezone.utc), CREATED))
        self.stub(self.listing({"usage": {"chatMessagesInboundTotal": 0}}))
        self.assertEqual(cip.read_signal({}), (datetime(2026, 7, 1, tzinfo=timezone.utc), CREATED))

    def test_absent_field_with_counted_messages_is_unknown(self):
        # An operator upgraded, or rolled back and returned, since the last
        # message: the field is missing, not never written, so no creation-time
        # fallback and nothing held until the next message.
        self.stub(self.listing({"usage": {"chatMessagesInboundTotal": 14, "lastActiveTime": "2026-08-06T06:00:00Z"}}))
        with self.assertRaisesRegex(cip.SignalUnknown, "lastHumanActiveTime is absent while chatMessagesInboundTotal counts 14"):
            cip.read_signal({})

    def test_namespace_comes_from_the_environment_it_is_given(self):
        # The reader reads the environment it is handed, not the process's, so
        # the test holds in a shell or a pod that exports the name itself.
        calls = self.stub(self.listing())
        cip.read_signal({cip.NAMESPACE_ENV: "agents"})
        self.assertEqual(calls[0][0][4], "agents")
        cip.read_signal({cip.NAMESPACE_ENV: "  "})
        self.assertEqual(calls[1][0][4], cip.DEFAULT_NAMESPACE)

    def test_apply_hands_the_reader_its_environment(self):
        calls = self.stub(self.listing())
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "profiles" / "platform" / "cron").mkdir(parents=True)
            (home / "profiles" / "platform" / "cron" / "jobs.json").write_text(
                json.dumps({"jobs": [prompt_job("audit", "20 6 * * *", NOW, days_ago(1))]})
            )
            cip.apply(home, NOW, {cip.NAMESPACE_ENV: "agents"})
        self.assertEqual(calls[0][0][4], "agents")

    def test_missing_cr_raises(self):
        self.stub(json.dumps({"items": []}))
        with self.assertRaisesRegex(RuntimeError, "no PlatformAgent"):
            cip.read_signal({})

    def test_failed_kubectl_raises_with_its_stderr(self):
        self.stub(returncode=1, stderr="Error from server (Forbidden): platformagents is forbidden")
        with self.assertRaisesRegex(RuntimeError, "exited 1: Error from server"):
            cip.read_signal({})

    def test_unreachable_sandbox_propagates(self):
        self.stub(raises=subprocess.TimeoutExpired(["kubectl"], 30))
        with self.assertRaises(subprocess.TimeoutExpired):
            cip.read_signal({})

    def test_neither_field_raises(self):
        self.stub(json.dumps({"items": [{"metadata": {"name": "x"}}]}))
        with self.assertRaisesRegex(RuntimeError, "neither"):
            cip.read_signal({})


if __name__ == "__main__":
    unittest.main()
