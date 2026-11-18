#!/usr/bin/env python3
"""Tests for cron_catch_up_policy.py: what a paused install's roster does on resume.

Every case drives :func:`cron_catch_up_policy.apply_profile` over a scratch
profile whose store entries have Hermes' shape (``schedule``, ``next_run_at``,
``last_run_at``, ``no_agent``) and whose ledger is a copy of Hermes' migrated
``executions`` table, so the rows the policy writes are checked against the
real schema.
"""

import contextlib
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))
PATCHES_DIR = Path(__file__).resolve().parents[3] / "deploy" / "docker" / "patches"
sys.path.insert(0, str(PATCHES_DIR))

import cron_catch_up_policy as ccp  # noqa: E402
import cron_idle_policy as cip  # noqa: E402
import cron_skip_ledger  # noqa: E402  the image's ledger migration, run over the fixture

# A Wednesday. The platform roster's weekly audits run on Mondays.
NOW = datetime(2026, 8, 5, 9, 25, 30, tzinfo=timezone.utc)

# Hermes' executions table as its own _initialize_schema creates it (v2026.9.14),
# before the image's migration widens it: no 'skipped', no skip_reason.
UPSTREAM_LEDGER_DDL = """
CREATE TABLE executions (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  source TEXT NOT NULL,
  process_id TEXT NOT NULL,
  pid INTEGER NOT NULL,
  process_started_at INTEGER,
  status TEXT NOT NULL CHECK(status IN ('claimed','running','completed','failed','unknown')),
  handoff_pending INTEGER NOT NULL DEFAULT 0,
  handoff_started_at REAL,
  claimed_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT
)
"""


def iso(when: datetime) -> str:
    return when.isoformat()


def slot(expr: str, before: datetime = NOW) -> datetime:
    """The expression's newest slot at or before ``before``: the timestamp
    Hermes itself would have left in ``next_run_at``."""
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
        entry["last_run_at"] = iso(last_run_at)
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


def script_job(job_id, expr, next_run_at):
    return {
        "id": job_id,
        "enabled": True,
        "no_agent": True,
        "script": f"{job_id}.py",
        "schedule": {"kind": "cron", "expr": expr},
        "next_run_at": iso(next_run_at),
    }


def marked(job: dict) -> dict:
    job[cip.MANUAL_RUN_FIELD] = job["next_run_at"]
    return job


class PolicyCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profile = Path(self._tmp.name) / "profiles" / "platform"
        (self.profile / "cron").mkdir(parents=True)
        self.store = self.profile / "cron" / "jobs.json"
        self.state_path = self.profile / "cron" / ccp.STATE_FILE_NAME
        self.ledger_path = self.profile / "cron" / ccp.LEDGER_FILE_NAME
        self.make_ledger(migrated=True)

    def make_ledger(self, migrated):
        """Hermes' table, plus the two columns its add_column_if_missing adds,
        then the image's own migration over it when ``migrated``."""
        self.ledger_path.unlink(missing_ok=True)
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn:
            conn.execute(UPSTREAM_LEDGER_DDL)
            conn.execute("ALTER TABLE executions ADD COLUMN delivery_outcome TEXT")
            conn.execute("ALTER TABLE executions ADD COLUMN scheduled_instant TEXT")
            conn.commit()
            if migrated:
                self.assertEqual(cron_skip_ledger.ensure_schema(conn), "rebuilt")
                conn.commit()

    def ledger_rows(self):
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM executions ORDER BY claimed_at, job_id")]

    def write(self, *jobs, raw=None):
        self.store.write_text(raw if raw is not None else json.dumps({"jobs": list(jobs)}), encoding="utf-8")

    def jobs(self) -> dict:
        return {j["id"]: j for j in json.loads(self.store.read_text(encoding="utf-8"))["jobs"]}

    def next_run(self, job_id) -> str:
        return self.jobs()[job_id]["next_run_at"]

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def run_policy(self, now=NOW, env=None):
        return ccp.apply_profile(self.profile, now, {} if env is None else env)


class SettingsTest(unittest.TestCase):
    def test_default_step(self):
        self.assertEqual(ccp.stagger_step({}), timedelta(minutes=30))

    def test_value_is_read_as_whole_minutes(self):
        self.assertEqual(ccp.stagger_step({ccp.STAGGER_MINUTES_ENV: " 10 "}), timedelta(minutes=10))

    def test_zero_turns_the_policy_off(self):
        self.assertIsNone(ccp.stagger_step({ccp.STAGGER_MINUTES_ENV: "0"}))

    def test_unparsable_and_negative_mean_the_default(self):
        for raw in ("soon", "-5", "1.5", ""):
            self.assertEqual(ccp.stagger_step({ccp.STAGGER_MINUTES_ENV: raw}), timedelta(minutes=30), raw)

    def test_a_value_wider_than_a_timedelta_means_the_default(self):
        for raw in (str(10**15), str(10**30)):
            self.assertEqual(ccp.stagger_step({ccp.STAGGER_MINUTES_ENV: raw}), timedelta(minutes=30), raw)


class GraceTest(unittest.TestCase):
    """The catch-up line is Hermes' own: half the period, clamped to [2 min, 2 h]."""

    def test_half_the_period_between_the_clamps(self):
        self.assertEqual(ccp.grace_for(timedelta(minutes=30)), timedelta(minutes=15))
        self.assertEqual(ccp.grace_for(timedelta(hours=1)), timedelta(minutes=30))

    def test_clamped_below(self):
        self.assertEqual(ccp.grace_for(timedelta(minutes=1)), timedelta(minutes=2))

    def test_clamped_above(self):
        self.assertEqual(ccp.grace_for(timedelta(days=1)), timedelta(hours=2))
        self.assertEqual(ccp.grace_for(timedelta(days=7)), timedelta(hours=2))


