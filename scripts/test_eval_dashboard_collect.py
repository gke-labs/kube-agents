"""The eval-dashboard collector parses real Prow logs into the data.json contract.

The fixtures under scripts/eval_dashboard/testdata/ are REAL
pull-kube-agents-smoke-test builds (PRs 956 and 998), trimmed to the eval
section: the lease line, every `Task ... Result:` line, and the final verdict
where the build reached one. Their started.json/finished.json are the real
Prow uploads, verbatim. That makes these tests the proof that the parser
handles what the presubmit actually prints -- including the build where
resource preparation failed (an INFRA result, which must never count against
a case) and the build the Prow deadline truncated before a verdict line.

data.json is a contract two sibling dashboard PRs build against; the
assertions here pin the exact field names and derivation rules SCHEMA.md
documents, so a drive-by "improvement" to the collector fails here before it
breaks a renderer built in parallel.
"""

import contextlib
import io
import json
import os
import pathlib
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from eval_dashboard import collect

TESTDATA = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "testdata"

# The three real builds, oldest first (started.json timestamps).
BUILD_956_TRUNCATED = "2092688354838581248"  # PR 956, deadline hit before verdict
BUILD_998_INFRA = "2093030474753511424"  # PR 998, compliance canary infra-failed
BUILD_998_FULL = "2093054394793725952"  # PR 998, all 14 executed tasks graded


def _runs_by_id():
    return {run["build_id"]: run for run in collect.runs_from_dir(TESTDATA)}


def _cases_by_name(data):
    return {case["name"]: case for case in data["cases"]}


class TestFixtureParsing(unittest.TestCase):
    def test_full_run_998(self):
        run = _runs_by_id()[BUILD_998_FULL]
        self.assertEqual(run["pr"], 998)
        self.assertEqual(run["head_sha"], "a28f0b3")
        self.assertEqual(run["project"], "kube-agents-evals-2")
        self.assertEqual(run["result"], "FAILURE")
        # The verdict line's Total Duration, not the Prow timestamp delta.
        self.assertEqual(run["duration_s"], 5793)
        self.assertEqual(run["started"], "2026-08-27T19:13:55+00:00")
        self.assertEqual(run["finished"], "2026-08-27T21:07:53+00:00")
        self.assertEqual(len(run["tasks"]), 14)
        by_name = {t["name"]: t for t in run["tasks"]}
        self.assertEqual(
            by_name["reliability-pdb-probe"],
            {
                "name": "reliability-pdb-probe",
                "result": "pass",
                "duration_s": 182,
                "outcome_validity": 1.0,
            },
        )
        self.assertEqual(by_name["capacity-pinned-pool-probe"]["result"], "fail")
        self.assertEqual(by_name["capacity-pinned-pool-probe"]["duration_s"], 129)
        self.assertEqual(by_name["capacity-pinned-pool-probe"]["outcome_validity"], 0.0)
        self.assertEqual(by_name["upgrades-lagging-master-probe"]["outcome_validity"], 0.8)
        results = [t["result"] for t in run["tasks"]]
        self.assertEqual(results.count("pass"), 12)
        self.assertEqual(results.count("fail"), 2)

    def test_infra_run_998(self):
        run = _runs_by_id()[BUILD_998_INFRA]
        self.assertEqual(run["pr"], 998)
        self.assertEqual(run["head_sha"], "b336c6c")
        self.assertEqual(run["project"], "kube-agents-evals-5")
        self.assertEqual(run["duration_s"], 2143)
        self.assertEqual(len(run["tasks"]), 11)
        by_name = {t["name"]: t for t in run["tasks"]}
        infra = by_name["compliance-rbac-overgrant"]
        self.assertEqual(infra["result"], "infra")
        self.assertEqual(infra["duration_s"], 41)
        # No grade was recorded for a task the infrastructure never ran.
        self.assertIsNone(infra["outcome_validity"])
        results = [t["result"] for t in run["tasks"]]
        self.assertEqual((results.count("pass"), results.count("fail"), results.count("infra")), (8, 2, 1))

    def test_truncated_run_956_yields_partial_run(self):
        """A log the Prow deadline cut off is a partial run, not an exception."""
        run = _runs_by_id()[BUILD_956_TRUNCATED]
        self.assertEqual(run["pr"], 956)
        self.assertEqual(run["head_sha"], "13b2c71")
        self.assertEqual(run["project"], "kube-agents-evals-3")
        self.assertEqual(run["result"], "FAILURE")
        # No verdict line -> fall back to finished-started timestamps.
        self.assertEqual(run["duration_s"], 1787775335 - 1787770764)
        self.assertEqual(len(run["tasks"]), 5)
        results = [t["result"] for t in run["tasks"]]
        self.assertEqual((results.count("pass"), results.count("fail")), (2, 3))

    def test_runs_sorted_oldest_first(self):
        data = collect.collect(from_dir=TESTDATA)
        self.assertEqual(
            [run["build_id"] for run in data["runs"]],
            [BUILD_956_TRUNCATED, BUILD_998_INFRA, BUILD_998_FULL],
        )


