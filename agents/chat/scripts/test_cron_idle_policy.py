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

    def __call__(self):
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
        self.write(prompt_job("stockout-prevention", "20 9 * * *", NOW, last))
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

    def test_interval_and_unscheduled_jobs_are_left_to_hermes(self):
        self.write(
            {"id": "ticker-ish", "enabled": True, "schedule": {"kind": "interval", "minutes": 5},
             "next_run_at": iso(NOW), "last_run_at": iso(days_ago(1))},
            prompt_job("fresh", "20 6 * * *", None),
        )
        before = self.store.read_text()
        self.run_policy(Reader(days_ago(8)))
        self.assertEqual(self.store.read_text(), before)

    def test_disabled_job_is_not_held(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(2), enabled=False))
        self.run_policy(Reader(days_ago(8)))
        self.assertEqual(self.next_run("compliance-audit"), iso(NOW))


class HibernateTest(PolicyCase):
    def roster(self):
        return (
            prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)),
            prompt_job("security-patch-orchestrator", "20 7 * * 1", NOW, days_ago(20)),
            prompt_job("never-ran", "0 8 * * *", NOW),
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

    def test_an_overdue_job_after_a_restart_is_held_a_day_from_now(self):
        self.write(prompt_job("compliance-audit", "20 6 * * *", days_ago(3), days_ago(4)))
        late = NOW + timedelta(hours=5)
        self.run_policy(Reader(days_ago(15)), now=late)
        self.assertEqual(self.next_run("compliance-audit"), "2026-08-08T06:20:00+00:00")


class WakeTest(PolicyCase):
    def hibernate(self):
        self.write(
            prompt_job("compliance-audit", "20 6 * * *", NOW, days_ago(1)),
            prompt_job("obtainability-audit", "50 6 * * *", NOW, days_ago(1)),
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
            cip.read_signal(),
            (datetime(2026, 8, 1, 12, tzinfo=timezone.utc), HUMAN),
        )
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["kubectl", "get", cip.PLATFORM_AGENT_RESOURCE, "-n", "kubeagents-system", "-o", "json"])
        self.assertEqual(kwargs["timeout"], cip.KUBECTL_TIMEOUT_SECONDS)

    def test_absent_field_falls_back_to_creation_time(self):
        self.stub(self.listing({"usage": {"lastActiveTime": "2026-08-06T06:00:00Z"}}))
        self.assertEqual(cip.read_signal(), (datetime(2026, 7, 1, tzinfo=timezone.utc), CREATED))
        self.stub(self.listing())
        self.assertEqual(cip.read_signal(), (datetime(2026, 7, 1, tzinfo=timezone.utc), CREATED))

    def test_namespace_comes_from_the_environment(self):
        import os

        calls = self.stub(self.listing())
        os.environ[cip.NAMESPACE_ENV] = "agents"
        self.addCleanup(os.environ.pop, cip.NAMESPACE_ENV, None)
        cip.read_signal()
        self.assertEqual(calls[0][0][4], "agents")

    def test_missing_cr_raises(self):
        self.stub(json.dumps({"items": []}))
        with self.assertRaisesRegex(RuntimeError, "no PlatformAgent"):
            cip.read_signal()

    def test_failed_kubectl_raises_with_its_stderr(self):
        self.stub(returncode=1, stderr="Error from server (Forbidden): platformagents is forbidden")
        with self.assertRaisesRegex(RuntimeError, "exited 1: Error from server"):
            cip.read_signal()

    def test_unreachable_sandbox_propagates(self):
        self.stub(raises=subprocess.TimeoutExpired(["kubectl"], 30))
        with self.assertRaises(subprocess.TimeoutExpired):
            cip.read_signal()

    def test_neither_field_raises(self):
        self.stub(json.dumps({"items": [{"metadata": {"name": "x"}}]}))
        with self.assertRaisesRegex(RuntimeError, "neither"):
            cip.read_signal()


if __name__ == "__main__":
    unittest.main()