class VocabularyTest(unittest.TestCase):
    def test_the_skip_reason_is_in_the_ledgers_vocabulary(self):
        self.assertEqual(ccp.SKIP_REASON_STALE, cron_skip_ledger.SKIP_STALE_CATCH_UP)
        self.assertIn(ccp.SKIP_REASON_STALE, cron_skip_ledger.SKIP_REASONS)
        self.assertEqual(ccp.SKIPPED_STATUS, cron_skip_ledger.SKIPPED_STATUS)


class OnTimeTest(PolicyCase):
    def test_a_job_due_this_minute_is_left_alone(self):
        self.write(
            prompt_job("stockout-prevention", "20 9 * * *", slot("20 9 * * *"), NOW - timedelta(days=1)),
            script_job("stall-watch", "*/30 * * * *", NOW),
        )
        before = self.store.read_text()
        self.assertEqual(self.run_policy(), [])
        self.assertEqual(self.store.read_text(), before)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.ledger_rows(), [])

    def test_a_job_late_but_inside_hermes_grace_is_left_alone(self):
        # 08:00 on a daily schedule at 09:25: 85 minutes late, inside the two
        # hours Hermes gives a daily job before it calls the run a catch-up.
        self.write(prompt_job("gcp-networking-fabric-audit", "0 8 * * *", slot("0 8 * * *")))
        before = self.store.read_text()
        self.assertEqual(self.run_policy(), [])
        self.assertEqual(self.store.read_text(), before)

    def test_quiet_minute_costs_nothing(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW + timedelta(days=1)))
        self.assertEqual(self.run_policy(), [])
        self.assertFalse(self.state_path.exists())

    def test_no_store_and_an_unreadable_store_are_nothing(self):
        self.assertEqual(self.run_policy(), [])
        self.write(raw="{not json")
        self.assertEqual(self.run_policy(), [])


class TwoDayPauseTest(PolicyCase):
    """The reported episode: the install resumes after two days at scaleToZero.

    Every daily audit is more than a period overdue and is skipped to its next
    slot; the weekly audit missed once, inside its period, is the one catch-up
    and fires as Hermes has it; the half-hourly job is skipped to its next
    half hour; the scripts are not touched.
    """

    PAUSED_AT = NOW - timedelta(days=2)

    def roster(self):
        return (
            prompt_job("compliance-audit", "20 6 * * *", slot("20 6 * * *", self.PAUSED_AT), self.PAUSED_AT - timedelta(days=1)),
            prompt_job("obtainability-audit", "20 6 * * *", slot("20 6 * * *", self.PAUSED_AT), self.PAUSED_AT - timedelta(days=1)),
            prompt_job("ai-security-audit", "50 8 * * *", slot("50 8 * * *", self.PAUSED_AT)),
            prompt_job("security-patch-orchestrator", "20 7 * * 1", slot("20 7 * * 1", NOW), NOW - timedelta(days=9)),
            prompt_job("github-issue-resolver", "*/30 * * * *", slot("*/30 * * * *", self.PAUSED_AT)),
            script_job("stall-watch", "*/30 * * * *", slot("*/30 * * * *", self.PAUSED_AT)),
            script_job("github-repo-watcher", "*/10 * * * *", slot("*/10 * * * *", self.PAUSED_AT)),
        )

    def test_dailies_are_skipped_the_weekly_catches_up_and_scripts_fire(self):
        self.write(*self.roster())
        lines = self.run_policy()
        jobs = self.jobs()
        # Two days late on a daily schedule: the next slot is tomorrow's.
        self.assertEqual(jobs["compliance-audit"]["next_run_at"], "2026-08-06T06:20:00+00:00")
        self.assertEqual(jobs["obtainability-audit"]["next_run_at"], "2026-08-06T06:20:00+00:00")
        # Its newest slot, today's 08:50, came 35 minutes ago, inside Hermes'
        # grace: that run is on time and stays; the two before it are the skip.
        self.assertEqual(jobs["ai-security-audit"]["next_run_at"], "2026-08-05T08:50:00+00:00")
        # Monday's weekly audit, two days late on a seven-day period: the one
        # catch-up on this roster, left for Hermes to fire now.
        self.assertEqual(jobs["security-patch-orchestrator"]["next_run_at"], "2026-08-03T07:20:00+00:00")
        self.assertEqual(jobs["github-issue-resolver"]["next_run_at"], "2026-08-05T09:30:00+00:00")
        # Scripts prompt no model and are Hermes' to collapse.
        self.assertEqual(jobs["stall-watch"]["next_run_at"], iso(slot("*/30 * * * *", self.PAUSED_AT)))
        self.assertEqual(jobs["github-repo-watcher"]["next_run_at"], iso(slot("*/10 * * * *", self.PAUSED_AT)))
        self.assertEqual(
            [line for line in lines if "skipped" in line],
            [
                "platform: skipped the catch-up of compliance-audit: 3 occurrence(s) missed since "
                "2026-08-03T06:20:00+00:00, 2d 3h overdue on a 1d period; next run 2026-08-06T06:20:00+00:00",
                "platform: skipped the catch-up of obtainability-audit: 3 occurrence(s) missed since "
                "2026-08-03T06:20:00+00:00, 2d 3h overdue on a 1d period; next run 2026-08-06T06:20:00+00:00",
                "platform: skipped the catch-up of ai-security-audit: 2 occurrence(s) missed since "
                "2026-08-03T08:50:00+00:00, 2d overdue on a 1d period; next run 2026-08-05T08:50:00+00:00",
                "platform: skipped the catch-up of github-issue-resolver: 97 occurrence(s) missed since "
                "2026-08-03T09:00:00+00:00, 2d overdue on a 30m period; next run 2026-08-05T09:30:00+00:00",
            ],
        )
        self.assertEqual([line for line in lines if "deferred" in line], [])
        self.assertFalse(self.state_path.exists())

    def test_every_skip_is_a_row_in_the_jobs_history(self):
        self.write(*self.roster())
        self.run_policy()
        rows = self.ledger_rows()
        self.assertEqual(
            sorted(r["job_id"] for r in rows),
            ["ai-security-audit", "compliance-audit", "github-issue-resolver", "obtainability-audit"],
        )
        for row in rows:
            self.assertEqual(row["status"], "skipped")
            self.assertEqual(row["skip_reason"], "stale_catch_up")
            self.assertEqual(row["source"], "builtin")
            self.assertEqual(row["claimed_at"], iso(NOW))
            self.assertEqual(row["finished_at"], iso(NOW))
            self.assertIsNone(row["started_at"])
        compliance = [r for r in rows if r["job_id"] == "compliance-audit"][0]
        self.assertEqual(compliance["scheduled_instant"], "2026-08-03T06:20:00+00:00")
        self.assertIn("3 occurrence(s) missed", compliance["error"])
        self.assertIn("next run 2026-08-06T06:20:00+00:00", compliance["error"])

    def test_the_newest_slot_inside_grace_fires(self):
        # One day stale on a daily schedule. At 06:20:30 today's slot is thirty
        # seconds old: Hermes alone would fire it on time, so the policy
        # leaves it as the slot and skips only yesterday's.
        stale = datetime(2026, 8, 4, 6, 20, tzinfo=timezone.utc)
        for now, expect_next, expect_missed in (
            (datetime(2026, 8, 5, 6, 20, 30, tzinfo=timezone.utc), "2026-08-05T06:20:00+00:00", 1),
            (datetime(2026, 8, 5, 7, 0, tzinfo=timezone.utc), "2026-08-05T06:20:00+00:00", 1),
            # Past the two-hour grace the newest slot is a catch-up too.
            (datetime(2026, 8, 5, 8, 21, tzinfo=timezone.utc), "2026-08-06T06:20:00+00:00", 2),
        ):
            self.make_ledger(migrated=True)
            self.write(prompt_job("compliance-audit", "20 6 * * *", stale))
            lines = self.run_policy(now=now)
            self.assertEqual(self.next_run("compliance-audit"), expect_next, now)
            self.assertIn(f"skipped the catch-up of compliance-audit: {expect_missed} occurrence(s) missed since 2026-08-04T06:20:00+00:00", lines[0])
            self.assertIn(f"next run {expect_next}", lines[0])
            rows = self.ledger_rows()
            self.assertEqual([(r["job_id"], r["scheduled_instant"]) for r in rows], [("compliance-audit", iso(stale))], now)

    def test_the_next_tick_finds_nothing_to_do(self):
        self.write(*self.roster())
        self.run_policy()
        later = NOW + timedelta(minutes=1)
        store = self.store.read_text()
        self.assertEqual(self.run_policy(now=later), [])
        self.assertEqual(self.store.read_text(), store)
        self.assertEqual(len(self.ledger_rows()), 4)