class TestCaseDerivation(unittest.TestCase):
    def setUp(self):
        self.data = collect.collect(from_dir=TESTDATA)
        self.cases = _cases_by_name(self.data)

    def test_case_count_is_union_of_task_names(self):
        self.assertEqual(len(self.cases), 18)

    def test_infra_never_counts_against_a_case(self):
        """compliance-rbac-overgrant: pass, infra, fail across the fixtures.

        The infra run appears in runs_on_record and last3 (it is history) but
        is excluded from the pass_rate denominator and the duration stats.
        """
        case = self.cases["compliance-rbac-overgrant"]
        self.assertEqual(case["runs_on_record"], 3)
        self.assertEqual(case["pass_rate"], 0.5)  # 1 pass / 2 graded, NOT /3
        self.assertEqual(case["last3"], ["pass", "infra", "fail"])
        self.assertEqual(case["durations"], {"min": 606, "med": 1238, "max": 1870})
        self.assertEqual(
            case["ov_history"],
            [
                {"build_id": BUILD_956_TRUNCATED, "value": 0.1},
                {"build_id": BUILD_998_FULL, "value": 0.0},
            ],
        )

    def test_clean_case(self):
        case = self.cases["reliability-pdb-probe"]
        self.assertEqual(case["domain"], "reliability")
        self.assertTrue(case["active"])
        self.assertEqual(case["runs_on_record"], 2)
        self.assertEqual(case["pass_rate"], 1.0)
        self.assertEqual(case["last3"], ["pass", "pass"])
        self.assertEqual(case["durations"], {"min": 168, "med": 175, "max": 182})
        self.assertEqual(
            case["ov_history"],
            [
                {"build_id": BUILD_998_INFRA, "value": 1.0},
                {"build_id": BUILD_998_FULL, "value": 1.0},
            ],
        )

    def test_retired_task_is_inactive_but_kept(self):
        """A case only historical runs mention stays on record, active: false."""
        case = self.cases["obtainability-planted-pdb"]
        self.assertFalse(case["active"])
        self.assertEqual(case["runs_on_record"], 1)

    def test_unknown_task_name_never_crashes(self):
        log = (
            "✓ Successfully leased project: kube-agents-evals-9\n"
            "Task task-renamed-long-ago Result: [PASSED] exact checks green; "
            "OutcomeValidity recorded: 1.0 (Duration: 10s)\n"
        )
        run = {"build_id": "1", "started": "x", "tasks": collect.parse_build_log(log)["tasks"]}
        (case,) = collect.build_cases([run])
        self.assertEqual(case["name"], "task-renamed-long-ago")
        self.assertEqual(case["domain"], "unknown")
        self.assertFalse(case["active"])
        # Hostile-looking names stay a lookup miss, not a path traversal.
        self.assertEqual(collect.task_domain("../../etc/passwd"), "unknown")

    def test_only_infra_on_record_means_no_pass_rate(self):
        log = (
            "Task some-case Result: [RESOURCE_PREPARATION_FAILED] "
            "Infrastructure setup/teardown or agent transport error (Duration: 41s)\n"
        )
        run = {"build_id": "1", "started": "x", "tasks": collect.parse_build_log(log)["tasks"]}
        (case,) = collect.build_cases([run])
        self.assertEqual(case["runs_on_record"], 1)
        self.assertIsNone(case["pass_rate"])
        self.assertEqual(case["durations"], {"min": None, "med": None, "max": None})
        self.assertEqual(case["ov_history"], [])


