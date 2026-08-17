"""Unit tests for profile_cron_tick.py — the ticker for named profiles' cron.

Run: python3 -m unittest agents/chat/scripts/test_profile_cron_tick.py

The bug this script fixes was silent: the Platform Agent's whole watchdog
roster sat `enabled: true` with a `next_run_at` days in the past and nothing
anywhere said so. So the tests are weighted towards the two ways that silence
could come back —

  - a due job the filter declines to notice (every ambiguous shape must resolve
    to "due", because a spurious tick is a no-op and a skipped one is the bug);
  - the script shipping without the cron entry that runs it, or with an entry
    naming a script the image does not carry, which `_run_job_script` reports
    as "Script not found" once a minute into a job nobody reads.
"""

import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import profile_cron_tick as pct  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
ROOT_ROSTER = REPO / "agents" / "chat" / "defaults" / "cron" / "jobs.json"
# Both directories are copied into /opt/defaults/scripts by the Dockerfile, and
# the entrypoint force-syncs that onto $HERMES_HOME/scripts — the one directory
# the scheduler will run a `no_agent` script from.
SCRIPT_DIRS = (
    REPO / "agents" / "chat" / "scripts",
    REPO / "agents" / "platform" / "scripts",
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def job(job_id, next_run_at="__unset__", **extra):
    entry = {"id": job_id, "enabled": True, "schedule": {"kind": "cron", "expr": "* * * * *"}}
    if next_run_at != "__unset__":
        entry["next_run_at"] = next_run_at
    entry.update(extra)
    return entry


def write_store(directory: Path, *jobs, raw=None) -> Path:
    store = directory / "cron" / "jobs.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(raw if raw is not None else json.dumps({"jobs": list(jobs)}), encoding="utf-8")
    return store


class DueJobIdsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)

    def due(self, *jobs, raw=None):
        return pct.due_job_ids(write_store(self.d, *jobs, raw=raw), NOW)

    def test_overdue_job_is_due(self):
        self.assertEqual(self.due(job("audit", "2026-08-04T06:20:00+00:00")), ["audit"])

    def test_future_job_is_not_due(self):
        self.assertEqual(self.due(job("audit", "2026-08-07T06:20:00+00:00")), [])

    def test_exactly_now_is_due(self):
        self.assertEqual(self.due(job("audit", NOW.isoformat())), ["audit"])

    def test_disabled_job_is_never_due(self):
        """`hermes cron pause` sets enabled: False, so this is the pause path too."""
        self.assertEqual(self.due(job("retired", "2020-01-01T00:00:00+00:00", enabled=False)), [])

    def test_job_that_has_never_been_scheduled_is_due(self):
        """The `github-issue-resolver` shape: `enabled: true`, no `next_run_at`.

        Nothing had ever computed one because nothing had ever ticked the store.
        Treating it as not-due would leave the job in that state permanently —
        the exact failure being fixed.
        """
        self.assertEqual(self.due(job("resolver")), ["resolver"])
        self.assertEqual(self.due(job("resolver", None)), ["resolver"])

    def test_unparseable_timestamp_is_due(self):
        self.assertEqual(self.due(job("audit", "not-a-timestamp")), ["audit"])
        self.assertEqual(self.due(job("audit", 1754481600)), ["audit"])

    def test_naive_timestamp_is_read_as_local_time(self):
        past = (NOW - timedelta(days=1)).astimezone().replace(tzinfo=None)
        future = (NOW + timedelta(days=1)).astimezone().replace(tzinfo=None)
        self.assertEqual(self.due(job("past", past.isoformat())), ["past"])
        self.assertEqual(self.due(job("future", future.isoformat())), [])

    def test_corrupt_json_asks_for_a_tick(self):
        """Hermes' own loader repairs several malformed shapes under its lock.

        Skipping the profile until a human notices is the one outcome that
        reproduces the original bug, so hand it to Hermes instead.
        """
        self.assertEqual(self.due(raw="{not json"), ["<unparseable jobs.json>"])
        self.assertEqual(self.due(raw='{"jobs": "nonsense"}'), ["<unparseable jobs.json>"])

    def test_missing_store_is_not_due(self):
        self.assertEqual(pct.due_job_ids(self.d / "cron" / "jobs.json", NOW), [])

    def test_bare_list_roster_is_read(self):
        self.assertEqual(self.due(raw=json.dumps([job("audit", "2020-01-01T00:00:00+00:00")])), ["audit"])

    def test_only_the_due_ones_are_named(self):
        due = self.due(
            job("a", "2020-01-01T00:00:00+00:00"),
            job("b", "2030-01-01T00:00:00+00:00"),
            job("c", "2020-01-01T00:00:00+00:00", enabled=False),
            job("d"),
        )
        self.assertEqual(due, ["a", "d"])


class ProfileSelectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "profiles").mkdir()

    def profile(self, name) -> Path:
        p = self.home / "profiles" / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def test_finds_a_profile_with_due_work(self):
        write_store(self.profile("platform"), job("audit", "2020-01-01T00:00:00+00:00"))
        self.assertEqual(
            pct.profiles_with_due_jobs(self.home, NOW),
            [(self.home / "profiles" / "platform", ["audit"])],
        )

    def test_skips_a_profile_with_no_roster(self):
        """Cluster Agent profiles ship no jobs.json — only a cron/output dir.

        Starting an interpreter a minute for each of them would cost more than
        everything this script exists to run.
        """
        (self.profile("cluster-a") / "cron" / "output").mkdir(parents=True)
        self.assertEqual(pct.profiles_with_due_jobs(self.home, NOW), [])

    def test_skips_a_profile_with_nothing_due(self):
        write_store(self.profile("platform"), job("audit", "2030-01-01T00:00:00+00:00"))
        self.assertEqual(pct.profiles_with_due_jobs(self.home, NOW), [])

    def test_ignores_the_default_profiles_own_store(self):
        """The gateway's ticker already services $HERMES_HOME/cron. Ticking it
        again from inside one of its own jobs would recurse."""
        write_store(self.home, job("cluster-agent-reconcile", "2020-01-01T00:00:00+00:00"))
        self.assertEqual(pct.profiles_with_due_jobs(self.home, NOW), [])

    def test_no_profiles_dir_is_not_an_error(self):
        self.assertEqual(pct.profiles_with_due_jobs(Path(self._tmp.name) / "nope", NOW), [])

    def test_profiles_are_visited_in_name_order(self):
        for name in ("zeta", "alpha", "mu"):
            write_store(self.profile(name), job("audit", "2020-01-01T00:00:00+00:00"))
        self.assertEqual(
            [p.name for p, _ in pct.profiles_with_due_jobs(self.home, NOW)],
            ["alpha", "mu", "zeta"],
        )