class ShortOutageTest(PolicyCase):
    """A three-hour gap: the audits it covered are catch-ups inside their
    period, so the first fires now and the rest are spaced a step apart."""

    def roster(self):
        return (
            prompt_job("compliance-audit", "20 6 * * *", slot("20 6 * * *")),
            prompt_job("obtainability-audit", "20 6 * * *", slot("20 6 * * *")),
            prompt_job("security-patch-orchestrator", "20 7 * * 3", slot("20 7 * * 3")),
            prompt_job("gcp-networking-fabric-audit", "0 8 * * *", slot("0 8 * * *")),
            prompt_job("ai-security-audit", "50 8 * * *", slot("50 8 * * *")),
            script_job("stall-watch", "*/30 * * * *", slot("*/30 * * * *")),
        )

    def test_the_first_fires_and_the_rest_are_spaced_a_step_apart(self):
        self.write(*self.roster())
        lines = self.run_policy()
        jobs = self.jobs()
        # Oldest miss first. The two 06:20 audits tie on the slot; the id breaks it.
        self.assertEqual(jobs["compliance-audit"]["next_run_at"], "2026-08-05T06:20:00+00:00")
        self.assertEqual(jobs["obtainability-audit"]["next_run_at"], "2026-08-06T06:20:00+00:00")
        self.assertEqual(jobs["security-patch-orchestrator"]["next_run_at"], "2026-08-12T07:20:00+00:00")
        # 08:00 at 09:25 is 85 minutes late: inside Hermes' grace, no catch-up.
        self.assertEqual(jobs["gcp-networking-fabric-audit"]["next_run_at"], "2026-08-05T08:00:00+00:00")
        self.assertEqual(jobs["ai-security-audit"]["next_run_at"], "2026-08-05T08:50:00+00:00")
        self.assertEqual(jobs["stall-watch"]["next_run_at"], iso(slot("*/30 * * * *")))
        self.assertEqual(
            lines,
            [
                "platform: deferred the catch-up of obtainability-audit to 2026-08-05T09:55:30+00:00 "
                "(1 occurrence(s) missed since 2026-08-05T06:20:00+00:00); held at 2026-08-06T06:20:00+00:00 until then",
                "platform: deferred the catch-up of security-patch-orchestrator to 2026-08-05T10:25:30+00:00 "
                "(1 occurrence(s) missed since 2026-08-05T07:20:00+00:00); held at 2026-08-12T07:20:00+00:00 until then",
            ],
        )
        self.assertEqual(
            self.state()["deferred"],
            {
                "obtainability-audit": {
                    "next_run_at": "2026-08-06T06:20:00+00:00",
                    "restore": "2026-08-05T06:20:00+00:00",
                    "release_at": "2026-08-05T09:55:30+00:00",
                },
                "security-patch-orchestrator": {
                    "next_run_at": "2026-08-12T07:20:00+00:00",
                    "restore": "2026-08-05T07:20:00+00:00",
                    "release_at": "2026-08-05T10:25:30+00:00",
                },
            },
        )
        # A deferral is a hold, not a skip: nothing in the history yet. Hermes
        # writes the missed-window row itself when the catch-up fires.
        self.assertEqual(self.ledger_rows(), [])

    def test_a_held_slot_sits_on_the_expressions_lattice(self):
        # Hermes re-anchors a due cron instant that is not one of the
        # expression's own slots without firing it, so a hold off the lattice
        # would turn the deferral into a skip.
        from croniter import croniter

        self.write(*self.roster())
        self.run_policy()
        for job_id, expr in (("obtainability-audit", "20 6 * * *"), ("security-patch-orchestrator", "20 7 * * 3")):
            held = datetime.fromisoformat(self.next_run(job_id))
            prev = croniter(expr, held + timedelta(seconds=1)).get_prev(datetime)
            self.assertEqual(prev, held, job_id)

    def hermes_fired(self, job_id, next_run_at):
        """What Hermes leaves behind once it has run the job: the slot advanced."""
        jobs = self.jobs()
        jobs[job_id]["next_run_at"] = next_run_at
        self.write(*jobs.values())

    def test_each_deferred_job_is_released_at_its_own_time(self):
        self.write(*self.roster())
        self.run_policy()
        self.hermes_fired("compliance-audit", "2026-08-06T06:20:00+00:00")
        self.hermes_fired("gcp-networking-fabric-audit", "2026-08-06T08:00:00+00:00")
        self.hermes_fired("ai-security-audit", "2026-08-06T08:50:00+00:00")
        # One minute before the first release: still held.
        early = NOW + timedelta(minutes=29)
        self.assertEqual(self.run_policy(now=early), [])
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-06T06:20:00+00:00")

        first = NOW + timedelta(minutes=30)
        lines = self.run_policy(now=first)
        self.assertEqual(lines, ["platform: released obtainability-audit for its catch-up of 2026-08-05T06:20:00+00:00"])
        # The exact stored string comes back, so Hermes sees its own slot and
        # takes its own catch-up path for it.
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-05T06:20:00+00:00")
        self.assertEqual(self.next_run("security-patch-orchestrator"), "2026-08-12T07:20:00+00:00")
        self.assertEqual(list(self.state()["deferred"]), ["security-patch-orchestrator"])

        self.hermes_fired("obtainability-audit", "2026-08-06T06:20:00+00:00")
        second = NOW + timedelta(minutes=60)
        lines = self.run_policy(now=second)
        self.assertEqual(lines, ["platform: released security-patch-orchestrator for its catch-up of 2026-08-05T07:20:00+00:00"])
        self.assertEqual(self.next_run("security-patch-orchestrator"), "2026-08-05T07:20:00+00:00")
        self.assertEqual(self.state()["deferred"], {})

    def test_a_released_job_that_is_the_oldest_catch_up_fires_at_once(self):
        self.write(*self.roster())
        self.run_policy()
        self.hermes_fired("compliance-audit", "2026-08-06T06:20:00+00:00")
        self.run_policy(now=NOW + timedelta(minutes=30))
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-05T06:20:00+00:00")
        self.assertNotIn("obtainability-audit", self.state()["deferred"])

    def test_a_deferred_job_hermes_moved_is_left_as_hermes_left_it(self):
        self.write(*self.roster())
        self.run_policy()
        jobs = self.jobs()
        jobs["obtainability-audit"]["next_run_at"] = "2026-08-07T06:20:00+00:00"  # somebody ran it by hand
        self.write(*jobs.values())
        lines = self.run_policy(now=NOW + timedelta(minutes=30))
        self.assertEqual(
            lines,
            ["platform: dropped the deferred catch-up of obtainability-audit; its schedule moved to 2026-08-07T06:20:00+00:00"],
        )
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-07T06:20:00+00:00")
        self.assertNotIn("obtainability-audit", self.state()["deferred"])

    def test_a_deferred_job_that_left_the_roster_is_forgotten(self):
        self.write(*self.roster())
        self.run_policy()
        self.write(*[j for j in self.jobs().values() if j["id"] != "obtainability-audit"])
        self.run_policy(now=NOW + timedelta(minutes=1))
        self.assertNotIn("obtainability-audit", self.state()["deferred"])

    def test_the_step_is_tunable(self):
        self.write(*self.roster())
        lines = self.run_policy(env={ccp.STAGGER_MINUTES_ENV: "10"})
        self.assertIn("deferred the catch-up of obtainability-audit to 2026-08-05T09:35:30+00:00", lines[0])
        self.assertIn("deferred the catch-up of security-patch-orchestrator to 2026-08-05T09:45:30+00:00", lines[1])

    def test_a_step_past_the_calendars_end_folds_rather_than_raises(self):
        # 10**10 minutes fits a timedelta and not the calendar from now: no
        # turn to wait for, so the later catch-ups fold into their next slots.
        self.write(*self.roster())
        lines = self.run_policy(env={ccp.STAGGER_MINUTES_ENV: str(10**10)})
        self.assertFalse(any("failed" in line for line in lines), lines)
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-05T06:20:00+00:00")
        self.assertTrue(any(line.startswith("platform: skipped the catch-up of obtainability-audit") for line in lines), lines)
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-06T06:20:00+00:00")
        self.assertFalse(self.state_path.exists())

    def test_a_second_pause_past_the_period_turns_a_turn_into_a_skip(self):
        self.write(*self.roster())
        self.run_policy()
        self.hermes_fired("compliance-audit", "2026-08-06T06:20:00+00:00")
        self.hermes_fired("gcp-networking-fabric-audit", "2026-08-06T08:00:00+00:00")
        self.hermes_fired("ai-security-audit", "2026-08-06T08:50:00+00:00")
        # The ticker stops for two days with both records live, past their
        # turns and past the slots they are held at.
        later = NOW + timedelta(days=2)
        lines = self.run_policy(now=later)
        self.assertIn("platform: released obtainability-audit for its catch-up of 2026-08-05T06:20:00+00:00", lines)
        self.assertIn("platform: released security-patch-orchestrator for its catch-up of 2026-08-05T07:20:00+00:00", lines)
        # The daily one is a whole period late and more: skipped to its next
        # slot, with the row; the weekly one is inside its period and the
        # oldest catch-up left, so it fires.
        self.assertTrue(
            any(line.startswith("platform: skipped the catch-up of obtainability-audit: 3 occurrence(s) missed since 2026-08-05T06:20:00+00:00, 2d 3h overdue on a 1d period") for line in lines),
            lines,
        )
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-08T06:20:00+00:00")
        self.assertEqual(self.next_run("security-patch-orchestrator"), "2026-08-05T07:20:00+00:00")
        self.assertIn("obtainability-audit", [r["job_id"] for r in self.ledger_rows() if r["skip_reason"] == "stale_catch_up"])
        self.assertEqual(self.state()["deferred"], {})

    def test_two_turns_on_one_tick_are_spaced_again(self):
        self.write(*self.roster())
        self.run_policy()
        self.hermes_fired("compliance-audit", "2026-08-06T06:20:00+00:00")
        self.hermes_fired("gcp-networking-fabric-audit", "2026-08-06T08:00:00+00:00")
        self.hermes_fired("ai-security-audit", "2026-08-06T08:50:00+00:00")
        # The ticker misses both turns (09:55 and 10:25) and comes back at 10:40.
        later = NOW + timedelta(minutes=75)
        again = (later + timedelta(minutes=30)).isoformat()
        lines = self.run_policy(now=later)
        self.assertIn("platform: released obtainability-audit for its catch-up of 2026-08-05T06:20:00+00:00", lines)
        self.assertIn("platform: released security-patch-orchestrator for its catch-up of 2026-08-05T07:20:00+00:00", lines)
        # The oldest fires; the other waits one more step from this tick.
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-05T06:20:00+00:00")
        self.assertEqual(self.next_run("security-patch-orchestrator"), "2026-08-12T07:20:00+00:00")
        self.assertTrue(any(f"deferred the catch-up of security-patch-orchestrator to {again}" in line for line in lines), lines)
        self.assertEqual(self.state()["deferred"]["security-patch-orchestrator"]["release_at"], again)
        self.assertEqual(self.state()["deferred"]["security-patch-orchestrator"]["restore"], "2026-08-05T07:20:00+00:00")