class TestResilience(unittest.TestCase):
    def test_garbage_log_parses_to_nothing(self):
        parsed = collect.parse_build_log("no eval here\n\x00\xff{]] Task Result:\n")
        self.assertEqual(parsed["tasks"], [])
        self.assertIsNone(parsed["project"])
        self.assertIsNone(parsed["eval_verdict"])

    def test_build_without_finished_json_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = pathlib.Path(tmp) / "123"
            build.mkdir()
            (build / "build-log.txt").write_text("Task a Result: [PASSED] (Duration: 1s)\n")
            self.assertEqual(collect.runs_from_dir(pathlib.Path(tmp)), [])

    def test_corrupt_finished_json_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = pathlib.Path(tmp) / "123"
            build.mkdir()
            (build / "finished.json").write_text("{not json")
            self.assertEqual(collect.runs_from_dir(pathlib.Path(tmp)), [])


# A stand-in gsutil for the incremental-scan tests: serves the fixture builds
# from a local tree laid out like the Prow bucket and appends every argv to a
# log file, so a test can assert exactly which objects a sweep paid for.
_FAKE_GSUTIL = r"""#!/usr/bin/env python3
import os, pathlib, sys

root = pathlib.Path(os.environ["FAKE_GSUTIL_ROOT"])
with open(os.environ["FAKE_GSUTIL_LOG"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
BUCKET = "gs://fake-prow/"

def local(url):
    return root / url[len(BUCKET):]

if sys.argv[1] == "ls":
    base = local(sys.argv[2].rstrip("*"))
    if not base.is_dir():
        sys.exit(1)
    for p in sorted(base.iterdir()):
        if p.is_dir():
            print(BUCKET + p.relative_to(root).as_posix() + "/")
    sys.exit(0)
if sys.argv[1] == "cat":
    try:
        sys.stdout.write(local(sys.argv[2]).read_text())
    except OSError:
        sys.exit(1)
    sys.exit(0)
sys.exit(2)
"""

FAKE_GLOB = "gs://fake-prow/pull/gke-labs_kube-agents/998/pull-kube-agents-smoke-test/*"


class _MergeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write_prior(self, data, name="prior.json") -> str:
        path = self.tmp / name
        path.write_text(json.dumps(data))
        return str(path)

    def fake_gsutil(self, builds) -> tuple[str, pathlib.Path]:
        """A gsutil serving `builds` (fixture ids) plus the call log's path."""
        root = self.tmp / "bucket"
        for build in builds:
            dst = root / FAKE_GLOB[len("gs://fake-prow/"):].rstrip("*") / build
            shutil.copytree(TESTDATA / build, dst)
        gsutil = self.tmp / "fake-gsutil"
        gsutil.write_text(_FAKE_GSUTIL)
        gsutil.chmod(gsutil.stat().st_mode | stat.S_IXUSR)
        log = self.tmp / "gsutil-calls.log"
        log.write_text("")
        os.environ["FAKE_GSUTIL_ROOT"] = str(root)
        os.environ["FAKE_GSUTIL_LOG"] = str(log)
        self.addCleanup(os.environ.pop, "FAKE_GSUTIL_ROOT", None)
        self.addCleanup(os.environ.pop, "FAKE_GSUTIL_LOG", None)
        return str(gsutil), log

    @staticmethod
    def quiet_collect(**kwargs):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            data = collect.collect(**kwargs)
        return data, stderr.getvalue()


