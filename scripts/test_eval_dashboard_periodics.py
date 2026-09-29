"""scripts/eval_dashboard/periodics.py: the watched Prow periodics' latest
finished builds, read from the archive, and the notes health.py carries.

* `fetch` reads the pointer, walks back to a finished build, keeps the
  artifact when the job writes one, writes one <job>.json per job with a
  build, and nothing for a job that never ran or whose pointer is denied;
* `assess` notes a failed build and a job whose last finished build is older
  than its stale window, keeps `since` across ticks, and names the reconcile
  artifact's refused and failed projects;
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
            objects[f"{root}/{build}/{periodics.ARTIFACTS_DIR}/{periodics.RECONCILE_ARTIFACT}"] = json.dumps(artifact)
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
        self.assertEqual([c[2] for c in gsutil.calls], ["cat", "cat"], "pointer and finished.json only, no ls")

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

    def test_the_watched_jobs_are_the_periodics_in_oss_test_infra(self):
        # The names are the Prow job names; a rename there is a rename here.
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