class FoldedCatchUpTest(PolicyCase):
    def test_a_deferral_that_would_land_after_the_next_slot_is_a_skip(self):
        # Two half-hourly jobs 20 minutes late (grace is 15): the first fires
        # now, the second's step lands after its own 09:30 slot, so that slot
        # is its catch-up.
        late = slot("*/30 * * * *")  # 09:00
        self.assertEqual(late, datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc))
        self.write(prompt_job("a-resolver", "*/30 * * * *", late), prompt_job("b-resolver", "*/30 * * * *", late))
        lines = self.run_policy(now=NOW - timedelta(minutes=5))  # 09:20:30
        self.assertEqual(self.next_run("a-resolver"), iso(late))
        self.assertEqual(self.next_run("b-resolver"), "2026-08-05T09:30:00+00:00")
        self.assertEqual(
            lines,
            [
                "platform: skipped the catch-up of b-resolver: 1 occurrence(s) missed since "
                "2026-08-05T09:00:00+00:00, a catch-up 30m later would run at or after its next slot; next run 2026-08-05T09:30:00+00:00"
            ],
        )
        rows = self.ledger_rows()
        self.assertEqual([r["job_id"] for r in rows], ["b-resolver"])
        self.assertEqual(rows[0]["skip_reason"], "stale_catch_up")
        self.assertFalse(self.state_path.exists())


