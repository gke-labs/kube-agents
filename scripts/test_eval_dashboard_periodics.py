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
WEEKLY = periodics.WATCHED_BY_JOB["ci-kube-agents-fleet-reconcile-all"]
HOURLY = periodics.WATCHED_BY_JOB["ci-kube-agents-fleet-reconcile"]


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
        objects = archive(WEEKLY.job, {"100": (finished(NOW - timedelta(hours=1), passed=False), report)})
        gsutil = FakeGsutil(objects)
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=gsutil)
            written = json.loads((pathlib.Path(tmp) / f"{WEEKLY.job}.json").read_text())
        self.assertEqual(readings[WEEKLY.job], written)
        self.assertEqual((written["build"], written["passed"], written["result"]), ("100", False, "FAILURE"))
        self.assertEqual(written["finished_at"], (NOW - timedelta(hours=1)).isoformat(timespec="seconds"))
        self.assertEqual(written["artifact"], report)

    def test_a_running_newest_build_falls_back_to_the_one_before_it(self):
        objects = archive(HOURLY.job, {"101": (None, None), "100": (finished(NOW - timedelta(minutes=50)), None), "99": (finished(NOW - timedelta(hours=2)), None)})
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(HOURLY,), runner=FakeGsutil(objects))
        self.assertEqual(readings[HOURLY.job]["build"], "100")
        self.assertIsNone(readings[HOURLY.job]["artifact"], "the hourly wrote no artifact for that build")

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
        objects = archive(WEEKLY.job, {"101": (aborted, None), "100": (finished(NOW - timedelta(days=1)), None)})
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=FakeGsutil(objects))
        self.assertEqual((readings[WEEKLY.job]["build"], readings[WEEKLY.job]["passed"]), ("100", True))
        self.assertEqual(periodics.assess(readings, NOW, {}), {})
        # Every build aborted: nothing to read, no note.
        only_aborted = archive(WEEKLY.job, {"101": (aborted, None)})
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=FakeGsutil(only_aborted)), {})

    def test_a_job_that_never_ran_or_whose_pointer_is_denied_writes_nothing(self):
        objects = archive(SWEEP.job, {"7": (finished(NOW - timedelta(minutes=5)), None)})
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            (out / f"{HOURLY.job}.json").write_text("stale from last tick")
            readings = periodics.fetch(out, watched=(SWEEP, HOURLY), runner=FakeGsutil(objects))
            self.assertEqual(sorted(readings), [SWEEP.job])
            self.assertFalse((out / f"{HOURLY.job}.json").exists(), "last tick's file does not survive a job with no build")
            warnings = []
            denied = FakeGsutil(objects, denied={f"{periodics.LOGS_ROOT}/{SWEEP.job}/{periodics.POINTER}"})
            readings = periodics.fetch(out, watched=(SWEEP,), runner=denied, log=lambda *a, **k: warnings.append(a[0]))
        self.assertEqual(readings, {})
        self.assertEqual(len(warnings), 1, "a denied pointer is said, a NotFound is not")
        self.assertIn("build pointer", warnings[0])

    def test_a_finished_json_that_cannot_be_read_is_a_blind_tick_not_an_older_build(self):
        # A 503 on the newest finished build must not walk back to last
        # week's and report that: nothing is written, with a warning.
        objects = archive(WEEKLY.job, {"101": (finished(NOW - timedelta(days=1)), None), "100": (finished(NOW - timedelta(days=8, hours=1)), None)})
        denied = FakeGsutil(objects, denied={f"{periodics.LOGS_ROOT}/{WEEKLY.job}/101/{periodics.FINISHED}"})
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=denied, log=lambda *a, **k: warnings.append(a[0]))
        self.assertEqual(readings, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("101/finished.json", warnings[0])

    def test_a_build_id_containing_404_in_an_error_is_not_read_as_absent(self):
        # gsutil echoes the failing URL; a 503 on .../1404/finished.json is
        # not a NotFound, and the walk must not report the build before it.
        objects = archive(WEEKLY.job, {"1404": (finished(NOW - timedelta(hours=1), passed=False), None), "1400": (finished(NOW - timedelta(days=1)), None)})
        class Flaky(FakeGsutil):
            def __call__(self, cmd, **kwargs):
                if cmd[3].endswith("/1404/finished.json"):
                    return subprocess.CompletedProcess(cmd, 1, "", f"ServiceException: 503 Backend error reading {cmd[3]}")
                return super().__call__(cmd, **kwargs)
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=Flaky(objects), log=lambda *a, **k: warnings.append(a[0])), {})
        self.assertEqual(len(warnings), 1)
        self.assertTrue(periodics._not_found("CommandException: No URLs matched: gs://x/y"))
        self.assertTrue(periodics._not_found("NotFoundException: 404 gs://x/y does not exist."))
        self.assertFalse(periodics._not_found("ServiceException: 503 on gs://kube-agents-prow/logs/j/2097891568546404123/started.json"))

    def test_a_present_but_unparseable_finished_json_is_a_blind_tick(self):
        # An empty or truncated object is not "still running": walking past it
        # would report the build before as the latest finished run.
        root = f"{periodics.LOGS_ROOT}/{WEEKLY.job}"
        objects = archive(WEEKLY.job, {"101": (None, None), "100": (finished(NOW - timedelta(days=1), passed=True), None)})
        objects[f"{root}/101/{periodics.FINISHED}"] = ""
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=FakeGsutil(objects), log=lambda *a, **k: warnings.append(a[0]))
        self.assertEqual(readings, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("101/finished.json: not JSON", warnings[0])
        objects[f"{root}/101/{periodics.FINISHED}"] = "[]"
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=FakeGsutil(objects), log=lambda *a, **k: None), {})

    def test_an_absurd_timestamp_is_no_finish_time_not_a_crash(self):
        objects = archive(HOURLY.job, {"5": ({"timestamp": 10**20, "passed": True, "result": "SUCCESS"}, None)})
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(HOURLY,), runner=FakeGsutil(objects))
        self.assertIsNone(readings[HOURLY.job]["finished_at"])
        self.assertEqual(periodics.assess(readings, NOW, {})[HOURLY.job]["verdict"], periodics.VERDICT_STALE)

    def test_load_readings_skips_a_file_it_cannot_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            (out / f"{SWEEP.job}.json").write_text("{not json")
            (out / f"{HOURLY.job}.json").write_text(json.dumps({"job": HOURLY.job, "build": "1", "finished_at": NOW.isoformat(), "passed": True, "result": "SUCCESS"}))
            self.assertEqual(sorted(periodics.load_readings(out)), [HOURLY.job])
        self.assertEqual(periodics.load_readings(None), {})


    def test_a_listed_build_is_read_under_the_name_that_was_listed(self):
        # A non-canonical decimal name is Prow's to never write; if one is
        # there, it is read as listed rather than rebuilt through int().
        objects = archive(WEEKLY.job, {"200": (None, None), "0099": (finished(NOW - timedelta(hours=1), passed=False), None)})
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=FakeGsutil(objects))
        self.assertEqual(readings[WEEKLY.job]["build"], "0099")

    def test_a_passed_builds_unreadable_report_keeps_the_reading(self):
        # The report of a passed build is only the recovery's summary: a read
        # that fails for a reason other than NotFound warns and the reading
        # stands without it, where a failed build's would blind the tick.
        root = f"{periodics.LOGS_ROOT}/{WEEKLY.job}"
        objects = archive(WEEKLY.job, {"100": (finished(NOW - timedelta(hours=1), passed=True), {"summary": {"applied": 1}})})
        denied = FakeGsutil(objects, denied={f"{root}/100/{periodics.ARTIFACTS_DIR}/{periodics.RECONCILE_ARTIFACT}"})
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=denied, log=lambda *a, **k: warnings.append(a[0]))
        self.assertEqual((readings[WEEKLY.job]["build"], readings[WEEKLY.job]["artifact"]), ("100", None))
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
        readings = {SWEEP.job: self.reading(SWEEP, NOW - timedelta(minutes=5)), WEEKLY.job: self.reading(WEEKLY, NOW - timedelta(days=3))}
        self.assertEqual(periodics.assess(readings, NOW, {}), {})

    def test_a_failed_build_is_a_note_naming_the_refused_projects(self):
        artifact = {"dry_run": False, "outcomes": {"kube-agents-evals-3": {"outcome": "refused", "detail": "not a create or an in-place update: delete google_container_cluster.seeded_b"}, "kube-agents-evals-4": {"outcome": "applied", "detail": "2 to add"}}, "error": "1 project(s) not reconciled: kube-agents-evals-3"}
        readings = {WEEKLY.job: self.reading(WEEKLY, NOW - timedelta(hours=1), passed=False, artifact=artifact)}
        notes = periodics.assess(readings, NOW, {})
        note = notes[WEEKLY.job]
        self.assertEqual((note["verdict"], note["build"], note["since"], note["dry_run"]), (periodics.VERDICT_FAILED, "100", NOW.isoformat(timespec="seconds"), False))
        self.assertEqual(note["detail"], ["kube-agents-evals-3: refused (not a create or an in-place update: delete google_container_cluster.seeded_b)", "run: 1 project(s) not reconciled: kube-agents-evals-3"])
        self.assertEqual(note["history_url"], f"{periodics.JOB_HISTORY_ROOT}/{WEEKLY.job}")
        self.assertIn("build 100 failed", periodics.evidence(note))
        # The next tick keeps the episode's start.
        later = periodics.assess(readings, NOW + timedelta(hours=1), notes)
        self.assertEqual(later[WEEKLY.job]["since"], note["since"])

    def test_a_job_past_its_stale_window_is_stale_whatever_its_last_verdict(self):
        readings = {WEEKLY.job: self.reading(WEEKLY, NOW - timedelta(days=9)), HOURLY.job: self.reading(HOURLY, NOW - timedelta(hours=2, minutes=59))}
        notes = periodics.assess(readings, NOW, {})
        self.assertEqual(sorted(notes), [WEEKLY.job])
        self.assertEqual(notes[WEEKLY.job]["verdict"], periodics.VERDICT_STALE)
        self.assertIn("no finished run since", periodics.evidence(notes[WEEKLY.job]))

    def test_no_reading_writes_no_note(self):
        self.assertEqual(periodics.assess({}, NOW, {WEEKLY.job: {"since": "x"}}), {})

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
        root = f"{periodics.LOGS_ROOT}/{WEEKLY.job}"
        report = {"outcomes": {"kube-agents-evals-3": {"outcome": "refused", "detail": "x"}}}
        objects = archive(WEEKLY.job, {"100": (finished(NOW - timedelta(hours=1), passed=False), report)})
        denied = FakeGsutil(objects, denied={f"{root}/100/{periodics.ARTIFACTS_DIR}/{periodics.RECONCILE_ARTIFACT}"})
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=denied, log=lambda *a, **k: warnings.append(a[0])), {})
        self.assertEqual(len(warnings), 1)
        # Present but cut short: no later tick can read it either, so the
        # failure is a reading whose detail says the report was unreadable.
        objects[f"{root}/100/{periodics.ARTIFACTS_DIR}/{periodics.RECONCILE_ARTIFACT}"] = '{"outcomes": {'
        warnings.clear()
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=FakeGsutil(objects), log=lambda *a, **k: warnings.append(a[0]))
        self.assertIn("not a JSON object", warnings[0])
        self.assertEqual(periodics.reconcile_detail(readings[WEEKLY.job]["artifact"]), [f"run: {periodics.REPORT_UNREADABLE}"])
        # A passed build's report is read too: the recovery message says what the run did.
        passed = FakeGsutil(archive(WEEKLY.job, {"100": (finished(NOW - timedelta(hours=1), passed=True), report)}))
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=passed)
        self.assertEqual(readings[WEEKLY.job]["artifact"], report)
        # A run that wrote no artifact is a reading without one.
        absent = archive(WEEKLY.job, {"100": (finished(NOW - timedelta(hours=1), passed=False), None)})
        with tempfile.TemporaryDirectory() as tmp:
            readings = periodics.fetch(pathlib.Path(tmp), watched=(WEEKLY,), runner=FakeGsutil(absent))
        self.assertIsNone(readings[WEEKLY.job]["artifact"])

    def test_a_stale_note_without_a_finish_time_says_the_window_cannot_be_measured(self):
        readings = {HOURLY.job: {"job": HOURLY.job, "build": "5", "finished_at": None, "passed": True, "result": "SUCCESS"}}
        note = periodics.assess(readings, NOW, {})[HOURLY.job]
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
        weekly = {"job": WEEKLY.job, "build": "7", "finished_at": NOW.isoformat(timespec="seconds"), "passed": False, "result": "FAILURE",
                  "artifact": {"outcomes": {"kube-agents-evals-3": {"outcome": "refused", "detail": "delete x"}}}}
        streaks = periodics.streaks({WEEKLY.job: weekly}, None)
        self.assertEqual(periodics.assess({WEEKLY.job: weekly}, NOW, None, streaks=streaks)[WEEKLY.job]["detail"], ["kube-agents-evals-3: refused (delete x)"])
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
        self.assertEqual(periodics.run_summary(WEEKLY, {"summary": {"applied": 3, "unchanged": 9, "refused": 0}}, passed=True), "3 applied, 9 unchanged")
        self.assertEqual(periodics.run_summary(WEEKLY, {"summary": {}}, passed=True), "nothing to do")
        # A failed reconcile build never reads as success: the named failures
        # lead, or the run failed above the projects.
        self.assertEqual(periodics.run_summary(WEEKLY, {"summary": {"unchanged": 9, "refused": 3, "failed": 1}}, passed=False), "3 refused, 1 failed, 9 unchanged")
        self.assertEqual(periodics.run_summary(WEEKLY, {"summary": {"unchanged": 9}}, passed=False), "the run failed after 9 unchanged")
        self.assertEqual(periodics.run_summary(WEEKLY, {"summary": {}}, passed=False), "the run failed before reaching a project")
        self.assertIsNone(periodics.run_summary(WEEKLY, None, passed=True))

    def test_a_failed_sweep_note_carries_its_report_and_a_passed_one_its_summary(self):
        report = {"projects": 3, "closed": 1, "failed": 2, "left_for_next_run": 0, "ended_early": None, "outcomes": {"kube-agents-evals-2": {"error": "HTTP 403 Forbidden: x"}, "kube-agents-evals-3": {"closed": 1}, "kube-agents-evals-4": {"error": "HTTP 502 Bad Gateway"}}}
        failed = {"job": SWEEP.job, "build": "100", "finished_at": (NOW - timedelta(minutes=5)).isoformat(timespec="seconds"), "passed": False, "result": "FAILURE", "artifact": report}
        notes = periodics.assess({SWEEP.job: failed}, NOW, None)
        note = notes[SWEEP.job]
        self.assertEqual(note["summary"], "failed in 2 of 3 project(s) after closing 1 pull request(s)")
        self.assertEqual(note["detail"], ["kube-agents-evals-2: HTTP 403 Forbidden: x", "kube-agents-evals-4: HTTP 502 Bad Gateway"])
        self.assertEqual((note["place"], note["absence"]), ("Eval GitOps repos", "leftover pull requests from eval runs are not being cleaned up"))
        self.assertTrue(note["runbook"].endswith("#55-the-pull-request-sweep"))
        passed = {"job": SWEEP.job, "build": "100", "finished_at": NOW.isoformat(timespec="seconds"), "passed": True, "result": "SUCCESS", "artifact": {"projects": 12, "closed": 241, "failed": 0, "left_for_next_run": 0, "outcomes": {}}}
        runs = periodics.runs({SWEEP.job: passed})
        self.assertEqual(runs[SWEEP.job], {"build": "100", "finished_at": NOW.isoformat(timespec="seconds"), "passed": True, "summary": "closed 241 pull request(s) across 12 project(s)"})

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

    def test_the_watched_jobs_are_the_three_periodics(self):
        # The names are the Prow job names in oss-test-infra, which nothing here
        # can check; a rename there is a rename here.
        self.assertEqual([p.job for p in periodics.WATCHED], ["ci-kube-agents-pull-sweep", "ci-kube-agents-fleet-reconcile", "ci-kube-agents-fleet-reconcile-all"])
        for periodic in periodics.WATCHED:
            self.assertTrue(periodic.stale_after >= timedelta(hours=1))

    def test_main_names_what_it_wrote(self):
        objects = archive(SWEEP.job, {"7": (finished(NOW - timedelta(minutes=5)), None)})
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as out:
            rc = periodics.main(["fetch", "--out-dir", tmp, "--job", SWEEP.job, "--job", HOURLY.job], runner=FakeGsutil(objects))
        self.assertEqual(rc, 0)
        self.assertIn(f"{SWEEP.job}: build 7 SUCCESS", out.getvalue())
        self.assertIn(f"{HOURLY.job}: no finished build", out.getvalue())
        self.assertEqual(periodics.main(["fetch", "--out-dir", "/nonexistent", "--job", "nope"]), 2)


if __name__ == "__main__":
    unittest.main()