class DispatchTest(unittest.TestCase):
    """End-to-end through main(), with a stub standing in for `hermes`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "data"
        (self.home / "profiles").mkdir(parents=True)
        self.record = self.tmp / "invocations"

        self._patch_env("HERMES_HOME", str(self.home))
        self._patch_env("PROFILE_CRON_TICK_RECORD", str(self.record))
        # Not a real pod: leave the credential-proxy branch of spawn_tick off
        # unless a test turns it on.
        self._patch_env("CREDENTIAL_PROXY_WORKSPACE_ROOT", None)
        self._patch(pct, "hermes_bin", lambda: self.fake_hermes())

    def _patch(self, obj, attr, value):
        original = getattr(obj, attr)
        setattr(obj, attr, value)
        self.addCleanup(setattr, obj, attr, original)

    def _patch_env(self, key, value):
        original = os.environ.get(key)

        def restore():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original

        self.addCleanup(restore)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def fake_hermes(self, exit_code=0):
        """A `hermes` that records how it was called and exits how it is told."""
        path = self.tmp / f"hermes-{exit_code}"
        path.write_text(
            "#!/bin/sh\n"
            # Fields 4 and 5 are the home target. The child resolves
            # `deliver=all` from its environment alone, so the only way to see
            # whether it can post anywhere is to have the stub report what it
            # was handed.
            'printf "%s|%s|%s|%s|%s\\n" "$HERMES_HOME" "$*" "$PWD" '
            '"$SLACK_HOME_CHANNEL" "$SLACK_HOME_CHANNEL_THREAD_ID" '
            '>> "$PROFILE_CRON_TICK_RECORD"\n'
            'echo "stub hermes says hello"\n'
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def profile(self, name, *jobs) -> Path:
        p = self.home / "profiles" / name
        p.mkdir(parents=True, exist_ok=True)
        write_store(p, *jobs)
        return p

    def run_main(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = pct.main()
        return code, buffer.getvalue()

    def invocations(self):
        if not self.record.exists():
            return []
        return [line.split("|") for line in self.record.read_text().splitlines()]

    def cwds(self):
        # Resolved on both sides: the stub reports $PWD, and on macOS the
        # temp dir reaches it through the /var -> /private/var symlink.
        return [str(Path(row[2]).resolve()) for row in self.invocations()]

    def capture_children(self) -> list:
        """Collect the children main() starts, so a test can reap them.

        main() hands off a tick that outlives its budget — deliberately — which
        leaves the stub running after the test returns. Real deployment wants
        exactly that; a test run does not want the stray process or the
        ResourceWarning its garbage collection prints.
        """
        started: list[subprocess.Popen] = []
        spawn = pct.spawn_tick

        def record(binary, profile_home, home_env=None):
            child = spawn(binary, profile_home, home_env)
            started.append(child)
            return child

        self._patch(pct, "spawn_tick", record)
        return started

    def set_home_channel(self, chat_id, thread_id=None):
        """Write a home channel the way `/sethome` does, into the root config."""
        home = {"chat_id": chat_id, "platform": "slack"}
        if thread_id:
            home["thread_id"] = thread_id
        (self.home / "config.yaml").write_text(
            yaml.safe_dump({"platforms": {"slack": {"home_channel": home}}}),
            encoding="utf-8",
        )

    def home_targets(self):
        return [(row[3], row[4]) for row in self.invocations()]

    @staticmethod
    def reap(children) -> None:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_quiet_minute_says_nothing(self):
        self.profile("platform", job("audit", "2030-01-01T00:00:00+00:00"))
        self.assertEqual(self.run_main(), (0, ""))
        self.assertEqual(self.invocations(), [])

    def test_ticks_each_due_profile_at_its_own_home(self):
        platform = self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))
        other = self.profile("other", job("sweep", None))
        self.profile("idle", job("later", "2030-01-01T00:00:00+00:00"))

        code, out = self.run_main()

        self.assertEqual(code, 0)
        homes = sorted(row[0] for row in self.invocations())
        self.assertEqual(homes, sorted([str(other), str(platform)]))
        self.assertTrue(all(row[1] == "cron tick" for row in self.invocations()))
        self.assertIn("platform: ticked 1 due job(s) — audit", out)
        self.assertIn("other: ticked 1 due job(s) — sweep", out)
        self.assertNotIn("idle", out)

    def test_child_output_lands_in_the_profiles_tick_log(self):
        platform = self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))
        self.run_main()
        self.assertIn("stub hermes says hello", (platform / "cron" / "tick.log").read_text())

    def test_a_failing_tick_is_reported_and_fails_the_run(self):
        platform = self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))
        self._patch(pct, "hermes_bin", lambda: self.fake_hermes(exit_code=3))

        code, out = self.run_main()

        self.assertEqual(code, 1, "a broken ticker must not report success")
        self.assertIn("exited 3", out)
        self.assertIn(str(platform / "cron" / "tick.log"), out)

    def test_a_missing_hermes_is_reported_and_fails_the_run(self):
        def boom():
            raise FileNotFoundError("no `hermes` anywhere")

        self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))
        self._patch(pct, "hermes_bin", boom)

        code, out = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("cannot tick platform", out)

    def test_runs_from_the_credential_proxy_workspace_root_when_there_is_one(self):
        """The gcloud/kubectl shims reject any call made from outside it, and the
        cwd a no_agent script inherits is the scripts dir — outside."""
        workspace = self.tmp / "workspace"
        workspace.mkdir()
        self._patch_env("CREDENTIAL_PROXY_WORKSPACE_ROOT", str(workspace))
        self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))

        self.run_main()

        self.assertEqual(self.cwds(), [str(workspace.resolve())])

    def test_falls_back_to_the_profile_home_when_there_is_no_workspace_root(self):
        platform = self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))
        self.run_main()
        self.assertEqual(self.cwds(), [str(platform.resolve())])

    def test_a_long_tick_is_handed_off_rather_than_waited_out(self):
        """A fleet audit runs for minutes; the dispatcher must not.

        The gateway ticker skips a job that is still in flight, so a dispatcher
        that waited for a 20-minute audit would cancel its own next twenty
        dispatches and starve every other profile with it. A tick still alive
        at the deadline is reported and left alone — `next_run_at` was advanced
        before it started, and it holds a per-job lock for the job it is
        running, so the next spawn re-dispatches nothing for that job while
        still dispatching anything else that has come due.
        """
        slow = self.tmp / "hermes-slow"
        slow.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        slow.chmod(slow.stat().st_mode | stat.S_IXUSR)
        self._patch(pct, "hermes_bin", lambda: slow)
        self._patch_env(pct.BUDGET_ENV, "1")
        self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))
        # The script hands the child off, which is the behaviour under test; the
        # test suite still has to reap it rather than leave a `sleep` behind.
        self.addCleanup(self.reap, self.capture_children())

        code, out = self.run_main()

        self.assertEqual(code, 0, "a tick that is merely slow is not a failed run")
        self.assertIn("still running after 1s", out)
        self.assertIn("handed off", out)

    def test_summary_counts_the_ids_it_does_not_name(self):
        self.profile("platform", *(job(f"j{n}", "2020-01-01T00:00:00+00:00") for n in range(8)))
        _, out = self.run_main()
        self.assertIn("ticked 8 due job(s) — j0, j1, j2, j3, j4, +3 more", out)

    # --- the home target, as the spawned child actually receives it ---------

    def test_the_child_is_given_the_home_channel_but_never_its_thread(self):
        # The channel has to cross the spawn boundary; the thread must not.
        # See `home_target_env` — cron briefs post flat.
        self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))
        self.set_home_channel("D0BKGRBM6RH", "1786243706.858639")

        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.home_targets(), [("D0BKGRBM6RH", "")])

    def test_the_child_gets_it_even_though_the_parent_was_scrubbed(self):
        """The regression itself: inheriting alone is what does not work.

        `build_subprocess_env` strips `SLACK_HOME_CHANNEL` before this script
        starts, so the parent genuinely does not have it. Passing `os.environ`
        through re-exports that hole.
        """
        self._patch_env("SLACK_HOME_CHANNEL", None)
        self._patch_env("SLACK_HOME_CHANNEL_THREAD_ID", None)
        self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))
        self.set_home_channel("D0BKGRBM6RH", "1786243706.858639")

        self.run_main()

        self.assertEqual(self.home_targets(), [("D0BKGRBM6RH", "")])

    def test_the_saved_home_wins_over_a_stale_inherited_one(self):
        # config.yaml is what `/sethome` wrote last. An inherited value that
        # survived scrubbing came from an earlier one.
        self._patch_env("SLACK_HOME_CHANNEL", "C0OLDCHANNEL")
        self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))
        self.set_home_channel("D0BKGRBM6RH")

        self.run_main()

        self.assertEqual(self.home_targets()[0][0], "D0BKGRBM6RH")

    def test_a_thread_less_home_does_not_inherit_a_stale_thread(self):
        """Why both keys move together.

        `SLACK_HOME_CHANNEL_THREAD_ID` is *not* on the blocklist, so it does
        survive the spawn. Re-homing to a plain channel while a thread id from
        the previous home is still in the environment would deliver into a
        thread the new home knows nothing about.
        """
        self._patch_env("SLACK_HOME_CHANNEL_THREAD_ID", "1786243706.858639")
        self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))
        self.set_home_channel("C0DEADBEEF")

        self.run_main()

        self.assertEqual(self.home_targets(), [("C0DEADBEEF", "")])

    def test_every_profile_gets_the_same_home(self):
        self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))
        self.profile("other", job("sweep", None))
        self.set_home_channel("D0BKGRBM6RH")

        self.run_main()

        self.assertEqual(sorted(self.home_targets()), [("D0BKGRBM6RH", "")] * 2)

    def test_no_home_set_still_ticks(self):
        # Delivery is not the point of a tick: jobs with an explicit target
        # must still run when nobody has set a home.
        self._patch_env("SLACK_HOME_CHANNEL", None)
        self.profile("platform", job("audit", "2020-01-01T00:00:00+00:00"))

        code, out = self.run_main()

        self.assertEqual(code, 0)
        self.assertEqual(len(self.invocations()), 1)
        self.assertIn("platform: ticked 1 due job(s)", out)


class TickLogRotationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log = Path(self._tmp.name) / "tick.log"

    def test_rotates_once_over_the_cap(self):
        self.log.write_bytes(b"x" * (pct.LOG_MAX_BYTES + 1))
        pct._rotate(self.log)
        self.assertFalse(self.log.exists())
        self.assertTrue(self.log.with_name("tick.log.1").exists())

    def test_leaves_a_small_log_alone(self):
        self.log.write_text("short")
        pct._rotate(self.log)
        self.assertEqual(self.log.read_text(), "short")


class ShippedRosterTest(unittest.TestCase):
    """The wiring, not the logic.

    A ticker script with no cron entry to run it is indistinguishable from the
    bug it fixes, and an entry naming a script the image does not carry fails as
    "Script not found" once a minute inside a job whose output nobody reads.
    """

    def setUp(self):
        self.jobs = json.loads(ROOT_ROSTER.read_text(encoding="utf-8"))["jobs"]

    def test_the_ticker_is_registered_on_the_profile_that_actually_ticks(self):
        entries = [j for j in self.jobs if j.get("script") == "profile_cron_tick.py"]
        self.assertEqual(len(entries), 1, "exactly one entry must run the named-profile ticker")
        entry = entries[0]
        self.assertTrue(entry.get("enabled"), "a disabled ticker leaves every named profile inert")
        self.assertTrue(entry.get("no_agent"), "the ticker is a plain subprocess; it must not prompt the model")
        self.assertEqual(
            entry.get("schedule"),
            {"kind": "cron", "expr": "* * * * *", "display": "* * * * *"},
            "the granularity of every named profile's cron is this expression",
        )

    def test_display_mirrors_expr_on_every_job(self):
        """`display` is a hand-written second copy of `expr` and nothing reconciles them.

        For `kind: "cron"` the runtime itself sets `display` to the raw
        expression (`"display": schedule` in `cron/jobs.py`); `every {minutes}m`
        is the form it generates for `kind: "interval"`. Three jobs here carried
        `"every 1m"` -- the interval wording on a cron job -- which was true of
        `* * * * *` by luck and would have kept reading as "every minute" after
        the expression changed. Nothing in the scheduler or in
        `scripts/generate_docs.py` reads `display` for a cron job, so the drift
        is invisible until a human trusts it.
        """
        drifted = [
            (j.get("id"), j["schedule"].get("expr"), j["schedule"].get("display"))
            for j in self.jobs
            if j.get("schedule", {}).get("kind") == "cron"
            and j["schedule"].get("display") != j["schedule"].get("expr")
        ]
        self.assertEqual(drifted, [], "display must be a verbatim copy of expr")

    def test_no_job_on_this_roster_uses_an_interval_schedule(self):
        """An `interval` schedule fires at half its configured rate on this image.

        Hermes re-anchors an interval job to the moment the last run
        *finished*: `mark_job_run` stamps `last_run_at` with the completion
        time and then overwrites the `next_run_at` that `tick()` had already
        advanced, recomputing it as `last_run_at + minutes` (`cron/jobs.py`,
        the `kind == "interval"` branch of `compute_next_run`). The gateway
        ticker meanwhile sleeps a fixed sixty seconds *after* each tick
        returns, so its wake grid is anchored a little earlier than every
        completion. The new due time therefore lands just past the next wake,
        `next_run_dt <= now` is false, and the job fires on the wake after
        that -- forever, because each fire re-anchors to a point later than
        the loop's own reference.

        Measured on a live pod before this was fixed: of 557 gaps between
        `profile-cron-tick` runs, 497 were 120s and only 47 were 60s.

        A cron expression is immune. Its branch bases croniter on the same
        completion timestamp but snaps forward to the next wall-clock minute,
        which lands *before* the next wake instead of after it. Do not
        "simplify" these back to `{kind: interval, minutes: 1}`.
        """
        offenders = [
            j.get("id")
            for j in self.jobs
            if isinstance(j.get("schedule"), dict)
            and j["schedule"].get("kind") == "interval"
        ]
        self.assertEqual(
            offenders,
            [],
            f"{offenders}: an `interval` schedule fires at half its configured rate on this "
            "image -- use a cron expression instead (see this test's docstring)",
        )

    def test_the_entrypoint_forces_the_ticker_onto_an_existing_volume(self):
        # Shipping the entry is only half of it. `cp -ru` can never refresh
        # cron/jobs.json — the scheduler rewrites it on every tick, so the
        # volume's copy always looks newer — which means on every PVC that
        # already exists the new job arrives only through the entrypoint's
        # merge, and only if the merge's id allowlist names it. Miss this and
        # the fix ships to fresh installs alone: a silent non-fix, which is the
        # failure class this whole change is about.
        entrypoint = (REPO / "deploy" / "shared" / "docker-entrypoint.sh").read_text(
            encoding="utf-8"
        )
        allowlists = re.findall(r"--cron-jobs\s+\"([^\"]*)\"", entrypoint)
        self.assertTrue(allowlists, "no --cron-jobs allowlist in the entrypoint")
        self.assertTrue(
            any("profile-cron-tick" in ids.split() for ids in allowlists),
            "the entrypoint merges the default-profile roster but not the ticker's id",
        )

    def test_every_no_agent_job_names_a_script_the_image_carries(self):
        for entry in self.jobs:
            if not entry.get("no_agent"):
                continue
            name = entry.get("script")
            self.assertTrue(name, f"{entry.get('id')}: no_agent with no script never runs")
            self.assertTrue(
                any((d / name).is_file() for d in SCRIPT_DIRS),
                f"{entry.get('id')}: {name} is in neither directory copied to $HERMES_HOME/scripts",
            )


class HomeTargetEnvTest(unittest.TestCase):
    """The value `/sethome` writes, read back from the file it writes it to.

    The failure this covers was silent in the worst way: `/sethome` reported
    success, the channel really was saved, and every scheduled job still posted
    nowhere — because `SLACK_HOME_CHANNEL` is on Hermes' provider-env blocklist
    and the cron spawn strips it. Nothing on the write side was wrong, so
    nothing on the write side can be asserted to catch a regression. Only the
    spawned environment can.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def write_config(self, mapping):
        (self.home / "config.yaml").write_text(yaml.safe_dump(mapping), encoding="utf-8")

    def test_the_channel_is_recovered_and_its_thread_is_deliberately_dropped(self):
        # `/sethome` records whichever thread it was typed in. A cron brief is
        # not a reply to that conversation, and forwarding the thread files
        # every scheduled report under one ageing thread where only the first
        # is visible. Flat is a property of cron delivery, not of where the
        # command happened to be run.
        self.write_config(
            {
                "platforms": {
                    "slack": {
                        "home_channel": {
                            "chat_id": "D0BKGRBM6RH",
                            "thread_id": "1786243706.858639",
                        }
                    }
                }
            }
        )
        self.assertEqual(
            pct.home_target_env(self.home),
            {
                "SLACK_HOME_CHANNEL": "D0BKGRBM6RH",
                "SLACK_HOME_CHANNEL_THREAD_ID": "",
            },
        )

    def test_a_home_with_no_thread_blanks_the_thread_rather_than_omitting_it(self):
        # Setting home from a channel rather than a thread is the common case.
        # The empty value is the instruction to drop the key from the child's
        # environment: omitting it would leave a stale thread id inherited.
        self.write_config(
            {"platforms": {"slack": {"home_channel": {"chat_id": "C0DEADBEEF"}}}}
        )
        self.assertEqual(
            pct.home_target_env(self.home),
            {"SLACK_HOME_CHANNEL": "C0DEADBEEF", "SLACK_HOME_CHANNEL_THREAD_ID": ""},
        )

    def test_a_non_string_chat_id_is_still_usable_as_an_env_value(self):
        self.write_config({"platforms": {"slack": {"home_channel": {"chat_id": 12345}}}})
        self.assertEqual(
            pct.home_target_env(self.home),
            {"SLACK_HOME_CHANNEL": "12345", "SLACK_HOME_CHANNEL_THREAD_ID": ""},
        )

    def test_no_home_set_yields_nothing_rather_than_an_empty_channel(self):
        # An empty SLACK_HOME_CHANNEL would satisfy `os.getenv` truthiness
        # checks nowhere but would still overwrite an inherited value.
        self.write_config({"platforms": {"slack": {}}})
        self.assertEqual(pct.home_target_env(self.home), {})

    def test_a_missing_config_is_not_an_error(self):
        self.assertEqual(pct.home_target_env(self.home), {})

    def test_a_corrupt_config_is_not_an_error(self):
        # A tick must still happen: jobs with an explicit target deliver fine.
        (self.home / "config.yaml").write_text("platforms: [oh: no\n", encoding="utf-8")
        self.assertEqual(pct.home_target_env(self.home), {})

    def test_an_empty_config_is_not_an_error(self):
        (self.home / "config.yaml").write_text("", encoding="utf-8")
        self.assertEqual(pct.home_target_env(self.home), {})

    def test_a_null_platforms_key_is_not_an_error(self):
        (self.home / "config.yaml").write_text("platforms:\n", encoding="utf-8")
        self.assertEqual(pct.home_target_env(self.home), {})

    def test_no_shape_of_config_can_stop_the_tick(self):
        """Well-formed YAML of the wrong shape used to take the whole schedule down.

        ``yaml.safe_load`` succeeds on every document below, so the ``except``
        above never sees them; the reads that followed it raised
        ``AttributeError`` instead, which escapes ``main`` and means *no*
        profile ticks — every minute, until someone edits the file. The
        docstring's promise that this is "never the reason a tick does not
        happen" has to hold for a bad shape and not just for a bad parse.
        """
        for label, document in (
            ("a scalar document", "just-a-string\n"),
            ("a list document", "- a\n- b\n"),
            ("platforms as a string", 'platforms: "slack"\n'),
            ("platforms as a list", "platforms:\n  - slack\n"),
            ("the platform as a string", 'platforms:\n  slack: "on"\n'),
            ("home_channel as a string", 'platforms:\n  slack:\n    home_channel: "C123"\n'),
        ):
            with self.subTest(label):
                (self.home / "config.yaml").write_text(document, encoding="utf-8")
                self.assertEqual(pct.home_target_env(self.home), {})

    def test_a_good_home_still_survives_a_junk_sibling(self):
        # The guard degrades one unreadable branch, not the whole read.
        (self.home / "config.yaml").write_text(
            'platforms:\n  google_chat: "spaces/AAA"\n'
            "  slack:\n    home_channel:\n      chat_id: C123\n      thread_id: T9\n",
            encoding="utf-8",
        )
        # Only the channel id is asserted. What happens to the thread id is a
        # separate policy with its own tests above; pinning it here as well
        # would make this test fail for a reason that has nothing to do with
        # the junk sibling it exists to cover.
        restored = pct.home_target_env(self.home)
        self.assertEqual("C123", restored.get("SLACK_HOME_CHANNEL"))

    def test_google_chat_is_restored_the_same_way_slack_is(self):
        # It was missing on the claim that `GOOGLE_CHAT_HOME_CHANNEL` escapes
        # the provider-env blocklist. It does not: the blocklist is derived at
        # import and blocks the whole `messaging` category, which is the
        # default bucket for every plugin-declared variable.
        self.write_config(
            {
                "platforms": {
                    "google_chat": {"home_channel": {"chat_id": "spaces/20MORyAAAAE"}}
                }
            }
        )
        self.assertEqual(
            pct.home_target_env(self.home),
            {
                "GOOGLE_CHAT_HOME_CHANNEL": "spaces/20MORyAAAAE",
                "GOOGLE_CHAT_HOME_CHANNEL_THREAD_ID": "",
            },
        )

    def test_the_authorization_list_is_never_restored(self):
        """`*_ALLOWED_USERS` is blocklisted too, and stays that way.

        It sits beside the home channel in the same `messaging` category, but
        it is an authorization list rather than a destination — a different
        risk class, and nothing in cron delivery needs it. Restoring it would
        be scope creep past what this function argues is safe.
        """
        self.write_config(
            {
                "platforms": {
                    "google_chat": {
                        "home_channel": {"chat_id": "spaces/AAA"},
                        "allowed_users": ["someone@example.com"],
                    },
                }
            }
        )
        restored = pct.home_target_env(self.home)
        self.assertEqual(
            {"GOOGLE_CHAT_HOME_CHANNEL", "GOOGLE_CHAT_HOME_CHANNEL_THREAD_ID"},
            set(restored),
        )
        self.assertNotIn("GOOGLE_CHAT_ALLOWED_USERS", restored)


if __name__ == "__main__":
    unittest.main()