class ExemptionsTest(PolicyCase):
    def test_a_run_asked_for_by_hand_is_not_touched(self):
        asked = NOW - timedelta(days=2)
        self.write(marked(prompt_job("compliance-audit", "20 6 * * *", asked)))
        self.assertEqual(self.run_policy(), [])
        self.assertEqual(self.next_run("compliance-audit"), iso(asked))

    def test_a_slot_a_dead_dispatch_left_pending_is_left_to_hermes(self):
        # Hermes restores an unclaimed pending slot as the due instant whatever
        # next_run_at says, so a rewrite here would be undone and its row wrong.
        stale = slot("20 6 * * *", NOW - timedelta(days=2))
        self.write(
            prompt_job("compliance-audit", "20 6 * * *", stale, pending_slot={"scheduled_at": iso(stale), "at": iso(stale), "by": "x"}),
            prompt_job("obtainability-audit", "20 6 * * *", stale),
        )
        lines = self.run_policy()
        self.assertEqual(self.next_run("compliance-audit"), iso(stale))
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-06T06:20:00+00:00")
        self.assertEqual([line for line in lines if "compliance-audit" in line], [])

    def test_a_disabled_job_and_a_script_are_not_touched(self):
        stale = slot("20 6 * * *", NOW - timedelta(days=2))
        self.write(
            prompt_job("compliance-audit", "20 6 * * *", stale, enabled=False),
            script_job("stall-watch", "*/30 * * * *", stale),
        )
        self.assertEqual(self.run_policy(), [])
        self.assertEqual(self.next_run("compliance-audit"), iso(stale))
        self.assertEqual(self.next_run("stall-watch"), iso(stale))

    def test_an_unscheduled_or_unparseable_job_is_left_to_hermes(self):
        self.write(prompt_job("fresh", "20 6 * * *", None), prompt_job("torn", "20 6 * * *", "yesterday"))
        before = self.store.read_text()
        self.assertEqual(self.run_policy(), [])
        self.assertEqual(self.store.read_text(), before)

    def test_a_bad_expression_is_left_to_hermes(self):
        self.write(prompt_job("odd", "not cron", NOW - timedelta(days=2)))
        before = self.store.read_text()
        self.assertEqual(self.run_policy(), [])
        self.assertEqual(self.store.read_text(), before)

    def test_an_interval_job_is_measured_by_its_period(self):
        # `cronjob(action='create', schedule='every 2h')` stores an interval.
        last = NOW - timedelta(days=2)
        self.write(interval_job("two-hourly", 120, last + timedelta(hours=2), last))
        lines = self.run_policy()
        # The newest slot is now itself, inside grace: it stays and fires; the
        # 23 before it are the skip.
        self.assertIn("skipped the catch-up of two-hourly: 23 occurrence(s) missed", lines[0])
        self.assertEqual(self.next_run("two-hourly"), iso(last + timedelta(hours=48)))
        # A tick past the grace moves it to the first multiple of the period
        # past now, as Hermes anchors an interval on last_run_at.
        self.write(interval_job("two-hourly", 120, last + timedelta(hours=2), last))
        self.run_policy(now=NOW + timedelta(minutes=61))
        self.assertEqual(self.next_run("two-hourly"), iso(last + timedelta(hours=50)))

    def test_turning_the_policy_off_releases_what_it_deferred(self):
        self.write(
            prompt_job("compliance-audit", "20 6 * * *", slot("20 6 * * *")),
            prompt_job("obtainability-audit", "20 6 * * *", slot("20 6 * * *")),
        )
        self.run_policy()
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-06T06:20:00+00:00")
        lines = self.run_policy(now=NOW + timedelta(minutes=1), env={ccp.STAGGER_MINUTES_ENV: "0"})
        self.assertEqual(lines, ["platform: released obtainability-audit for its catch-up of 2026-08-05T06:20:00+00:00"])
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-05T06:20:00+00:00")
        self.assertEqual(self.state()["deferred"], {})

    def test_off_means_nothing_is_skipped_either(self):
        stale = slot("20 6 * * *", NOW - timedelta(days=2))
        self.write(prompt_job("compliance-audit", "20 6 * * *", stale))
        self.assertEqual(self.run_policy(env={ccp.STAGGER_MINUTES_ENV: "0"}), [])
        self.assertEqual(self.next_run("compliance-audit"), iso(stale))
        self.assertFalse(self.state_path.exists())