class TestMergeWithPrior(_MergeBase):
    """--merge-with on the three paths: usable, missing, corrupt."""

    def test_merge_dedupes_by_build_id_and_recomputes_cases(self):
        """Prior + a fully overlapping fresh sweep == one clean collect."""
        baseline = collect.collect(from_dir=TESTDATA)
        prior = self.write_prior(baseline)
        merged, _ = self.quiet_collect(from_dir=TESTDATA, merge_with=prior)
        self.assertEqual(merged["runs"], baseline["runs"])
        self.assertEqual(merged["cases"], baseline["cases"])
        self.assertEqual(merged["schema_version"], 1)

    def test_stale_after_s_is_written_only_when_asked(self):
        """The publisher owns the freshness contract; a plain collect stays
        silent so the renderer's default applies."""
        plain = collect.collect(from_dir=TESTDATA)
        self.assertNotIn("stale_after_s", plain)
        tuned = collect.collect(from_dir=TESTDATA, stale_after_s=2400)
        self.assertEqual(tuned["stale_after_s"], 2400)

    def test_fresh_parse_wins_over_a_stale_prior_copy(self):
        stale = collect.collect(from_dir=TESTDATA)
        for run in stale["runs"]:
            if run["build_id"] == BUILD_998_FULL:
                run["tasks"] = []  # plausible shape, wrong content
        prior = self.write_prior(stale)
        merged, _ = self.quiet_collect(from_dir=TESTDATA, merge_with=prior)
        by_id = {run["build_id"]: run for run in merged["runs"]}
        self.assertEqual(len(by_id[BUILD_998_FULL]["tasks"]), 14)

    def test_prior_only_runs_are_carried_over_and_aggregated(self):
        baseline = collect.collect(from_dir=TESTDATA)
        retired = {
            "build_id": "1000000000000000000",  # older than every fixture
            "pr": 900,
            "head_sha": "abc1234",
            "project": "kube-agents-evals-1",
            "started": "2026-08-01T00:00:00+00:00",
            "finished": "2026-08-01T01:00:00+00:00",
            "result": "SUCCESS",
            "duration_s": 100,
            "tasks": [
                {"name": "prior-only-case", "result": "pass", "duration_s": 10, "outcome_validity": 1.0}
            ],
        }
        prior = self.write_prior({**baseline, "runs": [retired] + baseline["runs"]})
        merged, _ = self.quiet_collect(from_dir=TESTDATA, merge_with=prior)
        self.assertEqual(len(merged["runs"]), 4)
        # Oldest first, so the carried-over run leads.
        self.assertEqual(merged["runs"][0]["build_id"], retired["build_id"])
        case = _cases_by_name(merged)["prior-only-case"]
        self.assertEqual(case["pass_rate"], 1.0)
        self.assertEqual(case["domain"], "unknown")

    def test_missing_prior_degrades_to_a_bounded_fresh_sweep(self):
        merged, stderr = self.quiet_collect(
            from_dir=TESTDATA, merge_with=str(self.tmp / "never-written.json")
        )
        self.assertEqual(len(merged["runs"]), 3)
        self.assertIn("treating as a first run", stderr)
        self.assertIn(f"last {collect.DEGRADED_SINCE_DAYS:g} days", stderr)

    def test_corrupt_priors_are_discarded_never_fatal(self):
        corrupt = {
            "truncated download": '{"schema_version": 1, "runs": [{"bui',
            "wrong schema": json.dumps({"schema_version": 2, "runs": []}),
            "runs not a list": json.dumps({"schema_version": 1, "runs": {}}),
            "task missing a field the aggregation indexes": json.dumps(
                {
                    "schema_version": 1,
                    "runs": [
                        {"build_id": "5", "tasks": [{"name": "x", "result": "pass"}]}
                    ],
                }
            ),
        }
        for label, text in corrupt.items():
            with self.subTest(label):
                path = self.tmp / "bad.json"
                path.write_text(text)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertIsNone(collect.load_prior_runs(str(path)))
                self.assertIn("warning: --merge-with", stderr.getvalue())

    def test_newest_build_id_ignores_non_numeric_ids(self):
        self.assertIsNone(collect.newest_build_id([]))
        self.assertIsNone(collect.newest_build_id([{"build_id": "local-abc"}]))
        self.assertEqual(
            collect.newest_build_id(
                [{"build_id": "9"}, {"build_id": "10"}, {"build_id": "weird"}]
            ),
            10,
        )

    def test_merge_with_alone_recomputes_without_any_source(self):
        prior = self.write_prior(collect.collect(from_dir=TESTDATA))
        out = self.tmp / "out.json"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = collect.main(["--merge-with", prior, "--out", str(out)])
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out.read_text())["runs"]), 3)


