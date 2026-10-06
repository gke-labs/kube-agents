"""scripts/eval_dashboard/periodics.py: the watched Prow periodics' latest
finished builds, read from the bucket they log to, and the notes health.py carries.

* `fetch` reads the pointer, walks back to a finished build, keeps the job's
  report when the build wrote one, writes one <job>.json per job with a
  build, and nothing for a job that never ran or whose pointer is denied;
* `assess` notes a failed build (the sweep's once two consecutive checks have
  failed) and a job whose last finished build is older than its stale window,
  keeps `since` across ticks, and names the report's failed projects;
* the workflow fetches the readings before it adjudicates and hands the
  directory to health.py.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from eval_dashboard import periodics  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci-health.yml"
NOW = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)
SWEEP = periodics.WATCHED_BY_JOB["ci-kube-agents-pull-sweep"]
DAILY = periodics.WATCHED_BY_JOB["ci-kube-agents-fleet-reconcile-daily"]
POST = periodics.WATCHED_BY_JOB["post-kube-agents-fleet-reconcile"]


def epoch(when: datetime) -> int:
    return int(when.timestamp())


class FakeGsutil:
    """Answers `gsutil -q cat|ls <path>` from a dict of gs:// paths to text;
    a missing path is a NotFound unless `denied` names it."""

    def __init__(self, objects: dict, denied=()):
        self.objects = objects
        self.denied = set(denied)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        verb, path = cmd[2], cmd[3]
        if path in self.denied:
            return subprocess.CompletedProcess(cmd, 1, "", "AccessDeniedException: 403 caller does not have storage.objects.get access")
        if verb == "cat":
            if path in self.objects:
                return subprocess.CompletedProcess(cmd, 0, self.objects[path], "")
            return subprocess.CompletedProcess(cmd, 1, "", f"CommandException: No URLs matched: {path}")
        if verb == "ls":
            names = sorted({key[len(path):].split("/", 1)[0] for key in self.objects if key.startswith(path)})
            return subprocess.CompletedProcess(cmd, 0, "".join(f"{path}{name}/\n" for name in names), "")
        raise AssertionError(cmd)


def archive(job, builds):
    """{build id: (finished dict or None, artifact dict or None)} -> objects."""
    root = f"{periodics.LOGS_ROOT}/{job}"
    objects = {f"{root}/{periodics.POINTER}": max(builds, key=int) + "\n"}
    for build, (finished, artifact) in builds.items():
        objects[f"{root}/{build}/started.json"] = "{}"
        if finished is not None:
            objects[f"{root}/{build}/{periodics.FINISHED}"] = json.dumps(finished)
        if artifact is not None:
            objects[f"{root}/{build}/{periodics.ARTIFACTS_DIR}/{periodics.WATCHED_BY_JOB[job].artifact}"] = json.dumps(artifact)
    return objects


def finished(when, passed=True):
    return {"timestamp": epoch(when), "passed": passed, "result": "SUCCESS" if passed else "FAILURE"}


class FetchTest(unittest.TestCase):
    def test_the_latest_finished_build_is_read_with_its_artifact(self):
        report = {"schema_version": 1, "mode": "all", "dry_run": True, "exit": "failed", "outcomes": {"kube-agents-evals-3": {"outcome": "refused", "detail": "1 to add; not a create: delete x"}}}
        objects = archive(DAILY.job, {"100": (finished(NOW - timedelta(hours=1), passed=False), report)})
        gsutil = FakeGsutil(objects)
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=gsutil)
            written = json.loads((pathlib.Path(tmp) / f"{DAILY.job}.json").read_text())
        self.assertEqual(readings[DAILY.job], written)
        self.assertEqual((written["build"], written["passed"], written["result"]), ("100", False, "FAILURE"))
        self.assertEqual(written["finished_at"], (NOW - timedelta(hours=1)).isoformat(timespec="seconds"))
        self.assertEqual(written["artifact"], report)

    def test_a_running_newest_build_falls_back_to_the_one_before_it(self):
        objects = archive(POST.job, {"101": (None, None), "100": (finished(NOW - timedelta(minutes=50)), None), "99": (finished(NOW - timedelta(hours=2)), None)})
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(POST,), runner=FakeGsutil(objects))
        self.assertEqual(readings[POST.job]["build"], "100")
        self.assertIsNone(readings[POST.job]["artifact"], "the postsubmit wrote no artifact for that build")

    def test_the_prefix_is_listed_only_when_the_newest_build_has_not_finished(self):
        # One listing per job per tick over a prefix that only grows, and the
        # newest build has finished most of the time: it is not made then.
        objects = archive(SWEEP.job, {"8": (finished(NOW - timedelta(minutes=3)), None), "7": (finished(NOW - timedelta(minutes=13)), None)})
        gsutil = FakeGsutil(objects)
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(SWEEP,), runner=gsutil)
        self.assertEqual(readings[SWEEP.job]["build"], "8")
        self.assertEqual([c[2] for c in gsutil.calls], ["cat", "cat", "cat"], "pointer, finished.json and the report; no ls")

    def test_an_aborted_newest_build_is_walked_past_not_reported_as_failed(self):
        # Prow's sidecar writes finished.json with result ABORTED when it
        # stops a pod (a drained node, a plank abort): not a run of the job.
        aborted = {"timestamp": epoch(NOW - timedelta(minutes=30)), "passed": False, "result": "ABORTED"}
        objects = archive(DAILY.job, {"101": (aborted, None), "100": (finished(NOW - timedelta(days=1)), None)})
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=FakeGsutil(objects))
        self.assertEqual((readings[DAILY.job]["build"], readings[DAILY.job]["passed"]), ("100", True))
        self.assertEqual(periodics.assess(readings, NOW, {}), {})
        # Every build aborted: nothing to read, no note.
        only_aborted = archive(DAILY.job, {"101": (aborted, None)})
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=FakeGsutil(only_aborted)), {})

    def test_a_job_that_never_ran_or_whose_pointer_is_denied_writes_nothing(self):
        objects = archive(SWEEP.job, {"7": (finished(NOW - timedelta(minutes=5)), None)})
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            (out / f"{POST.job}.json").write_text("stale from last tick")
            readings = periodics.fetch(out, watched=(SWEEP, POST), runner=FakeGsutil(objects))
            self.assertEqual(sorted(readings), [SWEEP.job])
            self.assertFalse((out / f"{POST.job}.json").exists(), "last tick's file does not survive a job with no build")
            warnings = []
            denied = FakeGsutil(objects, denied={f"{periodics.LOGS_ROOT}/{SWEEP.job}/{periodics.POINTER}"})
            readings = periodics.fetch(out, watched=(SWEEP,), runner=denied, log=lambda *a, **k: warnings.append(a[0]))
        self.assertEqual(readings, {})
        self.assertEqual(len(warnings), 1, "a denied pointer is said, a NotFound is not")
        self.assertIn("build pointer", warnings[0])

    def test_a_finished_json_that_cannot_be_read_is_a_blind_tick_not_an_older_build(self):
        # A 503 on the newest finished build must not walk back to last
        # week's and report that: nothing is written, with a warning.
        objects = archive(DAILY.job, {"101": (finished(NOW - timedelta(days=1)), None), "100": (finished(NOW - timedelta(days=8, hours=1)), None)})
        denied = FakeGsutil(objects, denied={f"{periodics.LOGS_ROOT}/{DAILY.job}/101/{periodics.FINISHED}"})
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=denied, log=lambda *a, **k: warnings.append(a[0]))
        self.assertEqual(readings, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("101/finished.json", warnings[0])

    def test_a_build_id_containing_404_in_an_error_is_not_read_as_absent(self):
        # gsutil echoes the failing URL; a 503 on .../1404/finished.json is
        # not a NotFound, and the walk must not report the build before it.
        objects = archive(DAILY.job, {"1404": (finished(NOW - timedelta(hours=1), passed=False), None), "1400": (finished(NOW - timedelta(days=1)), None)})
        class Flaky(FakeGsutil):
            def __call__(self, cmd, **kwargs):
                if cmd[3].endswith("/1404/finished.json"):
                    return subprocess.CompletedProcess(cmd, 1, "", f"ServiceException: 503 Backend error reading {cmd[3]}")
                return super().__call__(cmd, **kwargs)
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=Flaky(objects), log=lambda *a, **k: warnings.append(a[0])), {})
        self.assertEqual(len(warnings), 1)
        self.assertTrue(periodics._not_found("CommandException: No URLs matched: gs://x/y"))
        self.assertTrue(periodics._not_found("NotFoundException: 404 gs://x/y does not exist."))
        self.assertFalse(periodics._not_found("ServiceException: 503 on gs://kube-agents-prow/logs/j/2097891568546404123/started.json"))

    def test_a_present_but_unparseable_finished_json_is_a_blind_tick(self):
        # An empty or truncated object is not "still running": walking past it
        # would report the build before as the latest finished run.
        root = f"{periodics.LOGS_ROOT}/{DAILY.job}"
        objects = archive(DAILY.job, {"101": (None, None), "100": (finished(NOW - timedelta(days=1), passed=True), None)})
        objects[f"{root}/101/{periodics.FINISHED}"] = ""
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=FakeGsutil(objects), log=lambda *a, **k: warnings.append(a[0]))
        self.assertEqual(readings, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("101/finished.json: not JSON", warnings[0])
        objects[f"{root}/101/{periodics.FINISHED}"] = "[]"
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=FakeGsutil(objects), log=lambda *a, **k: None), {})

    def test_an_absurd_timestamp_is_no_finish_time_not_a_crash(self):
        objects = archive(POST.job, {"5": ({"timestamp": 10**20, "passed": True, "result": "SUCCESS"}, None)})
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(POST,), runner=FakeGsutil(objects))
        self.assertIsNone(readings[POST.job]["finished_at"])
        self.assertEqual(periodics.assess(readings, NOW, {})[POST.job]["verdict"], periodics.VERDICT_STALE)

    def test_load_readings_skips_a_file_it_cannot_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            (out / f"{SWEEP.job}.json").write_text("{not json")
            (out / f"{POST.job}.json").write_text(json.dumps({"job": POST.job, "build": "1", "finished_at": NOW.isoformat(), "passed": True, "result": "SUCCESS"}))
            self.assertEqual(sorted(periodics.load_readings(out)), [POST.job])
        self.assertEqual(periodics.load_readings(None), {})


    def test_a_listed_build_is_read_under_the_name_that_was_listed(self):
        # A non-canonical decimal name is Prow's to never write; if one is
        # there, it is read as listed rather than rebuilt through int().
        objects = archive(DAILY.job, {"200": (None, None), "0099": (finished(NOW - timedelta(hours=1), passed=False), None)})
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=FakeGsutil(objects))
        self.assertEqual(readings[DAILY.job]["build"], "0099")

    def test_a_passed_builds_unreadable_report_keeps_the_reading(self):
        # The report of a passed build is only the recovery's summary: a read
        # that fails for a reason other than NotFound warns and the reading
        # stands without it, where a failed build's would blind the tick.
        root = f"{periodics.LOGS_ROOT}/{DAILY.job}"
        objects = archive(DAILY.job, {"100": (finished(NOW - timedelta(hours=1), passed=True), {"summary": {"applied": 1}})})
        denied = FakeGsutil(objects, denied={f"{root}/100/{periodics.ARTIFACTS_DIR}/{periodics.RECONCILE_ARTIFACT}"})
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=denied, log=lambda *a, **k: warnings.append(a[0]))
        self.assertEqual((readings[DAILY.job]["build"], readings[DAILY.job]["artifact"]), ("100", None))
        self.assertEqual(len(warnings), 1)

    def test_a_missing_bucket_is_warned_not_read_as_never_ran(self):
        class NoBucket:
            def __call__(self, cmd, **kwargs):
                return subprocess.CompletedProcess(cmd, 1, "", f"BucketNotFoundException: 404 gs://{cmd[3].split('/')[2]} bucket does not exist.")
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(periodics.fetch(pathlib.Path(tmp), watched=(SWEEP,), runner=NoBucket(), log=lambda *a, **k: warnings.append(a[0])), {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("log bucket does not exist", warnings[0])

class AssessTest(unittest.TestCase):
    def reading(self, periodic, when, passed=True, artifact=None, build="100"):
        reading = {"job": periodic.job, "build": build, "finished_at": when.isoformat(timespec="seconds"), "passed": passed, "result": "SUCCESS" if passed else "FAILURE"}
        if periodic.artifact:
            reading["artifact"] = artifact
        return reading

    def test_a_passed_fresh_build_is_no_note(self):
        readings = {SWEEP.job: self.reading(SWEEP, NOW - timedelta(minutes=5)), DAILY.job: self.reading(DAILY, NOW - timedelta(days=1)), POST.job: self.reading(POST, NOW - timedelta(days=40))}
        self.assertEqual(periodics.assess(readings, NOW, {}), {})

    def test_a_failed_build_is_a_note_naming_the_refused_projects(self):
        artifact = {"dry_run": False, "outcomes": {"kube-agents-evals-3": {"outcome": "refused", "detail": "not a create or an in-place update: delete google_container_cluster.seeded_b"}, "kube-agents-evals-4": {"outcome": "applied", "detail": "2 to add"}}, "error": "1 project(s) not reconciled: kube-agents-evals-3"}
        readings = {DAILY.job: self.reading(DAILY, NOW - timedelta(hours=1), passed=False, artifact=artifact)}
        notes = periodics.assess(readings, NOW, {})
        note = notes[DAILY.job]
        self.assertEqual((note["verdict"], note["build"], note["since"], note["dry_run"]), (periodics.VERDICT_FAILED, "100", NOW.isoformat(timespec="seconds"), False))
        self.assertEqual(note["detail"], ["kube-agents-evals-3: refused (not a create or an in-place update: delete google_container_cluster.seeded_b); next: a code change, or an entry in bench/tf/fleet/reconcile-allow.json", "run: 1 project(s) not reconciled: kube-agents-evals-3"])
        self.assertEqual(note["history_url"], f"{periodics.JOB_HISTORY_ROOT}/{DAILY.job}")
        self.assertIn("build 100 failed", periodics.evidence(note))
        # The next tick keeps the episode's start.
        later = periodics.assess(readings, NOW + timedelta(hours=1), notes)
        self.assertEqual(later[DAILY.job]["since"], note["since"])

    def test_a_job_past_its_stale_window_is_stale_whatever_its_last_verdict(self):
        readings = {DAILY.job: self.reading(DAILY, NOW - timedelta(hours=37)), POST.job: self.reading(POST, NOW - timedelta(days=30))}
        notes = periodics.assess(readings, NOW, {})
        self.assertEqual(sorted(notes), [DAILY.job], "a postsubmit has no cadence, so no age makes it stale")
        self.assertEqual(notes[DAILY.job]["verdict"], periodics.VERDICT_STALE)
        self.assertIn("no finished run since", periodics.evidence(notes[DAILY.job]))
        fresh = {DAILY.job: self.reading(DAILY, NOW - timedelta(hours=35))}
        self.assertEqual(periodics.assess(fresh, NOW, {}), {})

    def test_a_failed_postsubmit_is_retired_once_a_later_daily_has_dealt_with_its_projects(self):
        # The postsubmit has no window to retire it. A later daily that
        # reached the projects it failed on is the recovery; one that passed
        # without reaching them is not; one that failed is the current story
        # about the fleet, so the older failure is not news beside it.
        failed = self.reading(POST, NOW - timedelta(days=2), passed=False, artifact={"summary": {"refused": 1}, "outcomes": {"kube-agents-evals-9": {"outcome": "failed", "detail": "lock"}}})
        reached = self.reading(DAILY, NOW - timedelta(hours=1), artifact={"summary": {"converged": 35}, "outcomes": {"kube-agents-evals-9": {"outcome": "converged", "detail": ""}}})
        decided = periodics.superseded_jobs({POST.job: failed, DAILY.job: reached})
        # The run the recovery was decided on rides with it: what the clear
        # cites, whether or not the tick that sends it read the daily.
        by = {key: periodics.runs({DAILY.job: reached})[DAILY.job][key] for key in ("build", "finished_at", "summary")}
        self.assertEqual(decided, {POST.job: {"build": "100", "recovery": True, "by": by}})
        self.assertEqual(by["build"], "100")
        self.assertEqual(periodics.assess({POST.job: failed, DAILY.job: reached}, NOW, {}, superseded=decided), {})
        missed = self.reading(DAILY, NOW - timedelta(hours=1), artifact={"summary": {"converged": 34, "not_reached": 1}, "outcomes": {"kube-agents-evals-9": {"outcome": "not_reached", "detail": "busy"}}})
        self.assertEqual(periodics.superseded_jobs({POST.job: failed, DAILY.job: missed}), {})
        self.assertIn(POST.job, periodics.assess({POST.job: failed, DAILY.job: missed}, NOW, {}, superseded={}))
        # A project a whole pass no longer lists has left the pool (a stray
        # registration removed, as the note's own next step says): dealt
        # with. A pass that is not whole says nothing about it.
        gone = self.reading(DAILY, NOW - timedelta(hours=1), artifact={"mode": "all", "visited": 1, "mapped": 1, "summary": {"converged": 1}, "outcomes": {"kube-agents-evals-3": {"outcome": "converged", "detail": ""}}})
        self.assertEqual(periodics.superseded_jobs({POST.job: failed, DAILY.job: gone})[POST.job]["recovery"], True)
        partial = self.reading(DAILY, NOW - timedelta(hours=1), artifact={"mode": "all", "visited": 1, "mapped": 2, "summary": {"converged": 1}, "outcomes": {"kube-agents-evals-3": {"outcome": "converged", "detail": ""}}})
        self.assertEqual(periodics.superseded_jobs({POST.job: failed, DAILY.job: partial}), {})
        # A failed build whose report is unreadable names no project because
        # nothing can be read: no later pass recovers it, a later failed
        # daily still silences it.
        unread = self.reading(POST, NOW - timedelta(days=2), passed=False, artifact={"error": periodics.REPORT_UNREADABLE})
        self.assertEqual(periodics.superseded_jobs({POST.job: unread, DAILY.job: reached}), {})
        self.assertEqual(periodics.superseded_jobs({POST.job: self.reading(POST, NOW - timedelta(days=2), passed=False, artifact=None), DAILY.job: reached}), {})
        # A failed daily silences the older note but is no recovery.
        daily_failed = self.reading(DAILY, NOW - timedelta(hours=1), passed=False, artifact={"summary": {"failed": 1}, "outcomes": {"kube-agents-evals-9": {"outcome": "failed", "detail": "lock"}}})
        decided = periodics.superseded_jobs({POST.job: failed, DAILY.job: daily_failed})
        self.assertEqual(decided, {POST.job: {"build": "100", "recovery": False}})
        notes = periodics.assess({POST.job: failed, DAILY.job: daily_failed}, NOW, {}, superseded=decided)
        self.assertEqual(sorted(notes), [DAILY.job], "the daily's own note is the current story")
        earlier_daily = self.reading(DAILY, NOW - timedelta(days=3), artifact={"outcomes": {"kube-agents-evals-9": {"outcome": "converged", "detail": ""}}})
        self.assertEqual(periodics.superseded_jobs({POST.job: failed, DAILY.job: earlier_daily}), {})
        self.assertEqual(periodics.superseded_jobs({POST.job: failed}), {})
        # Sticky for the same failed build: a tick blind to the daily, or a
        # later failing daily, does not re-open a retired failure as news;
        # a new failed build is decided afresh.
        carried = {POST.job: {"build": "100", "recovery": True}}
        self.assertEqual(periodics.superseded_jobs({POST.job: failed}, carried), carried)
        self.assertEqual(periodics.superseded_jobs({POST.job: failed, DAILY.job: daily_failed}, carried), carried)
        self.assertEqual(periodics.assess({POST.job: failed}, NOW, {}, superseded=carried), {})
        newer = self.reading(POST, NOW - timedelta(hours=2), passed=False, build="101", artifact={"outcomes": {"kube-agents-evals-9": {"outcome": "failed", "detail": "lock"}}})
        self.assertEqual(periodics.superseded_jobs({POST.job: newer}, carried), {})
        # A tick blind to the postsubmit itself (its pointer, finished.json or
        # report unreadable) carries the entry unchanged; a reading that shows
        # the build passed, or a newer one, is what ends it.
        self.assertEqual(periodics.superseded_jobs({}, carried), carried)
        self.assertEqual(periodics.superseded_jobs({DAILY.job: daily_failed}, carried), carried)
        passed_post = self.reading(POST, NOW - timedelta(hours=1), passed=True, build="102")
        self.assertEqual(periodics.superseded_jobs({POST.job: passed_post}, carried), {})
        # And a carried silence becomes a recovery once a later daily reaches the projects.
        silenced = {POST.job: {"build": "100", "recovery": False}}
        self.assertEqual(periodics.superseded_jobs({POST.job: failed, DAILY.job: reached}, silenced), {POST.job: {"build": "100", "recovery": True, "by": by}})
        self.assertEqual(periodics.superseded_jobs({POST.job: unread, DAILY.job: daily_failed}), {POST.job: {"build": "100", "recovery": False}})
        # A silence ends when the daily that cast it passes without reaching
        # the projects: its own note clears, and the failure it hid is open
        # again (not news: the told key still holds). A failed daily keeps it.
        self.assertEqual(periodics.superseded_jobs({POST.job: failed, DAILY.job: missed}, silenced), {})
        self.assertEqual(periodics.superseded_jobs({POST.job: failed, DAILY.job: daily_failed}, silenced), silenced)
        # `by` rides through a tick blind to the daily and through a later
        # failed daily; a recovery never cites a run other than its own.
        with_by = {POST.job: {"build": "100", "recovery": True, "by": {"build": "9", "finished_at": "2026-09-14T08:40:00+00:00", "summary": "35 visited: 35 converged"}}}
        self.assertEqual(periodics.superseded_jobs({POST.job: failed}, with_by), with_by)
        self.assertEqual(periodics.superseded_jobs({POST.job: failed, DAILY.job: daily_failed}, with_by), with_by)

    def test_a_postsubmit_build_with_no_finish_time_is_still_said_without_a_window(self):
        readings = {POST.job: {"job": POST.job, "build": "9", "finished_at": None, "passed": True, "result": "SUCCESS", "artifact": None}}
        note = periodics.assess(readings, NOW, {})[POST.job]
        self.assertEqual((note["verdict"], note["stale_after_h"]), (periodics.VERDICT_STALE, None))
        self.assertIn("finished.json does not give", periodics.evidence(note))
        self.assertNotIn("None", periodics.evidence(note))

    def test_the_reconcile_summary_reads_the_visited_count_and_plain_words(self):
        artifact = {"visited": 35, "summary": {"applied": 2, "converged": 31, "unchanged": 0, "planned": 0, "busy": 0, "refused": 0, "failed": 0, "interrupted": 0, "not_reached": 2}}
        self.assertEqual(periodics.run_summary(DAILY, artifact, passed=True), "35 visited: 2 applied, 31 converged, 2 not reached")
        self.assertEqual(periodics.run_summary(DAILY, {"summary": {"applied": 3, "unchanged": 9}}, passed=True), "3 applied, 9 unchanged")
        self.assertEqual(periodics.run_summary(DAILY, {"visited": 0, "summary": {"not_reached": 35}}, passed=True), "0 visited: 35 not reached")

    def test_the_reconcile_detail_names_the_next_step_the_not_reached_and_the_unused_allowlist(self):
        artifact = {
            "outcomes": {
                "kube-agents-evals-3": {"outcome": "refused", "detail": "1 refused: delete x", "allowlist_unused": ["google_compute_disk.gone"]},
                "kube-agents-evals-4": {"outcome": "failed", "detail": "tofu apply exited 1: boom", "allowlist_unused": ["google_compute_disk.gone"]},
                "kube-agents-evals-5": {"outcome": "interrupted", "detail": "terminated (signal 15) while tofu ran; ... the next run tells whether it needs force-unlock"},
                "kube-agents-evals-6": {"outcome": "not_reached", "detail": "not started: 100s left in the run's budget, under the 3600s per-project ceiling; the next run takes it"},
                "kube-agents-evals-7": {"outcome": "not_reached", "detail": "not started: 100s left in the run's budget, under the 3600s per-project ceiling; the next run takes it"},
                "kube-agents-evals-8": {"outcome": "converged", "detail": "re-stamp", "allowlist_unused": ["google_compute_disk.gone"]},
            },
            "error": "2 project(s) not reconciled: kube-agents-evals-3, kube-agents-evals-4",
        }
        artifact["visited"] = 6
        artifact["mapped"] = 6
        artifact["mode"] = "all"
        lines = periodics.reconcile_detail(artifact)
        self.assertEqual(lines[0], "kube-agents-evals-3: refused (1 refused: delete x); next: a code change, or an entry in bench/tf/fleet/reconcile-allow.json")
        self.assertEqual(lines[1], "kube-agents-evals-4: failed (tofu apply exited 1: boom); next: nothing by hand, the next run retries it")
        # A failure the next run cannot clear names its hand step.
        stray = {"outcomes": {"kube-agents-evals-99": {"outcome": "failed", "detail": "not a mapped pool project (gitops_repo_for_project in hack/ci-deploy.sh)"}}}
        self.assertTrue(periodics.reconcile_detail(stray)[0].endswith(periodics.RECONCILE_NEXT_STEP_UNMAPPED))
        no_tofu = {"outcomes": {"p": {"outcome": "failed", "detail": "could not run tofu (FileNotFoundError: [Errno 2] No such file or directory: 'tofu')"}}}
        self.assertTrue(periodics.reconcile_detail(no_tofu)[0].endswith(periodics.RECONCILE_NEXT_STEP_RUNNER))
        # tofu's own lock error counts as locked too, and a non-string detail does not crash the tick.
        locked = {"outcomes": {"p": {"outcome": "failed", "detail": "tofu plan exited 1: Error acquiring the state lock: ConditionNotMet ... Lock Info: ID 1234"}}}
        self.assertTrue(periodics.reconcile_detail(locked)[0].endswith(periodics.RECONCILE_NEXT_STEP_LOCKED))
        # The 600-character tail the writer keeps can cut the header; the
        # Lock Info block and the trailer survive it.
        for tail in ("...  Lock Info:\n  ID: 1234\n  Path: gs://p-tf-state/seeded-fleet/default.tflock", "... OpenTofu acquires a state lock to protect the state from being written by multiple users at the same time."):
            cut = {"outcomes": {"p": {"outcome": "failed", "detail": "tofu plan exited 1: " + tail}}}
            self.assertTrue(periodics.reconcile_detail(cut)[0].endswith(periodics.RECONCILE_NEXT_STEP_LOCKED), tail)
        odd = {"outcomes": {"p": {"outcome": "failed", "detail": 1}, "q": {"outcome": "refused", "detail": True}}}
        self.assertEqual(len(periodics.reconcile_detail(odd)), 2)
        # A ceiling cut is not yet a lock: the runbook says force-unlock only
        # once the next run fails on it, and that run's message carries the
        # lock ID the command needs.
        ceiling = {"outcomes": {"p": {"outcome": "failed", "detail": "did not finish within 3600s; tofu was interrupted, and killed if it did not stop within 120s, which leaves the state locked; the next run tells whether it needs force-unlock"}}}
        self.assertEqual(periodics.reconcile_detail(ceiling), ["p: failed (did not finish within 3600s; tofu was interrupted, and killed if it did not stop within 120s, which leaves the state locked; the next run tells whether it needs force-unlock); next: the next run retries it, and force-unlocks only if it fails on the lock"])
        self.assertEqual(lines[2], "kube-agents-evals-5: interrupted (terminated (signal 15) while tofu ran; ... the next run tells whether it needs force-unlock); next: the next run retries it, and force-unlocks only if it fails on the lock")
        self.assertEqual(lines[3], "2 not reached (not started: 100s left in the run's budget, under the 3600s per-project ceiling; the next run takes it)")
        # The interrupted project carries no allowlist verdict, so this run
        # says nothing about the entry the others did not need.
        self.assertEqual(lines[4], "run: 2 project(s) not reconciled: kube-agents-evals-3, kube-agents-evals-4")
        self.assertEqual(len(lines), 5)

    def test_an_allowlist_entry_some_project_still_needed_is_not_called_unused(self):
        artifact = {"outcomes": {"p1": {"outcome": "applied", "detail": "", "allowlist_unused": ["a"]}, "p2": {"outcome": "applied", "detail": "", "allowlist_unused": []}}}
        self.assertEqual(periodics.reconcile_detail(artifact), [])
        # A project whose plan was never read carries no verdict, and a run
        # that did not reach every mapped project says nothing about the
        # allowlist: the projects it missed are the ones that may still need
        # the entry.
        artifact = {"visited": 1, "mapped": 2, "outcomes": {"p1": {"outcome": "applied", "detail": "", "allowlist_unused": ["a"]}, "p2": {"outcome": "busy", "detail": ""}}}
        self.assertEqual(periodics.reconcile_detail(artifact), [])
        # A visited project whose plan was never read (failed at init) carries
        # no verdict, and that is the project that may still need the entry.
        artifact = {"mode": "all", "visited": 2, "mapped": 2, "outcomes": {"p1": {"outcome": "applied", "detail": "", "allowlist_unused": ["a"]}, "p2": {"outcome": "failed", "detail": "init"}}}
        self.assertFalse(any(line.startswith("allowlist:") for line in periodics.reconcile_detail(artifact)))
        artifact = {"mode": "all", "visited": 2, "mapped": 2, "outcomes": {"p1": {"outcome": "applied", "detail": "", "allowlist_unused": ["a"]}, "p2": {"outcome": "converged", "detail": "", "allowlist_unused": ["a"]}}}
        self.assertEqual(periodics.reconcile_detail(artifact), ["allowlist: 1 entry no plan needed, remove it: a"])
        # A drifted or named run's `mapped` is its own list, not the pool: no claim.
        for mode in ("drifted", "project"):
            artifact["mode"] = mode
            self.assertEqual(periodics.reconcile_detail(artifact), [], mode)
        self.assertEqual(periodics.reconcile_detail({"outcomes": {"p1": {"outcome": "applied", "detail": "", "allowlist_unused": ["a"]}}}), [], "no visited/mapped counts, no claim")
        # A foreign artifact with non-string entries must not kill the tick:
        # a list that is not all strings is no verdict at all.
        for odd in ([["a"]], ["a", 1], [{"x": 1}]):
            artifact = {"mode": "all", "visited": 2, "mapped": 2, "outcomes": {"p1": {"outcome": "applied", "detail": "", "allowlist_unused": odd}, "p2": {"outcome": "applied", "detail": "", "allowlist_unused": ["a"]}}}
            self.assertEqual(periodics.reconcile_detail(artifact), [], odd)

    def test_the_failed_markers_are_the_words_the_script_writes(self):
        # The reader classifies a `failed` by words of the writer's reasons.
        # Nothing else holds the two files together: a reason reworded in
        # hack/fleet_reconcile.py and not here would restore "nothing by
        # hand" beside a detail that names a hand step.
        spec = importlib.util.spec_from_file_location("fleet_reconcile_markers", REPO / "hack" / "fleet_reconcile.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        self.assertIn(periodics.RECONCILE_UNMAPPED_MARKER, script.REASON_UNMAPPED)
        self.assertIn(periodics.RECONCILE_CEILING_MARKER, script.REASON_CEILING)
        self.assertIn(periodics.RECONCILE_CEILING_MARKER, script.REASON_INTERRUPTED)
        self.assertIn(periodics.RECONCILE_RUNNER_MARKER, script.REASON_RUNNER)
        # And the ceiling words are not an instruction the runbook forbids yet.
        self.assertNotIn(": tofu force-unlock", script.REASON_CEILING + script.REASON_INTERRUPTED)

    def test_runs_carry_whether_the_build_was_a_dry_run(self):
        reading = self.reading(DAILY, NOW - timedelta(hours=1), artifact={"dry_run": True, "summary": {"planned": 3}})
        self.assertEqual(periodics.runs({DAILY.job: reading})[DAILY.job]["dry_run"], True)

    def test_no_reading_writes_no_note(self):
        self.assertEqual(periodics.assess({}, NOW, {DAILY.job: {"since": "x"}}), {})

    def test_the_detail_is_capped_on_projects_and_the_run_line_follows(self):
        outcomes = {f"kube-agents-evals-{i}": {"outcome": "failed", "detail": "x"} for i in range(1, 9)}
        lines = periodics.reconcile_detail({"outcomes": outcomes, "error": "8 project(s) not reconciled"})
        self.assertEqual(len(lines), periodics.DETAIL_LIMIT + 2)
        self.assertEqual(lines[periodics.DETAIL_LIMIT], f"and {8 - periodics.DETAIL_LIMIT} more")
        self.assertEqual(lines[-1], "run: 8 project(s) not reconciled")

    def test_a_pointer_or_listing_of_digit_lookalikes_is_not_a_build(self):
        # str.isdigit admits what int rejects; the reader keys on isdecimal.
        root = f"{periodics.LOGS_ROOT}/{SWEEP.job}"
        objects = archive(SWEEP.job, {"8": (finished(NOW - timedelta(minutes=3)), None)})
        objects[f"{root}/{periodics.POINTER}"] = "\u00b2\n"
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(periodics.fetch(pathlib.Path(tmp), watched=(SWEEP,), runner=FakeGsutil(objects), log=lambda *a, **k: warnings.append(a[0])), {})
        self.assertIn("not a build id", warnings[0])
        objects = archive(SWEEP.job, {"9": (None, None), "8": (finished(NOW - timedelta(minutes=13)), None)})
        objects[f"{root}/\u2460/started.json"] = "{}"
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(SWEEP,), runner=FakeGsutil(objects))
        self.assertEqual(readings[SWEEP.job]["build"], "8")

    def test_an_unreadable_artifact_is_a_blind_tick_and_a_cut_one_is_said(self):
        root = f"{periodics.LOGS_ROOT}/{DAILY.job}"
        report = {"outcomes": {"kube-agents-evals-3": {"outcome": "refused", "detail": "x"}}}
        objects = archive(DAILY.job, {"100": (finished(NOW - timedelta(hours=1), passed=False), report)})
        denied = FakeGsutil(objects, denied={f"{root}/100/{periodics.ARTIFACTS_DIR}/{periodics.RECONCILE_ARTIFACT}"})
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=denied, log=lambda *a, **k: warnings.append(a[0])), {})
        self.assertEqual(len(warnings), 1)
        # Present but cut short: no later tick can read it either, so the
        # failure is a reading whose detail says the report was unreadable.
        objects[f"{root}/100/{periodics.ARTIFACTS_DIR}/{periodics.RECONCILE_ARTIFACT}"] = '{"outcomes": {'
        warnings.clear()
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=FakeGsutil(objects), log=lambda *a, **k: warnings.append(a[0]))
        self.assertIn("not a JSON object", warnings[0])
        self.assertEqual(periodics.reconcile_detail(readings[DAILY.job]["artifact"]), [f"run: {periodics.REPORT_UNREADABLE}"])
        # A passed build's report is read too: the recovery message says what the run did.
        passed = FakeGsutil(archive(DAILY.job, {"100": (finished(NOW - timedelta(hours=1), passed=True), report)}))
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=passed)
        self.assertEqual(readings[DAILY.job]["artifact"], report)
        # A run that wrote no artifact is a reading without one.
        absent = archive(DAILY.job, {"100": (finished(NOW - timedelta(hours=1), passed=False), None)})
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(DAILY,), runner=FakeGsutil(absent))
        self.assertIsNone(readings[DAILY.job]["artifact"])

    def test_a_stale_note_without_a_finish_time_says_the_window_cannot_be_measured(self):
        readings = {DAILY.job: {"job": DAILY.job, "build": "5", "finished_at": None, "passed": True, "result": "SUCCESS"}}
        note = periodics.assess(readings, NOW, {})[DAILY.job]
        self.assertEqual(note["verdict"], periodics.VERDICT_STALE)
        self.assertIn("cannot be measured", periodics.evidence(note))
        self.assertNotIn("finished nothing", periodics.evidence(note))

    def test_a_listing_that_fails_and_a_bad_pointer_are_said(self):
        root = f"{periodics.LOGS_ROOT}/{SWEEP.job}"
        objects = archive(SWEEP.job, {"9": (None, None), "8": (finished(NOW - timedelta(minutes=13)), None)})
        warnings = []
        denied = FakeGsutil(objects, denied={f"{root}/"})
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(periodics.fetch(pathlib.Path(tmp), watched=(SWEEP,), runner=denied, log=lambda *a, **k: warnings.append(a[0])), {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not list", warnings[0])
        objects[f"{root}/{periodics.POINTER}"] = "\n"
        warnings.clear()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(periodics.fetch(pathlib.Path(tmp), watched=(SWEEP,), runner=FakeGsutil(objects), log=lambda *a, **k: warnings.append(a[0])), {})
        self.assertIn("not a build id", warnings[0])

    def test_streaks_count_consecutive_failed_checks_per_project_and_per_run(self):
        def reading(build, passed, outcomes):
            return {"job": SWEEP.job, "build": build, "finished_at": NOW.isoformat(timespec="seconds"), "passed": passed, "result": "SUCCESS" if passed else "FAILURE",
                    "artifact": {"projects": len(outcomes), "closed": 0, "failed": 0, "left_for_next_run": 0, "outcomes": outcomes}}
        fail = {"error": "HTTP 401 Unauthorized: owner mismatch"}
        ok = {"closed": 1}
        one = periodics.streaks({SWEEP.job: reading("1", False, {"kube-agents-evals-3": fail, "kube-agents-evals-4": ok})}, None)
        self.assertEqual(one[SWEEP.job], {"build": "1", "projects": {"kube-agents-evals-3": 1}, "runs": 1})
        # The same build read again on the next check counts nothing twice.
        self.assertEqual(periodics.streaks({SWEEP.job: reading("1", False, {"kube-agents-evals-3": fail})}, one)[SWEEP.job], one[SWEEP.job])
        # A new failed build: evals-3 again (2), evals-5 (1); evals-4, not reached, had nothing to keep.
        two = periodics.streaks({SWEEP.job: reading("2", False, {"kube-agents-evals-3": fail, "kube-agents-evals-5": fail})}, one)
        self.assertEqual(two[SWEEP.job], {"build": "2", "projects": {"kube-agents-evals-3": 2, "kube-agents-evals-5": 1}, "runs": 2})
        # A failed build that did not reach evals-5 (busy) keeps its count.
        three = periodics.streaks({SWEEP.job: reading("3", False, {"kube-agents-evals-3": fail})}, two)
        self.assertEqual(three[SWEEP.job]["projects"], {"kube-agents-evals-3": 3, "kube-agents-evals-5": 1})
        # A failed build in which evals-5 succeeded drops its count while another project fails the run.
        dropped = periodics.streaks({SWEEP.job: reading("3b", False, {"kube-agents-evals-3": fail, "kube-agents-evals-5": ok})}, three)
        self.assertEqual(dropped[SWEEP.job]["projects"], {"kube-agents-evals-3": 4})
        # A clean build clears every count: a project it did not reach was busy, not failing.
        four = periodics.streaks({SWEEP.job: reading("4", True, {"kube-agents-evals-3": ok})}, three)
        self.assertEqual(four[SWEEP.job], {"build": "4", "projects": {}, "runs": 0})
        # No reading keeps everything.
        self.assertEqual(periodics.streaks({}, three)[SWEEP.job], three[SWEEP.job])

    def test_a_single_failed_sweep_check_is_not_news_and_two_in_a_row_are(self):
        def reading(build, passed, outcomes):
            return {"job": SWEEP.job, "build": build, "finished_at": NOW.isoformat(timespec="seconds"), "passed": passed, "result": "SUCCESS" if passed else "FAILURE",
                    "artifact": {"projects": len(outcomes), "closed": 0, "failed": sum(1 for o in outcomes.values() if "error" in o), "left_for_next_run": 0, "outcomes": outcomes}}
        fail = {"error": "HTTP 401 Unauthorized: owner mismatch request by x, currently owned by "}
        readings = {SWEEP.job: reading("1", False, {"kube-agents-evals-3": fail})}
        streaks = periodics.streaks(readings, None)
        self.assertEqual(periodics.assess(readings, NOW, None, streaks=streaks), {}, "one flap is not news")
        # The second consecutive failed check is; a project that failed in both leads the detail with its count.
        readings = {SWEEP.job: reading("2", False, {"kube-agents-evals-3": fail, "kube-agents-evals-4": fail})}
        streaks = periodics.streaks(readings, streaks)
        note = periodics.assess(readings, NOW, None, streaks=streaks)[SWEEP.job]
        self.assertEqual(note["detail"], [
            "kube-agents-evals-3: failed in 2 consecutive checks (HTTP 401 Unauthorized: owner mismatch request by x, currently owned by )",
            "kube-agents-evals-4: HTTP 401 Unauthorized: owner mismatch request by x, currently owned by ",
        ])
        # A project with an old count that this build did not reach is not named as persisting.
        readings = {SWEEP.job: reading("3", False, {"kube-agents-evals-4": fail})}
        streaks = periodics.streaks(readings, streaks)
        self.assertEqual(periodics.assess(readings, NOW, None, streaks=streaks)[SWEEP.job]["detail"][0], "kube-agents-evals-4: failed in 2 consecutive checks (HTTP 401 Unauthorized: owner mismatch request by x, currently owned by )")
        # The cap bounds the merged list, and the run's own lines follow it.
        many = {f"kube-agents-evals-{n}": fail for n in range(10, 22)}
        readings = {SWEEP.job: dict(reading("4", False, many), artifact=dict(reading("4", False, many)["artifact"], left_for_next_run=7))}
        streaks = periodics.streaks(readings, streaks)
        detail = periodics.assess(readings, NOW, None, streaks=streaks)[SWEEP.job]["detail"]
        self.assertEqual(detail[periodics.DETAIL_LIMIT], "and 7 more")
        self.assertEqual(detail[-1], "7 write(s) left for the next run (the run's write budget)")
        self.assertEqual(len(detail), periodics.DETAIL_LIMIT + 2)
        # A reconcile's first failed build is news, with its own lines untouched.
        weekly = {"job": DAILY.job, "build": "7", "finished_at": NOW.isoformat(timespec="seconds"), "passed": False, "result": "FAILURE",
                  "artifact": {"outcomes": {"kube-agents-evals-3": {"outcome": "refused", "detail": "delete x"}}}}
        streaks = periodics.streaks({DAILY.job: weekly}, None)
        self.assertEqual(periodics.assess({DAILY.job: weekly}, NOW, None, streaks=streaks)[DAILY.job]["detail"], [f"kube-agents-evals-3: refused (delete x); next: {periodics.RECONCILE_NEXT_STEP['refused']}"])
        # Without streaks (a caller that has none), a failed build is a note as before.
        self.assertIn(SWEEP.job, periodics.assess({SWEEP.job: reading("1", False, {"kube-agents-evals-3": fail})}, NOW, None))


class WorkflowWiring(unittest.TestCase):
    def test_the_15_minute_tick_fetches_the_readings_and_hands_them_to_health(self):
        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        steps = jobs["refresh-and-adjudicate"]["steps"]
        names = [step.get("name") for step in steps]
        fetch = next(step for step in steps if "periodics.py fetch" in step.get("run", ""))
        self.assertTrue(fetch.get("continue-on-error"), "a failed fetch never fails the tick")
        self.assertTrue(fetch.get("timeout-minutes"), "a hung read cannot eat the verdict's half of the job")
        self.assertIn("--out-dir work/periodics", fetch["run"])
        adjudicate = next(step for step in steps if step.get("name") == "Adjudicate")
        self.assertIn("--periodics-dir work/periodics", adjudicate["run"])
        self.assertLess(names.index(fetch["name"]), names.index("Adjudicate"))

    def test_the_sweeps_report_gives_the_detail_and_the_summary(self):
        report = {"projects": 11, "closed": 0, "failed": 11, "left_for_next_run": 0, "ended_early": "GitHub refused PATCH twice (HTTP 403 Forbidden: secondary rate limit)",
                  "outcomes": {"kube-agents-evals-%d" % n: {"error": "HTTP 403 Forbidden: secondary rate limit"} for n in range(2, 13)}}
        lines = periodics.sweep_detail(report)
        self.assertEqual(lines[0], "kube-agents-evals-10: HTTP 403 Forbidden: secondary rate limit")
        self.assertEqual(lines[periodics.DETAIL_LIMIT], "and 6 more")
        self.assertEqual(lines[-1], "run ended early: GitHub refused PATCH twice (HTTP 403 Forbidden: secondary rate limit)")
        self.assertEqual(periodics.run_summary(SWEEP, report, passed=False), "failed in 11 of 11 project(s)")
        partial = {"projects": 1, "closed": 2, "failed": 1, "left_for_next_run": 0, "outcomes": {"kube-agents-evals-7": {"closed": 2, "error": "GitHub refused PATCH twice"}}}
        self.assertEqual(periodics.run_summary(SWEEP, partial, passed=False), "failed in 1 of 1 project(s) after closing 2 pull request(s)")
        drained = {"projects": 12, "closed": 241, "failed": 0, "left_for_next_run": 30, "ended_early": None, "outcomes": {}}
        self.assertEqual(periodics.run_summary(SWEEP, drained, passed=True), "closed 241 pull request(s) across 12 project(s), 30 write(s) left for the next run")
        stopped = {"projects": 1, "closed": 0, "failed": 1, "left_for_next_run": 0, "ended_early": "GitHub refused PATCH twice", "skipped": ["kube-agents-evals-3", "kube-agents-evals-4"], "outcomes": {"kube-agents-evals-2": {"error": "GitHub refused PATCH twice"}}}
        self.assertEqual(periodics.run_summary(SWEEP, stopped, passed=False), "failed in 1 of 1 project(s), 2 not swept")
        self.assertIn("2 project(s) not swept after the run stopped: kube-agents-evals-3, kube-agents-evals-4", periodics.sweep_detail(stopped))
        killed = {"exit": "terminated", "projects": 2, "closed": 7, "failed": 0, "left_for_next_run": 0, "outcomes": {}}
        self.assertEqual(periodics.run_summary(SWEEP, killed, passed=False), "terminated after closing 7 pull request(s) across 2 project(s)")
        # Failed above the project level (Boskos unreachable, the mapping): not the success wording.
        above = {"exit": "failed", "projects": 7, "closed": 7, "failed": 0, "left_for_next_run": 0, "error": "could not reach a service (OSError: ...)", "outcomes": {}}
        self.assertEqual(periodics.run_summary(SWEEP, above, passed=False), "the run failed after closing 7 pull request(s) across 7 project(s)")
        self.assertEqual(periodics.sweep_detail(above), ["run: could not reach a service (OSError: ...)"])
        self.assertEqual(periodics.sweep_detail(drained), ["30 write(s) left for the next run (the run's write budget)"])
        self.assertEqual(periodics.run_summary(DAILY, {"summary": {"applied": 3, "unchanged": 9, "refused": 0}}, passed=True), "3 applied, 9 unchanged")
        self.assertEqual(periodics.run_summary(DAILY, {"summary": {}}, passed=True), "nothing to do")
        # A failed reconcile build never reads as success: the named failures
        # lead, or the run failed above the projects.
        self.assertEqual(periodics.run_summary(DAILY, {"summary": {"unchanged": 9, "refused": 3, "failed": 1}}, passed=False), "3 refused, 1 failed, 9 unchanged")
        self.assertEqual(periodics.run_summary(DAILY, {"summary": {"unchanged": 9}}, passed=False), "the run failed after 9 unchanged")
        self.assertEqual(periodics.run_summary(DAILY, {"summary": {}}, passed=False), "the run failed before reaching a project")
        self.assertIsNone(periodics.run_summary(DAILY, None, passed=True))

    def test_a_failed_sweep_note_carries_its_report_and_a_passed_one_its_summary(self):
        report = {"projects": 3, "closed": 1, "failed": 2, "left_for_next_run": 0, "ended_early": None, "outcomes": {"kube-agents-evals-2": {"error": "HTTP 403 Forbidden: x"}, "kube-agents-evals-3": {"closed": 1}, "kube-agents-evals-4": {"error": "HTTP 502 Bad Gateway"}}}
        failed = {"job": SWEEP.job, "build": "100", "finished_at": (NOW - timedelta(minutes=5)).isoformat(timespec="seconds"), "passed": False, "result": "FAILURE", "artifact": report}
        notes = periodics.assess({SWEEP.job: failed}, NOW, None)
        note = notes[SWEEP.job]
        self.assertEqual(note["summary"], "failed in 2 of 3 project(s) after closing 1 pull request(s)")
        self.assertEqual(note["detail"], ["kube-agents-evals-2: HTTP 403 Forbidden: x", "kube-agents-evals-4: HTTP 502 Bad Gateway"])
        self.assertEqual((note["place"], note["absence"]), ("Eval GitOps repos", "leftover pull requests from eval runs are not being cleaned up"))
        self.assertTrue(note["runbook"].endswith("#55-the-repository-reset-and-the-sweep-behind-it"))
        passed = {"job": SWEEP.job, "build": "100", "finished_at": NOW.isoformat(timespec="seconds"), "passed": True, "result": "SUCCESS", "artifact": {"projects": 12, "closed": 241, "failed": 0, "left_for_next_run": 0, "outcomes": {}}}
        runs = periodics.runs({SWEEP.job: passed})
        self.assertEqual(runs[SWEEP.job], {"build": "100", "finished_at": NOW.isoformat(timespec="seconds"), "passed": True, "summary": "closed 241 pull request(s) across 12 project(s)", "dry_run": False})

    def test_every_watched_job_has_its_words_and_a_runbook_section_that_exists(self):
        headings = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "ci-pool-projects.md").read_text().splitlines()
        # GitHub's anchor: lowercase, spaces to hyphens, punctuation dropped
        # except hyphens and underscores; lines inside fenced code are not headings.
        slugs = set()
        fenced = False
        for line in headings:
            if line.startswith("```"):
                fenced = not fenced
                continue
            if not fenced and line.startswith("#"):
                text = line.lstrip("#").strip().lower()
                slugs.add("".join(ch for ch in text.replace(" ", "-") if ch.isalnum() or ch in "-_"))
        for periodic in periodics.WATCHED:
            for field in ("place", "absence", "presence", "does", "effect", "runbook"):
                self.assertTrue(getattr(periodic, field), f"{periodic.job} has no {field}")
            self.assertTrue(periodic.runbook.startswith(periodics.RUNBOOK_ROOT + "docs/ci-pool-projects.md#"), periodic.runbook)
            self.assertIn(periodic.runbook.split("#", 1)[1], slugs, f"{periodic.job}'s runbook anchor names no heading")

    def test_the_watched_jobs_are_the_periodics_and_the_postsubmit(self):
        # The names are the Prow job names in oss-test-infra, which nothing here
        # can check; a rename there is a rename here.
        # The hourly and the weekly stay watched until the oss-test-infra
        # change retires them: the watch must not go dark between the merges.
        self.assertEqual(
            [p.job for p in periodics.WATCHED],
            ["ci-kube-agents-pull-sweep", "ci-kube-agents-fleet-reconcile", "ci-kube-agents-fleet-reconcile-all", "ci-kube-agents-fleet-reconcile-daily", "post-kube-agents-fleet-reconcile"],
        )
        for periodic in periodics.WATCHED:
            self.assertTrue(periodic.stale_after is None or periodic.stale_after >= timedelta(hours=1))
        self.assertIsNone(POST.stale_after, "a job that runs on merges has no cadence to be late against")
        for job in ("ci-kube-agents-fleet-reconcile", "ci-kube-agents-fleet-reconcile-all"):
            # Their last build stays in the bucket after Prow drops them; a
            # merely old one must not read as "stopped running".
            self.assertIsNone(periodics.WATCHED_BY_JOB[job].stale_after, job)
        self.assertEqual(DAILY.stale_after, timedelta(hours=36))

    def test_main_names_what_it_wrote(self):
        objects = archive(SWEEP.job, {"7": (finished(NOW - timedelta(minutes=5)), None)})
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as out:
            rc = periodics.main(["fetch", "--out-dir", tmp, "--job", SWEEP.job, "--job", POST.job], runner=FakeGsutil(objects))
        self.assertEqual(rc, 0)
        self.assertIn(f"{SWEEP.job}: build 7 SUCCESS", out.getvalue())
        self.assertIn(f"{POST.job}: no finished build", out.getvalue())
        self.assertEqual(periodics.main(["fetch", "--out-dir", "/nonexistent", "--job", "nope"]), 2)


if __name__ == "__main__":
    unittest.main()