class LedgerTest(PolicyCase):
    def stale_roster(self):
        return (prompt_job("compliance-audit", "20 6 * * *", slot("20 6 * * *", NOW - timedelta(days=2))),)

    def test_no_ledger_yet_still_skips_and_says_so(self):
        self.ledger_path.unlink()
        self.write(*self.stale_roster())
        lines = self.run_policy()
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-06T06:20:00+00:00")
        self.assertTrue(any("no history to record the skip of compliance-audit in" in line for line in lines), lines)
        self.assertFalse(self.ledger_path.exists())

    def test_an_unmigrated_ledger_is_not_written_to(self):
        self.make_ledger(migrated=False)
        self.write(*self.stale_roster())
        lines = self.run_policy()
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-06T06:20:00+00:00")
        self.assertTrue(any("does not record skipped occurrences yet" in line for line in lines), lines)
        self.assertEqual(self.ledger_rows(), [])

    def test_a_ledger_that_cannot_be_opened_costs_one_line(self):
        self.ledger_path.write_text("not a database", encoding="utf-8")
        self.write(*self.stale_roster())
        lines = self.run_policy()
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-06T06:20:00+00:00")
        self.assertTrue(any("could not record the skip of compliance-audit" in line for line in lines), lines)


class ZoneTest(PolicyCase):
    def test_slots_are_walked_in_the_profiles_zone(self):
        # 06:20 Brussels in August is 04:20 UTC: the skip lands on tomorrow's
        # 04:20 UTC, the zone's own slot.
        (self.profile / "config.yaml").write_text("timezone: Europe/Brussels\n", encoding="utf-8")
        stale = datetime(2026, 8, 3, 4, 20, tzinfo=timezone.utc)
        self.write(prompt_job("compliance-audit", "20 6 * * *", stale))
        self.run_policy()
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-06T06:20:00+02:00")

    def test_a_stamp_west_of_utc_carries_the_zones_offset(self):
        # Hermes re-anchors, without firing, a due instant whose stored offset
        # differs from now's while its wall clock reads later than now's. A
        # UTC stamp for 06:20 New York (10:20Z) is exactly that shape, so the
        # hold and the skip are written in the zone, as Hermes writes them.
        (self.profile / "config.yaml").write_text("timezone: America/New_York\n", encoding="utf-8")
        stale = datetime(2026, 8, 5, 10, 20, tzinfo=timezone.utc)  # today's 06:20 EDT
        now = datetime(2026, 8, 5, 13, 25, 30, tzinfo=timezone.utc)  # 09:25 EDT, three hours after the slot
        self.write(
            prompt_job("compliance-audit", "20 6 * * *", stale),
            prompt_job("obtainability-audit", "20 6 * * *", stale),
            prompt_job("stockout-prevention", "20 9 * * *", datetime(2026, 8, 3, 13, 20, tzinfo=timezone.utc)),
        )
        self.run_policy(now=now)
        self.assertEqual(self.next_run("compliance-audit"), iso(stale))
        self.assertEqual(self.next_run("obtainability-audit"), "2026-08-06T06:20:00-04:00")
        self.assertEqual(self.next_run("stockout-prevention"), "2026-08-05T09:20:00-04:00")
        self.assertEqual(self.state()["deferred"]["obtainability-audit"]["next_run_at"], "2026-08-06T06:20:00-04:00")
        # The history keys the missed slot in UTC, as Hermes does.
        rows = self.ledger_rows()
        self.assertEqual([(r["job_id"], r["scheduled_instant"]) for r in rows], [("stockout-prevention", "2026-08-03T13:20:00+00:00")])


