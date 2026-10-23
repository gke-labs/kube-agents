"""Unit tests for profile_scaffold.overlay_template (the image -> PVC force-sync).

Run: python3 -m unittest agents.platform.scripts.test_profile_scaffold

This is the mechanism the entrypoint uses to keep an EXISTING platform profile
tracking the image. It replaced a `for f in ...; do [ -f ... ] && cp -f; done`
loop, which could only ever copy files: `[ -f ]` is false for a directory, so
cron/, skills/, and governance/ were silently skipped on every upgrade and the
agent shipped a CAPABILITIES.md describing machinery that was not there. The
directory cases below are that regression, not a formality.
"""

import io
import fcntl
import shutil
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_scaffold as ps  # noqa: E402

ITEMS = ("config.yaml", "SOUL.md", "AGENTS.md", "CAPABILITIES.md", "cron", "skills", "governance")
SHIPPED_ROSTER = Path(__file__).resolve().parents[1] / "cron" / "jobs.json"


def job(job_id, **extra):
    """A cron entry shaped like the ones agents/platform/cron/jobs.json ships."""
    return {
        "id": job_id,
        "name": job_id.replace("-", " ").title(),
        "schedule": {"kind": "cron", "expr": "20 6 * * *"},
        "prompt": f"run {job_id}",
        "risk": "low",
        "enabled": True,
        **extra,
    }


class OverlayTemplateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.template = root / "template"
        self.home = root / "home"
        for path in (self.template, self.home):
            path.mkdir()
        write(self.template / "SOUL.md", "image persona")
        write(self.template / "CAPABILITIES.md", "image capabilities")
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit")]}))
        write(self.template / "skills" / "fleet-audit" / "SKILL.md", "image skill")
        write(self.template / "governance" / "compliance_audit_sop.md", "image sop")

    def tearDown(self):
        self.temp_dir.cleanup()

    def jobs_on_disk(self):
        return json.loads((self.home / "cron" / "jobs.json").read_text())["jobs"]

    def test_named_directories_are_overlaid_not_skipped(self):
        ps.overlay_template(self.home, self.template, None, ITEMS)
        self.assertEqual(["audit"], [j["id"] for j in self.jobs_on_disk()])
        self.assertEqual("image skill", (self.home / "skills" / "fleet-audit" / "SKILL.md").read_text())
        self.assertEqual("image sop", (self.home / "governance" / "compliance_audit_sop.md").read_text())

    def test_the_image_copy_wins_over_what_is_already_on_the_volume(self):
        write(self.home / "SOUL.md", "stale persona")
        write(self.home / "skills" / "fleet-audit" / "SKILL.md", "stale skill")
        ps.overlay_template(self.home, self.template, None, ITEMS)
        self.assertEqual("image persona", (self.home / "SOUL.md").read_text())
        self.assertEqual("image skill", (self.home / "skills" / "fleet-audit" / "SKILL.md").read_text())

    def test_runtime_state_and_removed_entries_survive(self):
        # The overlay adds and overwrites; it never prunes. Per-profile runtime
        # state has to survive an upgrade, and the price of that guarantee is
        # that a skill withdrawn from the image also survives — a known limit
        # recorded at the sync site in deploy/shared/docker-entrypoint.sh.
        write(self.home / "USER.md", "operator notes")
        write(self.home / "skills" / "withdrawn" / "SKILL.md", "no longer in the image")
        ps.overlay_template(self.home, self.template, None, ITEMS)
        self.assertEqual("operator notes", (self.home / "USER.md").read_text())
        self.assertTrue((self.home / "skills" / "withdrawn" / "SKILL.md").exists())

    def test_an_item_the_template_does_not_ship_is_not_an_error(self):
        # config.yaml and AGENTS.md are named by the entrypoint but a template
        # need not carry every one of them.
        ps.overlay_template(self.home, self.template, None, ITEMS)
        self.assertFalse((self.home / "config.yaml").exists())
        self.assertFalse((self.home / "AGENTS.md").exists())

    def test_no_item_list_overlays_the_whole_template(self):
        ps.overlay_template(self.home, self.template)
        self.assertTrue((self.home / "cron" / "jobs.json").exists())
        self.assertTrue((self.home / "CAPABILITIES.md").exists())

    def test_a_missing_template_is_a_hard_error(self):
        with self.assertRaises(SystemExit):
            ps.overlay_template(self.home, self.template.parent / "absent", None, ITEMS)

    def plugins_dir(self):
        plugins = Path(self.temp_dir.name) / "plugins"
        write(plugins / "hermes_otel" / "hooks.py", "image hook")
        return plugins

    def test_plugins_are_overlaid_and_plugin_runtime_state_survives(self):
        # The entrypoint passes --plugins on the force-sync path too, not only
        # on first scaffold, so an added or repaired plugin reaches a profile
        # that already exists. That is safe only because this copy adds and
        # overwrites without pruning: hermes_otel writes its event database
        # inside its own plugin directory, and the image ships no such file.
        write(self.home / "plugins" / "hermes_otel" / "live.db", "1028 events")
        ps.overlay_template(self.home, self.template, self.plugins_dir(), ITEMS)
        self.assertEqual("image hook", (self.home / "plugins" / "hermes_otel" / "hooks.py").read_text())
        self.assertEqual("1028 events", (self.home / "plugins" / "hermes_otel" / "live.db").read_text())

    def test_a_failed_plugin_copy_warns_and_leaves_the_rest_of_the_overlay_standing(self):
        # Two containers of the same pod ran this concurrently against one RWO
        # volume and both aborted here with shutil.Error, each naming different
        # files. The entrypoint serialises them now, but the persona, config,
        # skills, cron and governance above have already landed by this point,
        # and raising made the caller report the whole scaffold as failed.
        plugins = self.plugins_dir()
        real_copytree = shutil.copytree

        # `*args` because copytree recurses through the module global, which is
        # the name being patched — the inner calls arrive fully positional.
        def explode_only_on_plugins(src, dst, *args, **kwargs):
            if Path(src) == plugins:
                raise shutil.Error([(str(src), str(dst), "[Errno 2] No such file or directory")])
            return real_copytree(src, dst, *args, **kwargs)

        stderr = io.StringIO()
        with unittest.mock.patch.object(ps.shutil, "copytree", explode_only_on_plugins):
            with redirect_stderr(stderr):
                ps.overlay_template(self.home, self.template, plugins, ITEMS)

        self.assertIn("WARN", stderr.getvalue())
        self.assertIn("plugins", stderr.getvalue())
        self.assertEqual("image persona", (self.home / "SOUL.md").read_text())
        self.assertEqual("image skill", (self.home / "skills" / "fleet-audit" / "SKILL.md").read_text())
        self.assertEqual(["audit"], [j["id"] for j in self.jobs_on_disk()])