class TestIncrementalGcsScan(_MergeBase):
    """The watermark and --since-days must actually save the gsutil reads."""

    def test_scan_skips_every_build_at_or_below_the_watermark(self):
        gsutil, log = self.fake_gsutil(
            [BUILD_956_TRUNCATED, BUILD_998_INFRA, BUILD_998_FULL]
        )
        with tempfile.TemporaryDirectory() as sub:
            for build in (BUILD_956_TRUNCATED, BUILD_998_INFRA):
                shutil.copytree(TESTDATA / build, pathlib.Path(sub) / build)
            prior = self.write_prior(collect.collect(from_dir=pathlib.Path(sub)))
        merged, stderr = self.quiet_collect(
            pr_globs=[FAKE_GLOB], merge_with=prior, gsutil=gsutil
        )
        self.assertEqual(
            [run["build_id"] for run in merged["runs"]],
            [BUILD_956_TRUNCATED, BUILD_998_INFRA, BUILD_998_FULL],
        )
        calls = log.read_text()
        # One listing, then reads for the ONE new build only.
        self.assertIn(f"cat gs://fake-prow/pull/gke-labs_kube-agents/998/pull-kube-agents-smoke-test/{BUILD_998_FULL}/finished.json", calls)
        self.assertNotIn(BUILD_956_TRUNCATED + "/finished.json", calls)
        self.assertNotIn(BUILD_998_INFRA + "/finished.json", calls)
        self.assertIn("merged 2 prior runs with 1 newly collected", stderr)

    def test_since_days_stops_after_the_started_probe(self):
        gsutil, log = self.fake_gsutil([BUILD_998_FULL])
        merged, _ = self.quiet_collect(
            pr_globs=[FAKE_GLOB], since_days=1, gsutil=gsutil
        )
        self.assertEqual(merged["runs"], [])
        calls = log.read_text()
        self.assertIn("started.json", calls)  # the probe was paid...
        self.assertNotIn("build-log.txt", calls)  # ...the expensive reads were not
        self.assertNotIn("finished.json", calls)

    def test_since_days_keeps_recent_builds_without_a_second_started_read(self):
        gsutil, log = self.fake_gsutil([BUILD_998_FULL])
        merged, _ = self.quiet_collect(
            pr_globs=[FAKE_GLOB], since_days=365 * 100, gsutil=gsutil
        )
        self.assertEqual(len(merged["runs"]), 1)
        calls = [c for c in log.read_text().splitlines() if "started.json" in c]
        self.assertEqual(len(calls), 1)  # probe cached, not re-fetched by build_run

    def test_in_flight_build_below_the_watermark_is_retried_via_pending(self):
        """Prow ids are monotonic by START: a long build can finish after a
        shorter, newer one is already on record. The watermark alone would
        skip it forever; the prior's pending_builds punches it through."""
        gsutil, log = self.fake_gsutil(
            [BUILD_956_TRUNCATED, BUILD_998_INFRA, BUILD_998_FULL]
        )
        with tempfile.TemporaryDirectory() as sub:
            shutil.copytree(TESTDATA / BUILD_998_FULL, pathlib.Path(sub) / BUILD_998_FULL)
            prior_data = collect.collect(from_dir=pathlib.Path(sub))
        # INFRA (a lower id than FULL) was in flight when FULL got recorded.
        prior_data["pending_builds"] = [
            {
                "build_id": BUILD_998_INFRA,
                "first_seen": datetime.now(timezone.utc).isoformat(),
            }
        ]
        prior = self.write_prior(prior_data)
        merged, stderr = self.quiet_collect(
            pr_globs=[FAKE_GLOB], merge_with=prior, gsutil=gsutil
        )
        self.assertEqual(
            [run["build_id"] for run in merged["runs"]],
            [BUILD_998_INFRA, BUILD_998_FULL],
        )
        self.assertNotIn("pending_builds", merged)  # recorded -> off the list
        self.assertIn("retrying 1 pending", stderr)
        calls = log.read_text()
        self.assertIn(BUILD_998_INFRA + "/finished.json", calls)
        # A build below the watermark and NOT pending still costs zero reads.
        self.assertNotIn(BUILD_956_TRUNCATED + "/finished.json", calls)
        self.assertNotIn(BUILD_956_TRUNCATED + "/started.json", calls)

    def test_unfinished_build_lands_on_pending_and_keeps_first_seen(self):
        gsutil, _ = self.fake_gsutil([BUILD_998_FULL])
        bucket_build = (
            pathlib.Path(os.environ["FAKE_GSUTIL_ROOT"])
            / FAKE_GLOB[len("gs://fake-prow/"):].rstrip("*")
            / BUILD_998_FULL
        )
        (bucket_build / "finished.json").unlink()  # still in flight
        first_sweep = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        first, _ = self.quiet_collect(
            pr_globs=[FAKE_GLOB], gsutil=gsutil, now=first_sweep
        )
        self.assertEqual(first["runs"], [])
        self.assertEqual(
            first["pending_builds"],
            [{"build_id": BUILD_998_FULL, "first_seen": first_sweep.isoformat()}],
        )
        # An hour later it is STILL unfinished: the entry is carried with its
        # original first_seen, so the retry clock runs from the first sighting.
        prior = self.write_prior(first)
        second, _ = self.quiet_collect(
            pr_globs=[FAKE_GLOB],
            merge_with=prior,
            gsutil=gsutil,
            now=first_sweep + timedelta(hours=1),
        )
        self.assertEqual(
            second["pending_builds"],
            [{"build_id": BUILD_998_FULL, "first_seen": first_sweep.isoformat()}],
        )
        # Another hour on, finished.json has landed: recorded, list emptied.
        shutil.copy(
            TESTDATA / BUILD_998_FULL / "finished.json",
            bucket_build / "finished.json",
        )
        prior = self.write_prior(second)
        third, _ = self.quiet_collect(
            pr_globs=[FAKE_GLOB],
            merge_with=prior,
            gsutil=gsutil,
            now=first_sweep + timedelta(hours=2),
        )
        self.assertEqual(
            [run["build_id"] for run in third["runs"]], [BUILD_998_FULL]
        )
        self.assertNotIn("pending_builds", third)

    def test_expired_pending_entry_is_dropped_without_paying_a_read(self):
        gsutil, log = self.fake_gsutil([BUILD_998_INFRA, BUILD_998_FULL])
        with tempfile.TemporaryDirectory() as sub:
            shutil.copytree(TESTDATA / BUILD_998_FULL, pathlib.Path(sub) / BUILD_998_FULL)
            prior_data = collect.collect(from_dir=pathlib.Path(sub))
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        prior_data["pending_builds"] = [
            {
                "build_id": BUILD_998_INFRA,
                "first_seen": (
                    now - timedelta(days=collect.PENDING_RETRY_DAYS, hours=1)
                ).isoformat(),
            }
        ]
        prior = self.write_prior(prior_data)
        merged, stderr = self.quiet_collect(
            pr_globs=[FAKE_GLOB], merge_with=prior, gsutil=gsutil, now=now
        )
        self.assertEqual(
            [run["build_id"] for run in merged["runs"]], [BUILD_998_FULL]
        )
        self.assertNotIn("pending_builds", merged)
        self.assertIn("giving up", stderr)
        self.assertNotIn(BUILD_998_INFRA + "/", log.read_text())  # zero reads

    def test_malformed_pending_builds_is_ignored_but_runs_are_kept(self):
        prior_data = collect.collect(from_dir=TESTDATA)
        for label, bad in {
            "not a list": {"oops": 1},
            "entry missing first_seen": [{"build_id": "123"}],
            "non-numeric id": [{"build_id": "abc", "first_seen": "2026-09-01"}],
        }.items():
            with self.subTest(label):
                prior = self.write_prior({**prior_data, "pending_builds": bad})
                merged, stderr = self.quiet_collect(
                    from_dir=TESTDATA, merge_with=prior
                )
                self.assertEqual(len(merged["runs"]), 3)
                self.assertIn("pending_builds is malformed", stderr)
                self.assertNotIn("pending_builds", merged)

    def test_gs_prior_url_is_read_through_gsutil(self):
        gsutil, _ = self.fake_gsutil([])
        prior_data = collect.collect(from_dir=TESTDATA)
        local = pathlib.Path(os.environ["FAKE_GSUTIL_ROOT"]) / "dash" / "data.json"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(json.dumps(prior_data))
        runs = collect.load_prior_runs("gs://fake-prow/dash/data.json", gsutil=gsutil)
        self.assertEqual([r["build_id"] for r in runs], [r["build_id"] for r in prior_data["runs"]])