class DaylightSavingTest(PolicyCase):
    """Slots are walked on the zone's wall clock, as Hermes walks them: a
    zone-aware croniter start keeps its offset across a DST change and lands
    an hour off the lattice Hermes writes, which Hermes then re-anchors as an
    expression edit without firing."""

    def test_a_skip_across_the_spring_change_lands_on_hermes_slot(self):
        (self.profile / "config.yaml").write_text("timezone: America/New_York\n", encoding="utf-8")
        # Two days stale on Saturday 2026-03-07 09:25:30 EST; clocks go forward on the 8th.
        stale = datetime(2026, 3, 5, 11, 20, tzinfo=timezone.utc)  # 06:20 EST
        now = datetime(2026, 3, 7, 14, 25, 30, tzinfo=timezone.utc)
        self.write(prompt_job("compliance-audit", "20 6 * * *", stale))
        lines = self.run_policy(now=now)
        self.assertEqual(self.next_run("compliance-audit"), "2026-03-08T06:20:00-04:00")
        self.assertIn("3 occurrence(s) missed", lines[0])

    def test_a_skip_across_the_autumn_change_lands_on_hermes_slot(self):
        (self.profile / "config.yaml").write_text("timezone: America/New_York\n", encoding="utf-8")
        # Saturday 2026-10-31 09:25:30 EDT; clocks go back on 11-01.
        stale = datetime(2026, 10, 29, 10, 20, tzinfo=timezone.utc)  # 06:20 EDT
        now = datetime(2026, 10, 31, 13, 25, 30, tzinfo=timezone.utc)
        self.write(prompt_job("compliance-audit", "20 6 * * *", stale))
        self.run_policy(now=now)
        self.assertEqual(self.next_run("compliance-audit"), "2026-11-01T06:20:00-05:00")

    def test_a_hold_across_the_spring_change_lands_on_hermes_slot(self):
        (self.profile / "config.yaml").write_text("timezone: America/New_York\n", encoding="utf-8")
        now = datetime(2026, 3, 7, 14, 25, 30, tzinfo=timezone.utc)
        today = datetime(2026, 3, 7, 11, 20, tzinfo=timezone.utc)  # 06:20 EST, three hours ago
        self.write(prompt_job("compliance-audit", "20 6 * * *", today), prompt_job("obtainability-audit", "20 6 * * *", today))
        self.run_policy(now=now)
        self.assertEqual(self.next_run("compliance-audit"), iso(today))
        self.assertEqual(self.next_run("obtainability-audit"), "2026-03-08T06:20:00-04:00")


class CroniterMissingTest(PolicyCase):
    def test_says_so_once_and_changes_nothing(self):
        stale = slot("20 6 * * *", NOW - timedelta(days=2))
        self.write(prompt_job("compliance-audit", "20 6 * * *", stale))
        original = ccp.croniter_available
        ccp.croniter_available = lambda: False
        self.addCleanup(setattr, ccp, "croniter_available", original)
        lines = self.run_policy()
        self.assertEqual(lines, ["platform: cron catch-up policy cannot import croniter; nothing changed"])
        self.assertEqual(self.next_run("compliance-audit"), iso(stale))
        self.assertEqual(self.run_policy(now=NOW + timedelta(minutes=1)), [])
        self.assertTrue(self.state()["croniter_missing"])