class CronStoreMergeTest(unittest.TestCase):
    """cron/jobs.json is image-owned configuration and runtime state at once.

    A plain copytree took both, on every restart: a job the operator had added
    to the store simply disappeared, and each job's run history went with it.
    Losing `last_run_at` re-fires a one-shot — that field is its already-ran
    guard — while a recurring job fails the other way, its wiped `next_run_at`
    recomputed from now so a merely-late audit is skipped rather than caught
    up. These pin the per-key rule the merge replaced it with.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.template = root / "template"
        self.home = root / "home"
        for path in (self.template, self.home):
            path.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def overlay(self, image_jobs, live_jobs):
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": image_jobs}))
        write(self.home / "cron" / "jobs.json", json.dumps({"jobs": live_jobs}))
        ps.overlay_template(self.home, self.template, None, ("cron",))
        return json.loads((self.home / "cron" / "jobs.json").read_text())["jobs"]

    def test_run_history_survives_the_upgrade(self):
        merged = self.overlay(
            [job("audit", prompt="new prompt")],
            [job("audit", prompt="old prompt", last_run="2026-08-03T06:20:00Z")],
        )
        self.assertEqual(1, len(merged))
        self.assertEqual("new prompt", merged[0]["prompt"])
        self.assertEqual("2026-08-03T06:20:00Z", merged[0]["last_run"])

    def test_the_image_decides_whether_a_watchdog_is_enabled(self):
        # "Edit cron/jobs.json, flip enabled to false, and redeploy" is the
        # documented way to turn a watchdog off. A merge that let the volume
        # keep its own `enabled` would make that instruction a no-op on every
        # cluster that had already run the job once.
        merged = self.overlay([job("audit", enabled=False)], [job("audit", enabled=True)])
        self.assertIs(False, merged[0]["enabled"])

    def test_the_image_decides_where_a_report_is_delivered(self):
        # This is what makes moving the whole roster onto `deliver: "chat"` a
        # one-field edit rather than a migration script. Every live volume
        # already carries these jobs at `"all"`, and nothing on the volume can
        # be reached by hand; the next pod start is what rewrites them. The
        # default profile is the other way round — `cron_jobs_sync.py` lists
        # `deliver` in RUNTIME_WINS, because onboarding rewrites it to `origin`
        # on the delivery job — so this rule holds for named profiles only.
        merged = self.overlay([job("audit", deliver="chat")], [job("audit", deliver="all")])
        self.assertEqual("chat", merged[0]["deliver"])

    def test_the_image_decides_the_risk_tier(self):
        merged = self.overlay([job("audit", risk="low")], [job("audit", risk="high")])
        self.assertEqual("low", merged[0]["risk"])

    def test_a_job_the_image_does_not_ship_is_kept(self):
        merged = self.overlay([job("audit")], [job("audit"), job("operator-added")])
        self.assertEqual(["audit", "operator-added"], [j["id"] for j in merged])

    def test_an_unannotated_operator_job_is_backfilled_with_low_risk(self):
        legacy = {"id": "legacy-custom", "schedule": {"kind": "cron", "expr": "* * * * *"}}
        merged = self.overlay([job("audit")], [job("audit"), legacy])
        custom = next(j for j in merged if j["id"] == "legacy-custom")
        self.assertEqual("low", custom.get("risk"))
        self.assertNotIn("risk", legacy, "the merge must not mutate caller input in-place")

    def test_cron_jobs_lock_held_during_overlay(self):
        """Issue #3179: Hermes' .jobs.lock must be held exclusively while merging jobs.json."""
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit")]}))
        write(self.home / "cron" / "jobs.json", json.dumps({"jobs": [job("audit", last_run="yesterday")]}))

        lock_held = False
        lock_file = self.home / "cron" / ".jobs.lock"

        orig_merge = ps.merge_cron_store

        def probe_merge(*args, **kwargs):
            nonlocal lock_held
            self.assertTrue(lock_file.is_file(), ".jobs.lock must exist while merge runs")
            with open(lock_file, "r") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_held = False
                except (BlockingIOError, OSError):
                    lock_held = True
            return orig_merge(*args, **kwargs)

        with unittest.mock.patch.object(ps, "merge_cron_store", side_effect=probe_merge):
            ps.overlay_template(self.home, self.template, None, ("cron",))

        self.assertTrue(lock_held, ".jobs.lock was NOT held exclusively during overlay")

    def test_cron_pause_during_overlay_not_lost(self):
        """Issue #3179: A cron pause landing while gateway starts must survive the merge."""
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit", prompt="image prompt")]}))
        write(self.home / "cron" / "jobs.json", json.dumps({"jobs": [job("audit", prompt="old prompt", state="running")]}))

        lock_file = self.home / "cron" / ".jobs.lock"
        jobs_path = self.home / "cron" / "jobs.json"
        orig_merge = ps._merge_after_overlay
        pause_thread = None
        about_to_lock = threading.Event()

        def pause_worker():
            fd = open(lock_file, "a+", encoding="utf-8")
            try:
                about_to_lock.set()
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    data = json.loads(jobs_path.read_text(encoding="utf-8"))
                    data["jobs"][0]["state"] = "paused"
                    data["jobs"][0]["paused_at"] = "2026-10-15T07:23:22Z"
                    jobs_path.write_text(json.dumps(data), encoding="utf-8")
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                fd.close()

        def inject_pause_mid_merge(*args, **kwargs):
            nonlocal pause_thread
            pause_thread = threading.Thread(target=pause_worker)
            pause_thread.start()
            self.assertTrue(about_to_lock.wait(timeout=5.0))
            # If overlay_template holds .jobs.lock, pause_worker blocks in flock(LOCK_EX), so join times out.
            # If overlay_template does NOT hold .jobs.lock, pause_worker finishes immediately, writing mid-merge
            # before orig_merge runs, which orig_merge's os.replace clobbers.
            pause_thread.join(timeout=0.05)
            return orig_merge(*args, **kwargs)

        with unittest.mock.patch.object(ps, "_merge_after_overlay", side_effect=inject_pause_mid_merge):
            ps.overlay_template(self.home, self.template, None, ("cron",))

        if pause_thread:
            pause_thread.join()

        merged = json.loads((self.home / "cron" / "jobs.json").read_text())["jobs"]
        self.assertEqual(merged[0]["state"], "paused", "pause state was lost during startup merge")
        self.assertEqual(merged[0]["paused_at"], "2026-10-15T07:23:22Z")
        self.assertEqual(merged[0]["prompt"], "image prompt", "image prompt was not updated")

    def test_overlay_template_copies_non_cron_outside_lock(self):
        """Non-cron template items (skills, governance) must be copied outside .jobs.lock."""
        write(self.template / "skills" / "foo.md", "skill content")
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit")]}))
        write(self.home / "cron" / "jobs.json", json.dumps({"jobs": [job("audit")]}))

        lock_path = self.home / "cron" / ".jobs.lock"
        copied_skills_during_lock = False
        orig_copytree = shutil.copytree

        def probe_copytree(src, dst, *args, **kwargs):
            nonlocal copied_skills_during_lock
            if "skills" in str(src):
                if lock_path.is_file():
                    try:
                        with open(lock_path, "a+") as probe_fd:
                            try:
                                fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                fcntl.flock(probe_fd, fcntl.LOCK_UN)
                            except (BlockingIOError, OSError):
                                copied_skills_during_lock = True
                    except OSError:
                        pass
            return orig_copytree(src, dst, *args, **kwargs)

        with unittest.mock.patch("shutil.copytree", side_effect=probe_copytree):
            ps.overlay_template(self.home, self.template, None, ("skills", "cron"))

        self.assertFalse(copied_skills_during_lock, "shutil.copytree on skills was called while .jobs.lock was held")

    def test_cron_jobs_lock_non_contention_oserror_fails_open_immediately(self):
        """Non-BlockingIOError OSError (e.g. ENOLCK/EOPNOTSUPP) fails open immediately without 15s wait."""
        import errno
        lock_dir = self.home / "cron"
        lock_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        with unittest.mock.patch("fcntl.flock", side_effect=OSError(errno.ENOLCK, "No record locks available")):
            with ps.cron_jobs_lock(lock_dir, timeout=5.0):
                pass
        duration = time.monotonic() - t0
        self.assertLess(duration, 1.0, f"lock waited {duration}s on permanent OSError instead of failing open immediately")

    def test_cron_jobs_lock_timeout_fallback(self):
        """When .jobs.lock is held and times out, overlay_template still completes."""
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit", prompt="new")]}))
        write(self.home / "cron" / "jobs.json", json.dumps({"jobs": [job("audit", prompt="old")]}))

        lock_file = self.home / "cron" / ".jobs.lock"
        (self.home / "cron").mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_file, "a+")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            with unittest.mock.patch.object(ps, "DEFAULT_JOBS_LOCK_TIMEOUT_SECONDS", 0.05):
                ps.overlay_template(self.home, self.template, None, ("cron",))
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

        merged = json.loads((self.home / "cron" / "jobs.json").read_text())["jobs"]
        self.assertEqual(merged[0]["prompt"], "new")

    def test_an_operator_job_with_explicit_risk_is_preserved(self):
        custom_high = {"id": "custom-high", "schedule": {"kind": "cron", "expr": "* * * * *"}, "risk": "high"}
        merged = self.overlay([job("audit")], [job("audit"), custom_high])
        custom = next(j for j in merged if j["id"] == "custom-high")
        self.assertEqual("high", custom.get("risk"))

    def test_a_job_withdrawn_from_the_image_is_not_pruned(self):
        # The cost of the rule above, stated rather than discovered: nothing
        # here can tell an operator's own job from one this release deleted,
        # and the overlay does not prune. Removing an entry from the shipped
        # jobs.json therefore does not stop it firing on a cluster that already
        # has it. Shipping it with `enabled: false` does, so retiring a
        # watchdog takes two steps: ship it disabled, let every live cluster
        # merge that state, and only then drop the entry — from that point the
        # volume's own copy keeps it off with no help from the image. That is
        # the path the five retired watchdogs took out of agents/platform/cron/.
        merged = self.overlay([job("audit")], [job("audit"), job("withdrawn")])
        self.assertEqual(["audit", "withdrawn"], [j["id"] for j in merged])

    def test_a_new_job_arrives_with_no_prior_state(self):
        merged = self.overlay([job("audit"), job("brand-new")], [job("audit")])
        self.assertEqual(["audit", "brand-new"], [j["id"] for j in merged])

    def test_a_first_scaffold_just_takes_the_image(self):
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit")]}))
        ps.overlay_template(self.home, self.template, None, ("cron",))
        store = json.loads((self.home / "cron" / "jobs.json").read_text())
        self.assertEqual(["audit"], [j["id"] for j in store["jobs"]])

    def test_an_unreadable_store_lets_the_image_copy_stand(self):
        # Half a jobs.json is not a reason to leave the profile with no cron
        # roster at all.
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit")]}))
        write(self.home / "cron" / "jobs.json", '{"jobs": [{"id": "aud')
        ps.overlay_template(self.home, self.template, None, ("cron",))
        store = json.loads((self.home / "cron" / "jobs.json").read_text())
        self.assertEqual(["audit"], [j["id"] for j in store["jobs"]])

    def test_top_level_keys_follow_the_same_rule(self):
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [], "version": 2}))
        write(
            self.home / "cron" / "jobs.json",
            json.dumps({"jobs": [], "version": 1, "scheduler_state": {"tick": 9}}),
        )
        ps.overlay_template(self.home, self.template, None, ("cron",))
        store = json.loads((self.home / "cron" / "jobs.json").read_text())
        self.assertEqual(2, store["version"])
        self.assertEqual({"tick": 9}, store["scheduler_state"])

    def test_a_named_id_list_leaves_every_other_shipped_job_to_the_volume(self):
        # What the default profile's merge needs. Two jobs on that roster
        # remove themselves once first-run onboarding is delivered
        # (`bootstrap_delivery.py` calls `remove_job`), so an unfiltered merge
        # would put them back on the next restart — polling every minute
        # forever to no-op on a marker. Only the named id is forced.
        write(
            self.template / "cron" / "jobs.json",
            json.dumps({"jobs": [job("profile-cron-tick"), job("bootstrap-inventory-scan")]}),
        )
        write(self.home / "cron" / "jobs.json", json.dumps({"jobs": [job("reconcile")]}))
        ps.overlay_template(self.home, self.template, None, ("cron",), ("profile-cron-tick",))
        merged = json.loads((self.home / "cron" / "jobs.json").read_text())["jobs"]
        self.assertEqual(["profile-cron-tick", "reconcile"], [j["id"] for j in merged])

    def test_a_named_id_still_takes_the_image_definition_and_keeps_its_state(self):
        write(
            self.template / "cron" / "jobs.json",
            json.dumps({"jobs": [job("profile-cron-tick", prompt="new")]}),
        )
        write(
            self.home / "cron" / "jobs.json",
            json.dumps(
                {"jobs": [job("profile-cron-tick", prompt="old", last_run="2026-08-05T00:00:00Z")]}
            ),
        )
        ps.overlay_template(self.home, self.template, None, ("cron",), ("profile-cron-tick",))
        merged = json.loads((self.home / "cron" / "jobs.json").read_text())["jobs"]
        self.assertEqual("new", merged[0]["prompt"])
        self.assertEqual("2026-08-05T00:00:00Z", merged[0]["last_run"])

    def test_an_empty_id_list_forces_nothing(self):
        # `--cron-jobs ""` reaches overlay_template as None, not (), so the
        # empty tuple can only come from a caller that means it: force no job.
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit")]}))
        write(self.home / "cron" / "jobs.json", json.dumps({"jobs": [job("only-here")]}))
        ps.overlay_template(self.home, self.template, None, ("cron",), ())
        merged = json.loads((self.home / "cron" / "jobs.json").read_text())["jobs"]
        self.assertEqual(["only-here"], [j["id"] for j in merged])

    def test_no_id_list_merges_the_whole_shipped_roster(self):
        # The platform profile's path, unchanged: it passes no filter.
        merged = self.overlay([job("audit"), job("drift")], [job("audit")])
        self.assertEqual(["audit", "drift"], [j["id"] for j in merged])

    def test_the_shipped_roster_lands_a_new_entry_on_a_volume_that_predates_it(self):
        # The real roster, not a fixture: this is how a job added to the image
        # reaches every existing install at its next pod start, with no
        # migration. `feedback-prompt` is the entry that relies on it -- an
        # install upgraded to the image that ships it gets the job, armed from
        # that first tick, while its `.feedback_prompt_*` markers on the volume
        # are what keep a later roll from re-arming it.
        shipped = json.loads(SHIPPED_ROSTER.read_text(encoding="utf-8"))["jobs"]
        volume = [dict(j, last_run_at="2026-09-01T06:20:00Z") for j in shipped if j["id"] != "feedback-prompt"]
        self.assertNotIn("feedback-prompt", [j["id"] for j in volume])
        merged = self.overlay(shipped, volume)
        by_id = {j["id"]: j for j in merged}
        self.assertIn("feedback-prompt", by_id)
        self.assertIs(True, by_id["feedback-prompt"]["enabled"])
        self.assertEqual("feedback_prompt.py", by_id["feedback-prompt"]["script"])
        self.assertNotIn("last_run_at", by_id["feedback-prompt"])
        self.assertEqual("2026-09-01T06:20:00Z", by_id["compliance-audit"]["last_run_at"])

    def test_the_merge_is_skipped_when_cron_is_not_being_overlaid(self):
        # `--items` without `cron` means the caller is not touching the cron
        # directory; rewriting jobs.json anyway would be this function editing
        # a file it was not asked to.
        write(self.template / "cron" / "jobs.json", json.dumps({"jobs": [job("audit")]}))
        write(self.template / "SOUL.md", "image persona")
        original = json.dumps({"jobs": [job("only-on-the-volume")]})
        write(self.home / "cron" / "jobs.json", original)
        ps.overlay_template(self.home, self.template, None, ("SOUL.md",))
        self.assertEqual(original, (self.home / "cron" / "jobs.json").read_text())


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