class TestRepoDerivedFacts(unittest.TestCase):
    def test_coverage_matches_domains_yaml(self):
        cov = collect.coverage()
        self.assertEqual(cov["domains_total"], 11)
        # #1045 activated incident-triage, emptying the allowlist.
        self.assertEqual(cov["uncovered"], [])
        self.assertEqual(cov["domains_covered"], cov["domains_total"] - len(cov["uncovered"]))

    def test_active_tasks_are_the_uncommented_entries(self):
        active = collect.active_task_names()
        self.assertIn("reliability-pdb-probe", active)
        self.assertIn("compliance-rbac-overgrant", active)
        self.assertNotIn("obtainability-planted-pdb", active)  # registered, commented out
        self.assertNotIn("stockout-pinned-pool", active)


class TestContractShape(unittest.TestCase):
    def test_top_level_contract(self):
        data = collect.collect(from_dir=TESTDATA)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["source"], "logs")
        self.assertEqual(
            list(data),
            ["schema_version", "generated_at", "source", "runs", "cases", "coverage"],
        )
        # Round-trips as JSON (datetimes serialized, no exotic types).
        reparsed = json.loads(json.dumps(data))
        self.assertEqual(reparsed["coverage"], data["coverage"])

    def test_run_and_case_field_names(self):
        data = collect.collect(from_dir=TESTDATA)
        self.assertEqual(
            list(data["runs"][0]),
            ["build_id", "pr", "head_sha", "project", "started", "finished", "result", "duration_s", "tasks"],
        )
        self.assertEqual(
            list(data["runs"][0]["tasks"][0]),
            ["name", "result", "duration_s", "outcome_validity"],
        )
        self.assertEqual(
            list(data["cases"][0]),
            ["name", "domain", "active", "runs_on_record", "pass_rate", "last3", "durations", "ov_history"],
        )


if __name__ == "__main__":
    unittest.main()