class StoreLockTest(PolicyCase):
    def test_a_lock_that_falls_open_skips_the_write(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", slot("20 6 * * *", NOW - timedelta(days=2))))

        class FallenOpen:
            @staticmethod
            def cron_jobs_lock(cron_dir, timeout=None, *, warn=None):
                warn(f"WARN: timed out after 15.0s waiting for {cron_dir / '.jobs.lock'}; proceeding without lock")
                return contextlib.nullcontext()

        original = ccp._platform_module
        ccp._platform_module = lambda name: FallenOpen() if name == ccp.LOCK_MODULE else original(name)
        self.addCleanup(setattr, ccp, "_platform_module", original)
        lines = self.run_policy()
        self.assertTrue(any("the store lock is held" in line for line in lines), lines)
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-03T06:20:00+00:00")
        self.assertEqual(self.ledger_rows(), [])

    def test_the_write_happens_under_hermes_lock(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", slot("20 6 * * *", NOW - timedelta(days=2))))
        taken = []
        scaffold = ccp._platform_module(ccp.LOCK_MODULE)
        real = scaffold.cron_jobs_lock

        def recording(cron_dir, timeout=None, *, warn=None):
            taken.append(cron_dir)
            return real(cron_dir, timeout, warn=warn)

        scaffold.cron_jobs_lock = recording
        self.addCleanup(setattr, scaffold, "cron_jobs_lock", real)
        self.run_policy()
        self.assertEqual(taken, [self.profile / "cron"])


class WriteOrderTest(PolicyCase):
    """A new record reaches disk before the store takes its hold, and a
    released record leaves the state only after the store has its slot back."""

    def failing_store_write(self):
        self.real_write = ccp._write_json

        def write(path, payload):
            if path == self.store:
                raise OSError("disk full")
            self.real_write(path, payload)

        ccp._write_json = write
        self.addCleanup(setattr, ccp, "_write_json", self.real_write)

    def store_writable_again(self):
        ccp._write_json = self.real_write

    def test_a_hold_the_store_never_took_is_a_record_the_next_tick_drops(self):
        self.write(prompt_job("a", "20 6 * * *", slot("20 6 * * *")), prompt_job("b", "20 6 * * *", slot("20 6 * * *")))
        self.failing_store_write()
        lines = ccp.apply(self.profile.parent.parent, NOW, {})
        self.assertTrue(any("cron catch-up policy failed" in line for line in lines), lines)
        self.assertEqual(self.next_run("b"), iso(slot("20 6 * * *")))  # the store is as it was
        self.assertIn("b", self.state()["deferred"])  # the record landed first
        self.store_writable_again()
        # Next tick: the store never took the hold, so the record is dropped and
        # the job is decided afresh (deferred again, the store now writable).
        lines = ccp.apply(self.profile.parent.parent, NOW + timedelta(minutes=1), {})
        self.assertTrue(any("dropped the deferred catch-up of b" in line for line in lines), lines)
        self.assertTrue(any("deferred the catch-up of b" in line for line in lines), lines)

    def test_a_state_write_that_fails_on_a_release_tick_loses_no_record(self):
        # b was deferred; a ran; c comes due stale. The release of b and the
        # new record for c go into one state write, and that write fails.
        self.write(prompt_job("a", "20 6 * * *", slot("20 6 * * *")), prompt_job("b", "20 6 * * *", slot("20 6 * * *")))
        self.run_policy()
        jobs = self.jobs()
        jobs["a"]["next_run_at"] = "2026-08-06T06:20:00+00:00"
        self.write(*jobs.values(), prompt_job("c", "20 7 * * *", slot("20 7 * * *")))
        real = ccp._write_json
        failed = []

        def write(path, payload):
            if path == self.state_path and not failed:
                failed.append(path)
                raise OSError("transient")
            real(path, payload)

        ccp._write_json = write
        self.addCleanup(setattr, ccp, "_write_json", real)
        later = NOW + timedelta(minutes=30)
        lines = self.run_policy(now=later)
        self.assertTrue(any("could not save its state (transient); nothing changed" in line for line in lines), lines)
        self.assertEqual(self.next_run("b"), "2026-08-06T06:20:00+00:00")  # still held
        self.assertEqual(self.next_run("c"), iso(slot("20 7 * * *")))  # untouched
        self.assertEqual(list(self.state()["deferred"]), ["b"])  # the record survived both saves
        lines = self.run_policy(now=later + timedelta(minutes=1))
        self.assertTrue(any("released b for its catch-up" in line for line in lines), lines)

    def test_a_release_the_store_never_took_keeps_its_record(self):
        self.write(prompt_job("a", "20 6 * * *", slot("20 6 * * *")), prompt_job("b", "20 6 * * *", slot("20 6 * * *")))
        self.run_policy()
        self.assertIn("b", self.state()["deferred"])
        jobs = self.jobs()
        jobs["a"]["next_run_at"] = "2026-08-06T06:20:00+00:00"  # Hermes ran the one it was handed
        self.write(*jobs.values())
        self.failing_store_write()
        lines = ccp.apply(self.profile.parent.parent, NOW + timedelta(minutes=30), {})
        self.assertTrue(any("cron catch-up policy failed" in line for line in lines), lines)
        self.assertEqual(self.next_run("b"), "2026-08-06T06:20:00+00:00")  # still held
        self.assertIn("b", self.state()["deferred"])  # and still on record
        self.store_writable_again()
        lines = ccp.apply(self.profile.parent.parent, NOW + timedelta(minutes=31), {})
        self.assertTrue(any("released b for its catch-up" in line for line in lines), lines)
        self.assertEqual(self.next_run("b"), iso(slot("20 6 * * *")))
        self.assertEqual(self.state()["deferred"], {})


class ApplyTest(PolicyCase):
    def test_every_profile_with_a_store_is_visited_and_a_fault_is_one_line(self):
        home = self.profile.parent.parent
        other = home / "profiles" / "cluster-a"
        (other / "cron").mkdir(parents=True)
        (other / "cron" / "jobs.json").write_text(
            json.dumps({"jobs": [prompt_job("compliance-audit", "20 6 * * *", slot("20 6 * * *", NOW - timedelta(days=2)))]}),
            encoding="utf-8",
        )
        self.write(raw="[")  # torn: nothing to do here, not a fault
        lines = ccp.apply(home, NOW, {})
        self.assertTrue(any(line.startswith("cluster-a: skipped the catch-up of compliance-audit") for line in lines), lines)
        self.assertEqual([line for line in lines if line.startswith("platform:")], [])

    def test_no_profiles_dir_is_nothing(self):
        self.assertEqual(ccp.apply(Path(self._tmp.name) / "nowhere", NOW, {}), [])


if __name__ == "__main__":
    unittest.main()