# --- The scaffold gate ----------------------------------------------------------
#
# `hermes profile create` is the only thing that builds a profile, and it used to be
# skipped whenever the profile's directory already existed. A directory is not evidence:
# the kubelet creates a mounted volume's mount point inside the data PVC before anything
# here runs, so a profile could have a directory and nothing else — no registration, no
# skills — with every later start skipping the scaffold again.


class EnsureProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "profiles" / "platform"
        self.calls = []
        self.real_run = ps.subprocess.run
        self.addCleanup(setattr, ps.subprocess, "run", self.real_run)
        ps.subprocess.run = self.fake_run
        self.fail_create = False

    def fake_run(self, cmd, **kwargs):
        """Stand in for `hermes profile create`, which writes profiles/<name>/profile.yaml."""
        self.calls.append(cmd)
        if self.fail_create:
            raise subprocess.CalledProcessError(1, cmd, output="", stderr="profile already exists")
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "profile.yaml").write_text("name: platform\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def ensure(self):
        with redirect_stderr(io.StringIO()) as err:
            home = ps.ensure_profile("platform", "desc", self.tmp)
        self.stderr = err.getvalue()
        return home

    def test_creates_a_profile_that_does_not_exist(self):
        self.assertEqual(self.ensure(), self.home)
        self.assertEqual(len(self.calls), 1)

    def test_skips_a_profile_already_registered(self):
        self.home.mkdir(parents=True)
        (self.home / "profile.yaml").write_text("name: platform\n")
        self.ensure()
        self.assertEqual(self.calls, [], "a registered profile must not be re-created")

    def test_a_bare_mount_point_does_not_count_as_a_scaffold(self):
        """The regression: profiles/platform/plugins/<plugin>/ made by the kubelet.

        The old gate read that directory as a finished profile and skipped the scaffold —
        so the profile had no skills, was never registered, and never self-healed.
        """
        (self.home / "plugins" / "stockout").mkdir(parents=True)

        self.ensure()

        self.assertEqual(len(self.calls), 1, "the scaffold must still run")
        self.assertTrue((self.home / "profile.yaml").is_file())
        self.assertFalse((self.home / "plugins" / "stockout").exists(), "the skeleton is cleared")

    def test_a_home_holding_files_is_never_deleted(self):
        """Only provably empty directories are cleared; a file means somebody's state."""
        self.home.mkdir(parents=True)
        (self.home / "USER.md").write_text("cluster identity\n")
        self.fail_create = True

        self.ensure()

        self.assertTrue((self.home / "USER.md").is_file())
        self.assertIn("continuing", self.stderr, "the refusal is logged, not fatal")

    def test_a_failed_create_on_a_fresh_home_is_fatal(self):
        self.fail_create = True
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            ps.ensure_profile("platform", "desc", self.tmp)

    def test_is_scaffolded_reads_the_marker_not_the_directory(self):
        self.assertFalse(ps.is_scaffolded(self.home))
        self.home.mkdir(parents=True)
        self.assertFalse(ps.is_scaffolded(self.home))
        (self.home / "config.yaml").write_text("plugins: {}\n")
        self.assertFalse(ps.is_scaffolded(self.home), "a config alone proves nothing")
        (self.home / "profile.yaml").write_text("name: platform\n")
        self.assertTrue(ps.is_scaffolded(self.home))


class NpmCacheTest(unittest.TestCase):
    """Every profile's npm uses one cache under $HERMES_HOME (gke-labs#3376).

    A profile's processes run with HOME at <profile>/home, so without a ~/.npm link
    npm keeps a separate ~100 MiB cache per profile.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.real_run = ps.subprocess.run
        self.addCleanup(setattr, ps.subprocess, "run", self.real_run)
        ps.subprocess.run = self.fake_run

    def fake_run(self, cmd, **kwargs):
        """Stand in for `hermes profile create`, which also makes the profile's home/."""
        home = Path(kwargs["env"]["HERMES_HOME"]) / "profiles" / cmd[3]
        (home / "home").mkdir(parents=True, exist_ok=True)
        (home / "profile.yaml").write_text(f"name: {cmd[3]}\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def scaffold(self, hermes_home, name="cluster-a"):
        with redirect_stderr(io.StringIO()):
            return ps.ensure_profile(name, "desc", hermes_home) / "home"

    def share(self, process_home, hermes_home, reclaim=False):
        with redirect_stderr(io.StringIO()) as err:
            ps.share_npm_cache(process_home, hermes_home, reclaim=reclaim)
        return err.getvalue()

    def npm_cache(self, process_home):
        """`npm config get cache` as a process with HOME=process_home sees it."""
        cwd = self.tmp / "cwd"
        cwd.mkdir(exist_ok=True)
        proc = self.real_run(
            ["npm", "config", "get", "cache"],
            capture_output=True, text=True, timeout=60, cwd=cwd,
            env={"PATH": ps.os.environ["PATH"], "HOME": str(process_home)},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def old_cache(self, process_home):
        """A per-profile cache as npm left it before this change."""
        old = process_home / ".npm" / "_cacache"
        old.mkdir(parents=True)
        (old / "index").write_bytes(b"x" * 10)
        return process_home / ".npm"

    def test_a_scaffolded_profile_uses_the_shared_cache(self):
        """Hermes looks for npx packages at ~/.npm/_npx, so that path leads to the shared cache too."""
        for hermes_home in (self.tmp / "opt" / "data", self.tmp / "srv" / "agent-home"):
            with self.subTest(hermes_home=hermes_home):
                process_home = self.scaffold(hermes_home)
                shared = hermes_home / ".cache" / "npm"
                self.assertTrue((process_home / ".npm").is_symlink())
                self.assertEqual((process_home / ".npm").resolve(), shared)
                if shutil.which("npm"):
                    self.assertEqual(Path(self.npm_cache(process_home)).resolve(), shared)

    @unittest.skipUnless(shutil.which("npm"), "npm is not installed")
    def test_without_the_link_npm_uses_a_cache_per_home(self):
        """The control for the test above: the cache follows HOME when nothing points it elsewhere."""
        process_home = self.tmp / "profiles" / "cluster-a" / "home"
        process_home.mkdir(parents=True)
        self.assertEqual(self.npm_cache(process_home), f"{process_home}/.npm")

    @unittest.skipUnless(shutil.which("npm"), "npm is not installed")
    def test_a_cache_line_a_user_wrote_still_wins(self):
        process_home = self.scaffold(self.tmp)
        (process_home / ".npmrc").write_text("cache=/elsewhere/npm\n", encoding="utf-8")
        self.assertEqual(self.npm_cache(process_home), "/elsewhere/npm")

    def test_an_existing_symlink_stays(self):
        process_home = self.tmp / "home"
        process_home.mkdir()
        (process_home / ".npm").symlink_to("/elsewhere/npm")

        self.share(process_home, self.tmp, reclaim=True)

        self.assertEqual(ps.os.readlink(process_home / ".npm"), "/elsewhere/npm")

    def test_the_scaffolder_leaves_an_old_cache_in_place(self):
        """A process of the profile can be using it; only the start-up sweep removes it."""
        process_home = self.tmp / "profiles" / "cluster-a" / "home"
        process_home.mkdir(parents=True)
        old = self.old_cache(process_home)

        self.scaffold(self.tmp)

        self.assertFalse(old.is_symlink())
        self.assertTrue((old / "_cacache" / "index").is_file())

    def test_the_sweep_replaces_an_old_cache(self):
        process_home = self.tmp / "home"
        process_home.mkdir()
        old = self.old_cache(process_home)

        err = self.share(process_home, self.tmp, reclaim=True)

        self.assertEqual(old.resolve(), self.tmp / ".cache" / "npm")
        self.assertIn(f"reclaimed the per-profile npm cache {process_home / ps.NPM_RECLAIM_NAME} (10 bytes)", err)

    def test_a_failed_delete_still_leaves_the_link(self):
        # The old cache is moved aside before the link goes in, so a delete that
        # fails (another replica writing into it) leaves an orphan directory,
        # never a home with no link until the next start.
        process_home = self.tmp / "home"
        process_home.mkdir()
        old = self.old_cache(process_home)
        real_rmtree = ps.shutil.rmtree
        self.addCleanup(setattr, ps.shutil, "rmtree", real_rmtree)

        def fail(path, *args, **kwargs):
            raise OSError(39, "Directory not empty", str(path))

        ps.shutil.rmtree = fail
        err = self.share(process_home, self.tmp, reclaim=True)

        self.assertTrue(old.is_symlink())
        self.assertEqual(old.resolve(), self.tmp / ".cache" / "npm")
        self.assertTrue((process_home / ps.NPM_RECLAIM_NAME).is_dir())
        self.assertIn(f"could not delete the old per-profile npm cache {process_home / ps.NPM_RECLAIM_NAME}", err)
        self.assertNotIn("could not point npm", err)

        # The next start-up sweep deletes the orphan.
        ps.shutil.rmtree = real_rmtree
        err = self.share(process_home, self.tmp, reclaim=True)

        self.assertFalse((process_home / ps.NPM_RECLAIM_NAME).exists())
        self.assertIn("reclaimed the per-profile npm cache", err)
        self.assertTrue(old.is_symlink())

    def test_an_orphan_does_not_block_the_next_reclaim(self):
        # A start-up stopped between the move and the link: an orphan and no
        # link. A process then made a new real cache. The sweep removes both.
        process_home = self.tmp / "home"
        process_home.mkdir()
        old = self.old_cache(process_home)
        orphan = process_home / ps.NPM_RECLAIM_NAME
        (orphan / "_cacache").mkdir(parents=True)
        (orphan / "_cacache" / "index").write_text("x" * 7, encoding="utf-8")

        err = self.share(process_home, self.tmp, reclaim=True)

        self.assertEqual(old.resolve(), self.tmp / ".cache" / "npm")
        self.assertFalse(orphan.exists())
        self.assertNotIn("WARN", err)

    def test_the_scaffolder_leaves_an_orphan_alone(self):
        """Only the start-up sweep deletes, because a process can be using the directory."""
        process_home = self.tmp / "home"
        process_home.mkdir()
        orphan = process_home / ps.NPM_RECLAIM_NAME
        orphan.mkdir()

        self.share(process_home, self.tmp)

        self.assertTrue(orphan.is_dir())

    def test_a_second_run_changes_nothing(self):
        process_home = self.scaffold(self.tmp)
        before = ps.os.readlink(process_home / ".npm")

        self.assertEqual(self.share(process_home, self.tmp, reclaim=True), "")
        self.assertEqual(ps.os.readlink(process_home / ".npm"), before)

    def test_the_link_follows_a_moved_agent_home(self):
        """An absolute link would point npm at the old mount path."""
        old_home, new_home = self.tmp / "opt" / "data", self.tmp / "srv" / "agent-home"
        self.scaffold(old_home)
        new_home.parent.mkdir(parents=True)
        old_home.rename(new_home)

        link = new_home / "profiles" / "cluster-a" / "home" / ".npm"
        self.assertEqual(link.resolve(), new_home / ".cache" / "npm")

    def test_a_removed_shared_cache_is_made_again(self):
        """npm fails on every command when its cache is a dangling link."""
        process_home = self.scaffold(self.tmp)
        (self.tmp / ".cache" / "npm").rmdir()

        self.scaffold(self.tmp)

        self.assertTrue((process_home / ".npm").resolve().is_dir())

    def test_a_missing_home_is_not_created(self):
        """Hermes uses <profile>/home as HOME only when it exists; creating it would move HOME."""
        process_home = self.tmp / "profiles" / "cluster-a" / "home"
        self.share(process_home, self.tmp)
        self.assertFalse(process_home.exists())

    def test_the_sweep_reaches_the_default_home_and_every_profile(self):
        homes = [self.tmp / "home", self.tmp / "profiles" / "a" / "home", self.tmp / "profiles" / "b" / "home"]
        for home in homes:
            home.mkdir(parents=True)
        self.old_cache(homes[1])
        bare = self.tmp / "profiles" / "c"
        bare.mkdir()

        with unittest.mock.patch.object(sys, "argv", ["profile_scaffold.py", "--share-npm-cache"]), \
                unittest.mock.patch.dict(ps.os.environ, {"HERMES_HOME": str(self.tmp)}), \
                redirect_stderr(io.StringIO()):
            ps.main()

        for home in homes:
            self.assertEqual((home / ".npm").resolve(), self.tmp / ".cache" / "npm")
        self.assertFalse((bare / "home").exists())


class BackfillCronStoreTest(unittest.TestCase):
    """Tests for standalone cron store backfill helpers (for cluster profiles and runtime stores)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_backfill_cron_store_adds_low_risk_to_unannotated_jobs(self):
        store = {
            "jobs": [
                {"id": "a", "prompt": "do a"},
                {"id": "b", "prompt": "do b", "risk": "high"},
            ]
        }
        res = ps.backfill_cron_store(store)
        self.assertIsNot(res, store)
        self.assertEqual(res["jobs"][0]["risk"], "low")
        self.assertEqual(res["jobs"][1]["risk"], "high")
        self.assertNotIn("risk", store["jobs"][0], "input dict must stay unmodified")

    def test_backfill_cron_store_no_op_when_already_annotated(self):
        store = {
            "jobs": [
                {"id": "a", "risk": "low"},
                {"id": "b", "risk": "high"},
            ]
        }
        res = ps.backfill_cron_store(store)
        self.assertIs(res, store)

    def test_backfill_cron_store_handles_non_dict_and_invalid_shapes(self):
        self.assertIsNone(ps.backfill_cron_store(None))
        self.assertEqual(ps.backfill_cron_store("not-a-dict"), "not-a-dict")
        self.assertEqual(ps.backfill_cron_store({"jobs": "not-a-list"}), {"jobs": "not-a-list"})

    def test_backfill_cron_file_modifies_file_in_place_atomically(self):
        cron_file = self.root / "jobs.json"
        cron_file.write_text(json.dumps({"jobs": [{"id": "job1"}]}))
        modified = ps.backfill_cron_file(cron_file)
        self.assertTrue(modified)
        data = json.loads(cron_file.read_text())
        self.assertEqual(data["jobs"][0]["risk"], "low")

        # Second run is a no-op
        self.assertFalse(ps.backfill_cron_file(cron_file))

    def test_backfill_cron_file_handles_nonexistent_file(self):
        self.assertFalse(ps.backfill_cron_file(self.root / "absent.json"))

    def test_backfill_cron_file_holds_cron_jobs_lock(self):
        cron_file = self.root / "jobs.json"
        cron_file.write_text(json.dumps({"jobs": [{"id": "job1"}]}))
        lock_held = False
        orig_backfill = ps.backfill_cron_store

        def probe_backfill(*args, **kwargs):
            nonlocal lock_held
            lock_path = self.root / ".jobs.lock"
            if lock_path.is_file():
                try:
                    with open(lock_path, "a+") as probe_fd:
                        try:
                            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fcntl.flock(probe_fd, fcntl.LOCK_UN)
                        except (BlockingIOError, OSError):
                            lock_held = True
                except OSError:
                    pass
            return orig_backfill(*args, **kwargs)

        with unittest.mock.patch.object(ps, "backfill_cron_store", side_effect=probe_backfill):
            ps.backfill_cron_file(cron_file)

        self.assertTrue(lock_held, ".jobs.lock was NOT held exclusively during backfill_cron_file")


if __name__ == "__main__":
    unittest.main()
