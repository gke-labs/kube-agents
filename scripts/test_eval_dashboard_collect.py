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
import re
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from eval_dashboard import collect

TESTDATA = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "testdata"

# Real multi-repetition builds (see SCHEMA.md's fixtures table): the grading
# blocks the single-rep-era fixtures above predate.
REPS_TESTDATA = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "testdata_reps"
BUILD_1057_PARALLEL = "2094432646640701440"  # PR 1057, parallel fan-out, green
BUILD_1075_SERIAL = "2094467976156680192"  # PR 1075, serial reps, aborted mid-run
BUILD_1089_MIXED = "2094714569262895104"  # PR 1089, blocked/infra-heavy reps

# A real post-kube-agents-eval-rc build -- a postsubmit, so no `pull` key in
# started.json and no PR number anywhere in the log.
RC_TESTDATA = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "testdata_rc"
BUILD_RC_GREEN = "2097891568546484224"  # staging_2609092307_5b5ad10, GREEN, no baseline

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
# FAKE_GSUTIL_SLEEP (optional JSON {argv substring: seconds}) delays a
# matching call, which is how a test stages a hung listing or reads that
# finish out of order.
_FAKE_GSUTIL = r"""#!/usr/bin/env python3
import json, os, pathlib, sys, time

root = pathlib.Path(os.environ["FAKE_GSUTIL_ROOT"])
argv = " ".join(sys.argv[1:])
with open(os.environ["FAKE_GSUTIL_LOG"], "a") as fh:
    fh.write(argv + "\n")
for needle, seconds in json.loads(os.environ.get("FAKE_GSUTIL_SLEEP", "{}")).items():
    if needle in argv:
        time.sleep(seconds)
BUCKET = "gs://fake-prow/"

def local(url):
    return root / url[len(BUCKET):]

if sys.argv[1] == "ls":
    base = local(sys.argv[2].rstrip("*"))
    if not base.is_dir():
        sys.exit(1)
    for p in sorted(base.iterdir()):
        rel = BUCKET + p.relative_to(root).as_posix()
        print(rel + "/" if p.is_dir() else rel)
    sys.exit(0)
if sys.argv[1] == "cat":
    try:
        sys.stdout.write(local(sys.argv[2]).read_text())
    except OSError:
        sys.exit(1)
    sys.exit(0)
sys.exit(2)
"""

FAKE_BUCKET = "gs://fake-prow/"
FAKE_GLOB = FAKE_BUCKET + "pull/gke-labs_kube-agents/998/pull-kube-agents-smoke-test/*"
# Prow's per-job directory index for the fake bucket, laid out like the real
# one: <prefix>/<build_id>.txt holding the build directory's gs:// path.
FAKE_INDEX_PREFIX = FAKE_BUCKET + "pr-logs/directory/pull-kube-agents-smoke-test/"

# The RC job is a postsubmit, so its builds land under logs/ rather than
# pull-logs/. Same fake gsutil, different prefix.
RC_FAKE_GLOB = "gs://fake-prow/logs/post-kube-agents-eval-rc/*"


class _MergeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write_prior(self, data, name="prior.json") -> str:
        path = self.tmp / name
        path.write_text(json.dumps(data))
        return str(path)

    def fake_gsutil(self, builds, pr=998) -> tuple[str, pathlib.Path]:
        """A gsutil serving `builds` (fixture ids) plus the call log's path.

        Each build is placed under PR `pr`'s directory and gets an index
        pointer, so a test can discover it either by glob or by index; the
        pointer holds the directory path without a trailing slash, as Prow
        writes it. The index also carries a latest-build.txt.
        """
        root = self.tmp / "bucket"
        for build in builds:
            self.place_build(root, build, pr)
        index = root / FAKE_INDEX_PREFIX[len(FAKE_BUCKET):]
        index.mkdir(parents=True, exist_ok=True)
        (index / "latest-build.txt").write_text(max(builds, key=int) + "\n" if builds else "")
        return self._install_fake_gsutil(root)

    def fake_rc_gsutil(self, build_ids) -> tuple[str, pathlib.Path]:
        """The same, under the RC job's prefix, serving copies of the one real
        RC fixture under whatever build ids the test needs."""
        root = self.tmp / "bucket"
        prefix = root / RC_FAKE_GLOB[len(FAKE_BUCKET):].rstrip("*")
        prefix.mkdir(parents=True, exist_ok=True)
        for build_id in build_ids:
            shutil.copytree(RC_TESTDATA / BUILD_RC_GREEN, prefix / build_id)
        return self._install_fake_gsutil(root)

    def _install_fake_gsutil(self, root) -> tuple[str, pathlib.Path]:
        """Write the fake gsutil over `root`; (gsutil path, call-log path)."""
        gsutil = self.tmp / "fake-gsutil"
        gsutil.write_text(_FAKE_GSUTIL)
        gsutil.chmod(gsutil.stat().st_mode | stat.S_IXUSR)
        log = self.tmp / "gsutil-calls.log"
        log.write_text("")
        os.environ["FAKE_GSUTIL_ROOT"] = str(root)
        os.environ["FAKE_GSUTIL_LOG"] = str(log)
        self.addCleanup(os.environ.pop, "FAKE_GSUTIL_ROOT", None)
        self.addCleanup(os.environ.pop, "FAKE_GSUTIL_LOG", None)
        self.addCleanup(os.environ.pop, "FAKE_GSUTIL_SLEEP", None)
        return str(gsutil), log

    @staticmethod
    def build_url(build, pr=998) -> str:
        """The build directory's gs:// URL under PR `pr`, no trailing slash."""
        return f"{FAKE_BUCKET}pull/gke-labs_kube-agents/{pr}/pull-kube-agents-smoke-test/{build}"

    def place_build(self, root, build, pr=998) -> pathlib.Path:
        """Copy fixture `build` under PR `pr` and write its index pointer."""
        url = self.build_url(build, pr)
        dst = root / url[len(FAKE_BUCKET):]
        shutil.copytree(TESTDATA / build, dst)
        index = root / FAKE_INDEX_PREFIX[len(FAKE_BUCKET):]
        index.mkdir(parents=True, exist_ok=True)
        (index / f"{build}.txt").write_text(url + "\n")
        return dst

    @staticmethod
    def bucket_root() -> pathlib.Path:
        return pathlib.Path(os.environ["FAKE_GSUTIL_ROOT"])

    def prior_with(self, builds) -> str:
        """A prior data.json holding exactly the fixture `builds`."""
        with tempfile.TemporaryDirectory() as sub:
            for build in builds:
                shutil.copytree(TESTDATA / build, pathlib.Path(sub) / build)
            return self.write_prior(collect.collect(from_dir=pathlib.Path(sub)))

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


# The refresh workflow's publish gate, read from .github/workflows/
# ci-health.yml rather than copied, so the tests below pin that a failed
# index listing or pointer read trips the grep the workflow actually runs.
# The workflow pipes collect.log through a `grep -v` that drops the
# release-candidate lane's lines before this pattern sees it; index warnings
# name the presubmit job, so that filter never hides them.
_WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci-health.yml"
_REFUSAL_GREP = re.search(
    r'work/collect\.log \\\n\s*\| grep -Eq "([^"]+)"', _WORKFLOW.read_text()
)
assert _REFUSAL_GREP, f"{_WORKFLOW} no longer greps collect.log; update this test's gate"
WORKFLOW_REFUSAL = re.compile(_REFUSAL_GREP.group(1))


class TestIndexDiscovery(_MergeBase):
    """Incremental discovery through Prow's per-job directory index.

    With a watermark the collector must list the index prefix instead of the
    whole-archive glob (which no longer fits the per-call timeout), read only
    the pointers above the watermark, and follow each pointer to wherever
    the build lives. The fallbacks and the refusal semantics are pinned too:
    no watermark means the glob, a failed listing means the workflow's
    refusal line and nothing new.
    """

    ALL = (BUILD_956_TRUNCATED, BUILD_998_INFRA, BUILD_998_FULL)

    def test_index_is_listed_instead_of_the_glob_and_only_new_pointers_are_read(self):
        gsutil, log = self.fake_gsutil(self.ALL)
        prior = self.prior_with([BUILD_956_TRUNCATED, BUILD_998_INFRA])
        merged, stderr = self.quiet_collect(
            pr_globs=[FAKE_GLOB], merge_with=prior, gsutil=gsutil, index_prefix=FAKE_INDEX_PREFIX
        )
        self.assertEqual([run["build_id"] for run in merged["runs"]], list(self.ALL))
        calls = log.read_text().splitlines()
        self.assertEqual([c for c in calls if c.startswith("ls ")], [f"ls {FAKE_INDEX_PREFIX}"])
        pointers = [c for c in calls if c.startswith(f"cat {FAKE_INDEX_PREFIX}")]
        self.assertEqual(pointers, [f"cat {FAKE_INDEX_PREFIX}{BUILD_998_FULL}.txt"])
        self.assertNotIn("latest-build.txt", "\n".join(calls))
        # The one new build was read through the path its pointer named.
        self.assertIn(f"cat {self.build_url(BUILD_998_FULL)}/finished.json", calls)
        self.assertNotIn(BUILD_998_INFRA + "/finished.json", "\n".join(calls))
        self.assertIn("merged 2 prior runs with 1 newly collected", stderr)
        self.assertIn("via the directory index", stderr)
        self.assertIsNone(WORKFLOW_REFUSAL.search(stderr))

    def test_index_build_ids_skip_latest_build_and_junk(self):
        listing = (
            f"{FAKE_INDEX_PREFIX}latest-build.txt\n"
            f"{FAKE_INDEX_PREFIX}200.txt\n"
            f"{FAKE_INDEX_PREFIX}100.txt\n"
            f"{FAKE_INDEX_PREFIX}notes.md\n"
            f"{FAKE_INDEX_PREFIX}subdir/\n"
            "\n"
        )
        self.assertEqual(collect._index_build_ids(listing), ["200", "100"])

    def test_pointer_to_a_build_under_another_pr_is_followed(self):
        """The glob names one PR; the index names every build the job ran.
        A pointer into a different PR's directory is read where it points,
        and that path is the PR hint when started.json carries none."""
        gsutil, log = self.fake_gsutil([BUILD_998_INFRA])
        other = self.place_build(self.bucket_root(), BUILD_998_FULL, pr=1234)
        started = json.loads((other / "started.json").read_text())
        del started["pull"]
        (other / "started.json").write_text(json.dumps(started))
        prior = self.prior_with([BUILD_998_INFRA])
        merged, _ = self.quiet_collect(
            pr_globs=[FAKE_GLOB], merge_with=prior, gsutil=gsutil, index_prefix=FAKE_INDEX_PREFIX
        )
        by_id = {run["build_id"]: run for run in merged["runs"]}
        self.assertEqual(by_id[BUILD_998_FULL]["pr"], 1234)
        self.assertEqual(len(by_id[BUILD_998_FULL]["tasks"]), 14)
        self.assertIn(f"cat {self.build_url(BUILD_998_FULL, 1234)}/build-log.txt", log.read_text())

    def test_pending_build_below_the_watermark_is_reread_through_the_index(self):
        gsutil, log = self.fake_gsutil(self.ALL)
        prior_data = json.loads(pathlib.Path(self.prior_with([BUILD_998_FULL])).read_text())
        prior_data["pending_builds"] = [
            {"build_id": BUILD_998_INFRA, "first_seen": datetime.now(timezone.utc).isoformat()}
        ]
        prior = self.write_prior(prior_data)
        merged, _ = self.quiet_collect(
            pr_globs=[FAKE_GLOB], merge_with=prior, gsutil=gsutil, index_prefix=FAKE_INDEX_PREFIX
        )
        self.assertEqual(
            [run["build_id"] for run in merged["runs"]], [BUILD_998_INFRA, BUILD_998_FULL]
        )
        self.assertNotIn("pending_builds", merged)
        calls = log.read_text()
        self.assertIn(f"{FAKE_INDEX_PREFIX}{BUILD_998_INFRA}.txt", calls)
        self.assertNotIn(f"{FAKE_INDEX_PREFIX}{BUILD_956_TRUNCATED}.txt", calls)  # zero reads

    def test_unfinished_build_found_through_the_index_lands_on_pending(self):
        gsutil, _ = self.fake_gsutil(self.ALL)
        (self.bucket_root() / self.build_url(BUILD_998_FULL)[len(FAKE_BUCKET):] / "finished.json").unlink()
        prior = self.prior_with([BUILD_956_TRUNCATED, BUILD_998_INFRA])
        now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        merged, stderr = self.quiet_collect(
            pr_globs=[FAKE_GLOB], merge_with=prior, gsutil=gsutil,
            index_prefix=FAKE_INDEX_PREFIX, now=now,
        )
        self.assertEqual(len(merged["runs"]), 2)
        self.assertEqual(
            merged["pending_builds"], [{"build_id": BUILD_998_FULL, "first_seen": now.isoformat()}]
        )
        # An in-flight build is the ordinary case, not a stall: publishable.
        self.assertIsNone(WORKFLOW_REFUSAL.search(stderr))

    def test_listing_timeout_or_failure_trips_the_refusal_line_and_adds_nothing(self):
        gsutil, log = self.fake_gsutil(self.ALL)
        prior = self.prior_with([BUILD_956_TRUNCATED, BUILD_998_INFRA])
        stages = {
            "timed out": {"FAKE_GSUTIL_SLEEP": json.dumps({f"ls {FAKE_INDEX_PREFIX}": 3})},
            "failed": {"FAKE_GSUTIL_ROOT": str(self.tmp / "no-such-bucket")},
        }
        original_timeout = collect.GSUTIL_TIMEOUT_S
        self.addCleanup(setattr, collect, "GSUTIL_TIMEOUT_S", original_timeout)
        for label, env in stages.items():
            with self.subTest(label):
                collect.GSUTIL_TIMEOUT_S = 1
                saved = {k: os.environ.get(k) for k in env}
                os.environ.update(env)
                try:
                    log.write_text("")
                    merged, stderr = self.quiet_collect(
                        pr_globs=[FAKE_GLOB], merge_with=prior, gsutil=gsutil,
                        index_prefix=FAKE_INDEX_PREFIX,
                    )
                finally:
                    for k, v in saved.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                # Nothing new, the prior carried over, and the exact line the
                # workflow greps for -- so this document is never published.
                self.assertEqual(len(merged["runs"]), 2)
                self.assertRegex(stderr, WORKFLOW_REFUSAL)
                self.assertIn("merged 2 prior runs with 0 newly collected", stderr)
                self.assertNotIn("cat ", log.read_text())  # no reads without a listing
                self.assertNotIn(f"ls {FAKE_GLOB}", log.read_text())  # and no glob fallback

    def test_unreadable_pointer_trips_the_refusal_line_and_defers_the_build(self):
        """One pointer read hangs past the per-call timeout (a mode-bit trick
        would not survive running as root); the other builds still read."""
        gsutil, _ = self.fake_gsutil(self.ALL)
        os.environ["FAKE_GSUTIL_SLEEP"] = json.dumps(
            {f"cat {FAKE_INDEX_PREFIX}{BUILD_998_FULL}.txt": 3}
        )
        self.addCleanup(setattr, collect, "GSUTIL_TIMEOUT_S", collect.GSUTIL_TIMEOUT_S)
        collect.GSUTIL_TIMEOUT_S = 1
        prior = self.prior_with([BUILD_956_TRUNCATED, BUILD_998_INFRA])
        now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        merged, stderr = self.quiet_collect(
            pr_globs=[FAKE_GLOB], merge_with=prior, gsutil=gsutil,
            index_prefix=FAKE_INDEX_PREFIX, now=now,
        )
        self.assertEqual(len(merged["runs"]), 2)
        self.assertRegex(stderr, WORKFLOW_REFUSAL)
        self.assertEqual(
            merged["pending_builds"], [{"build_id": BUILD_998_FULL, "first_seen": now.isoformat()}]
        )

    def test_pointer_without_a_gs_path_is_skipped_with_a_plain_warning(self):
        gsutil, _ = self.fake_gsutil(self.ALL)
        pointer = self.bucket_root() / FAKE_INDEX_PREFIX[len(FAKE_BUCKET):] / f"{BUILD_998_FULL}.txt"
        pointer.write_text("http://not-a-bucket/somewhere\n")
        prior = self.prior_with([BUILD_956_TRUNCATED, BUILD_998_INFRA])
        merged, stderr = self.quiet_collect(
            pr_globs=[FAKE_GLOB], merge_with=prior, gsutil=gsutil, index_prefix=FAKE_INDEX_PREFIX
        )
        self.assertEqual(len(merged["runs"]), 2)
        self.assertNotIn("pending_builds", merged)
        self.assertIn("does not hold a gs:// path", stderr)
        self.assertIsNone(WORKFLOW_REFUSAL.search(stderr))  # not a stall: no refusal

    def test_without_a_watermark_the_glob_is_listed_not_the_index(self):
        """The cold sweep: no prior (or a prior with no numeric watermark)
        still discovers through --pr-glob, index or no index."""
        gsutil, log = self.fake_gsutil([BUILD_998_FULL])
        # A prior with no watermark bounds the sweep to DEGRADED_SINCE_DAYS,
        # so `now` is pinned near the fixture: on the wall clock the build
        # (started 2026-08-27) aged out of that bound on 2026-09-10.
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        for label, kwargs in {
            "no prior": {},
            "prior without a watermark": {
                "merge_with": self.write_prior({"schema_version": 1, "runs": []})
            },
        }.items():
            with self.subTest(label):
                log.write_text("")
                # Pinned: the fixture build finished 2026-08-27 and the
                # default --since-days window would age it out.
                merged, _ = self.quiet_collect(
                    pr_globs=[FAKE_GLOB], gsutil=gsutil, index_prefix=FAKE_INDEX_PREFIX, now=now, **kwargs
                )
                self.assertEqual([run["build_id"] for run in merged["runs"]], [BUILD_998_FULL])
                listings = [c for c in log.read_text().splitlines() if c.startswith("ls ")]
                self.assertEqual(listings, [f"ls {FAKE_GLOB}"])

    def test_merge_with_alone_still_touches_no_bucket(self):
        """--merge-with without --pr-glob recomputes; the index is how a
        glob scan discovers builds, not a scan of its own."""
        gsutil, log = self.fake_gsutil(self.ALL)
        prior = self.prior_with([BUILD_956_TRUNCATED])
        merged, _ = self.quiet_collect(merge_with=prior, gsutil=gsutil, index_prefix=FAKE_INDEX_PREFIX)
        self.assertEqual(len(merged["runs"]), 1)
        self.assertEqual(log.read_text(), "")

    def test_reads_come_back_in_build_id_order_whatever_finishes_first(self):
        """Eight concurrent readers; the oldest build is made the slowest so
        completion order is the reverse of id order, and runs[] must still
        come out ascending -- two collects over one archive write the same
        document."""
        gsutil, _ = self.fake_gsutil(self.ALL)
        os.environ["FAKE_GSUTIL_SLEEP"] = json.dumps(
            {BUILD_956_TRUNCATED + "/": 0.4, BUILD_998_INFRA + "/": 0.2}
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            runs = collect.runs_from_index(FAKE_INDEX_PREFIX, gsutil=gsutil)
        self.assertEqual([run["build_id"] for run in runs], list(self.ALL))

    def test_cli_flag_reaches_the_scan_and_an_empty_value_disables_it(self):
        gsutil, log = self.fake_gsutil(self.ALL)
        prior = self.prior_with([BUILD_956_TRUNCATED, BUILD_998_INFRA])
        out = self.tmp / "out.json"
        common = ["--pr-glob", FAKE_GLOB, "--merge-with", prior, "--gsutil", gsutil, "--gh", "", "--out", str(out)]
        for label, extra, expected_listing in (
            ("index", ["--index-prefix", FAKE_INDEX_PREFIX], f"ls {FAKE_INDEX_PREFIX}"),
            ("disabled", ["--index-prefix", ""], f"ls {FAKE_GLOB}"),
        ):
            with self.subTest(label):
                log.write_text("")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(collect.main([*common, *extra]), 0)
                self.assertEqual(len(json.loads(out.read_text())["runs"]), 3)
                listings = [c for c in log.read_text().splitlines() if c.startswith("ls ")]
                self.assertEqual(listings, [expected_listing])

    def test_default_index_is_derived_from_the_globs_bucket_and_job(self):
        """The refresh workflow's PR_GLOB (ci-health.yml) resolves to the
        smoke-test job's index without any flag; an explicit prefix wins; an
        empty string, or a glob of another shape, means no index."""
        workflow_glob = (
            "gs://kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/*/pull-kube-agents-smoke-test/*"
        )
        self.assertEqual(
            collect.discovery_index(workflow_glob, None),
            "gs://kube-agents-prow/pr-logs/directory/pull-kube-agents-smoke-test/",
        )
        self.assertEqual(
            collect.discovery_index("gs://other/pr-logs/pull/o_r/1234/some-job/*", None),
            "gs://other/pr-logs/directory/some-job/",
        )
        self.assertEqual(collect.discovery_index(workflow_glob, "gs://x/idx"), "gs://x/idx/")
        self.assertIsNone(collect.discovery_index(workflow_glob, ""))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(collect.discovery_index(FAKE_GLOB, None))  # no pr-logs/, no job


# The nightly periodic's archive on the fake bucket, laid out as Prow lays out
# a periodic: gs://<bucket>/logs/<job>/<build_id>/ plus latest-build.txt, no
# pointer objects. The job name is deliberately not the live default, so a
# test can tell a derived job from a hardcoded one.
FAKE_NIGHTLY_JOB = "ci-fake-eval-nightly"
FAKE_NIGHTLY_PREFIX = f"{FAKE_BUCKET}logs/{FAKE_NIGHTLY_JOB}/"


class TestNightlySource(_MergeBase):
    """The nightly periodic beside the presubmit: same parser, its own tier,
    no pull request, its own watermark, and never the refusal line."""

    def place_nightly_build(self, build) -> pathlib.Path:
        """Copy fixture `build` under the periodic's prefix (one directory per
        build, as Prow archives a periodic) and refresh latest-build.txt."""
        root = self.bucket_root()
        prefix = root / FAKE_NIGHTLY_PREFIX[len(FAKE_BUCKET):]
        prefix.mkdir(parents=True, exist_ok=True)
        dst = prefix / build
        shutil.copytree(TESTDATA / build, dst)
        (prefix / "latest-build.txt").write_text(max(p.name for p in prefix.iterdir() if p.is_dir()) + "\n")
        return dst

    def test_nightly_builds_parse_with_tier_job_and_no_pr(self):
        gsutil, log = self.fake_gsutil([])
        self.place_nightly_build(BUILD_998_FULL)
        data, stderr = self.quiet_collect(nightly_prefix=FAKE_NIGHTLY_PREFIX, gsutil=gsutil)
        (run,) = data["runs"]
        self.assertEqual(run["build_id"], BUILD_998_FULL)
        self.assertEqual(run["tier"], "nightly")
        self.assertEqual(run["job"], FAKE_NIGHTLY_JOB, "derived from the prefix, not hardcoded")
        # The real fixture's started.json says pull 998; a periodic runs main
        # and its run is nobody's pull request whatever the metadata says.
        self.assertIsNone(run["pr"])
        # The same parser as the presubmit: every task, the verdict duration.
        self.assertEqual(len(run["tasks"]), 14)
        self.assertEqual(run["duration_s"], 5793)
        self.assertEqual(run["head_sha"], "a28f0b3")
        calls = log.read_text().splitlines()
        self.assertEqual([c for c in calls if c.startswith("ls ")], [f"ls {FAKE_NIGHTLY_PREFIX}"])
        self.assertNotIn("latest-build.txt", "\n".join(calls))
        self.assertIn(f"nightly prefix {FAKE_NIGHTLY_PREFIX}: 1 build(s) to read", stderr)
        self.assertIsNone(WORKFLOW_REFUSAL.search(stderr))

    def test_nightly_job_flag_overrides_the_derived_name(self):
        gsutil, _ = self.fake_gsutil([])
        self.place_nightly_build(BUILD_998_FULL)
        data, _ = self.quiet_collect(nightly_prefix=FAKE_NIGHTLY_PREFIX, nightly_job="renamed-job", gsutil=gsutil)
        self.assertEqual(data["runs"][0]["job"], "renamed-job")

    def test_presubmit_runs_carry_their_job_from_the_build_path(self):
        gsutil, _ = self.fake_gsutil([BUILD_998_FULL])
        data, _ = self.quiet_collect(pr_globs=[FAKE_GLOB], gsutil=gsutil)
        (run,) = data["runs"]
        self.assertEqual((run["tier"], run["job"], run["pr"]), ("presubmit", "pull-kube-agents-smoke-test", 998))

    def test_each_source_resumes_above_its_own_watermark(self):
        """Prow build ids are one global sequence, so the newest presubmit
        id is usually ABOVE every nightly id on record and vice versa. A
        shared watermark would skip whichever source is behind."""
        gsutil, log = self.fake_gsutil([BUILD_998_INFRA])  # presubmit: the middle id
        self.place_nightly_build(BUILD_956_TRUNCATED)  # nightly: the lowest id
        # Prior: the presubmit's HIGHEST id recorded (so the presubmit
        # candidate is genuinely old and must be skipped), and one old
        # nightly run with a tiny id. Under a shared watermark -- the
        # newest id on record -- the nightly candidate would be skipped
        # forever; above the nightly's own watermark it is new.
        prior_data = json.loads(pathlib.Path(self.prior_with([BUILD_998_FULL])).read_text())
        prior_data["runs"].append(dict(prior_data["runs"][0], build_id="1", tier="nightly", pr=None))
        prior = self.write_prior(prior_data)
        merged, stderr = self.quiet_collect(
            pr_globs=[FAKE_GLOB], nightly_prefix=FAKE_NIGHTLY_PREFIX, merge_with=prior, gsutil=gsutil,
            index_prefix=FAKE_INDEX_PREFIX,
        )
        by_id = {run["build_id"]: run for run in merged["runs"]}
        self.assertEqual(by_id[BUILD_956_TRUNCATED]["tier"], "nightly", "below the presubmit watermark, above the nightly's own")
        self.assertNotIn(BUILD_998_INFRA, by_id, "below the presubmit's own watermark and not pending: skipped, zero reads")
        self.assertNotIn(BUILD_998_INFRA + "/", log.read_text())
        self.assertEqual(sorted(by_id), sorted([BUILD_956_TRUNCATED, BUILD_998_FULL, "1"]))
        self.assertIn(f"GCS scan resumed above build {BUILD_998_FULL}", stderr)
        self.assertIn("nightly scan resumed above build 1, 1 new", stderr)

    def test_the_presubmit_watermark_ignores_nightly_ids(self):
        gsutil, log = self.fake_gsutil([BUILD_998_INFRA, BUILD_998_FULL])
        # Prior: presubmit INFRA on record, plus a nightly run whose id is
        # above FULL. FULL must still be read through the presubmit index.
        prior_data = json.loads(pathlib.Path(self.prior_with([BUILD_998_INFRA])).read_text())
        prior_data["runs"].append(dict(prior_data["runs"][0], build_id=str(int(BUILD_998_FULL) + 1), tier="nightly", pr=None))
        prior = self.write_prior(prior_data)
        merged, stderr = self.quiet_collect(
            pr_globs=[FAKE_GLOB], merge_with=prior, gsutil=gsutil, index_prefix=FAKE_INDEX_PREFIX
        )
        self.assertIn(BUILD_998_FULL, {run["build_id"] for run in merged["runs"]})
        self.assertIn(f"GCS scan resumed above build {BUILD_998_INFRA}", stderr)
        self.assertNotIn(f"cat {FAKE_INDEX_PREFIX}{BUILD_998_INFRA}.txt", log.read_text())

    def test_a_prefix_that_does_not_list_is_a_note_never_the_refusal_line(self):
        """Until oss-test-infra's periodic runs, the prefix does not exist;
        the gate's dashboard must keep publishing meanwhile."""
        gsutil, _ = self.fake_gsutil([BUILD_998_FULL])
        prior = self.prior_with([BUILD_998_INFRA])
        merged, stderr = self.quiet_collect(
            pr_globs=[FAKE_GLOB], nightly_prefix=FAKE_NIGHTLY_PREFIX, merge_with=prior, gsutil=gsutil,
            index_prefix=FAKE_INDEX_PREFIX,
        )
        self.assertEqual([run["build_id"] for run in merged["runs"]], [BUILD_998_INFRA, BUILD_998_FULL])
        self.assertIn(f"note: nightly prefix {FAKE_NIGHTLY_PREFIX} did not list", stderr)
        self.assertIsNone(WORKFLOW_REFUSAL.search(stderr))
        self.assertIn("nightly scan resumed above build None, 0 new", stderr)

    def test_a_known_prefix_that_stops_listing_is_the_refusal_line(self):
        """Once a night is on record, a prefix that does not list is a stall,
        not an absent job: republishing would freeze the nightly record."""
        gsutil, _ = self.fake_gsutil([BUILD_998_FULL])
        prior_data = json.loads(pathlib.Path(self.prior_with([BUILD_998_INFRA])).read_text())
        prior_data["runs"].append(dict(prior_data["runs"][0], build_id="1", tier="nightly", pr=None))
        merged, stderr = self.quiet_collect(
            pr_globs=[FAKE_GLOB], nightly_prefix=FAKE_NIGHTLY_PREFIX, merge_with=self.write_prior(prior_data),
            gsutil=gsutil, index_prefix=FAKE_INDEX_PREFIX,
        )
        self.assertEqual(len(merged["runs"]), 3, "the presubmit side still collected")
        self.assertRegex(stderr, WORKFLOW_REFUSAL)
        self.assertIn(f"warning: gsutil ls failed for {FAKE_NIGHTLY_PREFIX}", stderr)

    def test_an_unfinished_nightly_build_rides_pending_like_any_other(self):
        gsutil, _ = self.fake_gsutil([])
        built = self.place_nightly_build(BUILD_998_FULL)
        (built / "finished.json").unlink()
        now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        data, stderr = self.quiet_collect(nightly_prefix=FAKE_NIGHTLY_PREFIX, gsutil=gsutil, now=now)
        self.assertEqual(data["runs"], [])
        self.assertEqual(data["pending_builds"], [{"build_id": BUILD_998_FULL, "first_seen": now.isoformat()}])
        self.assertIsNone(WORKFLOW_REFUSAL.search(stderr))

    def test_cases_keep_the_two_records_apart(self):
        """The per-case fields are the presubmit's; the nightly's sit under
        `nightly`. A case only the nightly ran is still on record."""
        gsutil, _ = self.fake_gsutil([BUILD_998_INFRA])
        self.place_nightly_build(BUILD_998_FULL)
        data, _ = self.quiet_collect(pr_globs=[FAKE_GLOB], nightly_prefix=FAKE_NIGHTLY_PREFIX, gsutil=gsutil)
        cases = _cases_by_name(data)
        # compliance-rbac-overgrant: infra on the presubmit build, fail on the nightly one.
        case = cases["compliance-rbac-overgrant"]
        self.assertEqual((case["runs_on_record"], case["pass_rate"], case["last3"]), (1, None, ["infra"]))
        self.assertEqual(case["nightly"], {"runs_on_record": 1, "pass_rate": 0.0, "last3": ["fail"]})
        # A task only the FULL build ran (as the nightly here): empty presubmit side.
        only_nightly = [name for name, c in cases.items() if c["runs_on_record"] == 0]
        self.assertTrue(only_nightly)
        for name in only_nightly:
            self.assertEqual(cases[name]["nightly"]["runs_on_record"], 1, name)
            self.assertIsNone(cases[name]["pass_rate"])
            self.assertEqual(cases[name]["last3"], [])
        # And the pooled numbers a consumer used to read stay the presubmit's.
        self.assertEqual(sum(c["runs_on_record"] for c in cases.values()), 11)

    def test_nightly_active_is_the_superset_matrix(self):
        data = collect.collect(from_dir=TESTDATA)
        cases = _cases_by_name(data)
        self.assertTrue(cases["reliability-pdb-probe"]["active"])
        self.assertTrue(cases["reliability-pdb-probe"]["nightly_active"], "TASKS is in the nightly too")
        self.assertFalse(cases["obtainability-planted-pdb"]["active"])
        self.assertTrue(cases["obtainability-planted-pdb"]["nightly_active"], "an uncommented NIGHTLY_TASKS entry")
        self.assertFalse(cases["compliance-rbac-overgrant"]["nightly_active"] and not cases["compliance-rbac-overgrant"]["active"])

    def test_head_sha_falls_back_to_the_started_commit_for_a_periodic(self):
        """Prow writes `revision: main` in a periodic's finished.json and the
        commit in started.json's repo-commit."""
        files = {
            "finished.json": json.dumps({"timestamp": 1789072197, "result": "SUCCESS", "revision": "main"}),
            "started.json": json.dumps({"timestamp": 1789071786, "repo-commit": "7a322673af0252b464695ae65b0a044b72520fd4"}),
            "build-log.txt": "",
        }
        run = collect.build_run("1", files.get, tier="nightly", job="j")
        self.assertEqual(run["head_sha"], "7a32267")
        self.assertEqual((run["tier"], run["job"], run["pr"]), ("nightly", "j", None))
        files["finished.json"] = json.dumps({"timestamp": 1, "result": "SUCCESS", "revision": "727f252fb6709e7471bc63e2465bc68ef5aaa81e"})
        self.assertEqual(collect.build_run("1", files.get)["head_sha"], "727f252", "a presubmit's revision still wins")

    def test_cli_flags_bare_and_explicit(self):
        gsutil, log = self.fake_gsutil([])
        self.place_nightly_build(BUILD_998_FULL)
        out = self.tmp / "out.json"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                collect.main(["--nightly-prefix", FAKE_NIGHTLY_PREFIX, "--gsutil", gsutil, "--gh", "", "--out", str(out)]), 0
            )
        data = json.loads(out.read_text())
        self.assertEqual([(r["tier"], r["job"]) for r in data["runs"]], [("nightly", FAKE_NIGHTLY_JOB)])
        # Bare --nightly-prefix means the live periodic's prefix.
        log.write_text("")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(collect.main(["--nightly-prefix", "--gsutil", gsutil, "--gh", "", "--out", str(out)]), 0)
        self.assertEqual([c for c in log.read_text().splitlines() if c.startswith("ls ")], [f"ls {collect.DEFAULT_NIGHTLY_PREFIX}"])
        self.assertEqual(collect.DEFAULT_NIGHTLY_PREFIX, "gs://kube-agents-prow/logs/ci-kube-agents-eval-nightly/")
        # Nothing at all is still an error.
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            collect.main(["--out", str(out)])


class TestRepoDerivedFacts(unittest.TestCase):
    def test_nightly_tasks_are_tasks_plus_the_nightly_only_array(self):
        nightly = collect.nightly_task_names()
        self.assertTrue(collect.active_task_names() <= nightly)
        self.assertIn("obtainability-planted-pdb", nightly)
        self.assertIn("gpu-stress-test-diagnosis", nightly)

    def test_coverage_matches_domains_yaml(self):
        cov = collect.coverage()
        self.assertEqual(cov["domains_total"], 11)
        # incident-triage's presubmit coverage moved to the nightly tier on
        # 2026-09-03 (tofu wall clock; #1202); the allowlist carries it until
        # a non-tofu probe activates or the contract recognizes the tier.
        self.assertEqual(cov["uncovered"], ["incident-triage"])
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
            ["build_id", "tier", "job", "pr", "head_sha", "project", "started", "finished", "result", "duration_s", "tasks"],
        )
        self.assertEqual(
            list(data["runs"][0]["tasks"][0]),
            ["name", "result", "duration_s", "outcome_validity"],
        )
        self.assertEqual(
            list(data["cases"][0]),
            ["name", "domain", "active", "nightly_active", "runs_on_record", "pass_rate", "last3", "durations", "ov_history", "nightly"],
        )
        self.assertEqual(list(data["cases"][0]["nightly"]), ["runs_on_record", "pass_rate", "last3"])

    def test_from_dir_runs_are_the_presubmit_with_no_job(self):
        """The offline source has no URL to read a job from; the tier is the
        presubmit, as every run was before the field existed."""
        for run in collect.collect(from_dir=TESTDATA)["runs"]:
            self.assertEqual(run["tier"], "presubmit")
            self.assertIsNone(run["job"])


class TestReleaseCandidateParsing(unittest.TestCase):
    """releases[] from a REAL post-kube-agents-eval-rc build.

    The fixture is the first RC to go through the release-gate lane end to
    end: staging_2609092307_5b5ad10, four hours, 26 tasks, GREEN with no
    baseline at its version key. It keeps both banners the driver prints --
    resolve-rc-target.sh's "RELEASE CANDIDATE EVAL TARGET" near the top and
    ci-eval-rc.sh's "RELEASE CANDIDATE EVAL" at the bottom -- because a
    substring match opens the parse block on the wrong one.
    """

    def test_real_rc_build_parses(self):
        releases = collect.releases_from_dir(RC_TESTDATA)
        self.assertEqual(len(releases), 1)
        release = releases[0]
        self.assertEqual(release["build_id"], BUILD_RC_GREEN)
        self.assertEqual(release["rc_tag"], "staging_2609092307_5b5ad10")
        self.assertEqual(release["commit"], "5b5ad10")  # abbreviated from the banner's full sha
        self.assertEqual(release["tier"], "nightly")
        self.assertEqual(release["verdict"], "GREEN")
        self.assertEqual(release["result"], "SUCCESS")
        self.assertEqual(release["project"], "kube-agents-evals-10")
        self.assertEqual(release["started"], "2026-09-10T03:35:01+00:00")
        self.assertEqual(release["duration_s"], 15006)  # the verdict line, not the Prow delta
        self.assertEqual(
            release["artifacts_url"],
            "https://oss.gprow.dev/view/gs/kube-agents-prow/logs/"
            "post-kube-agents-eval-rc/" + BUILD_RC_GREEN,
        )
        self.assertEqual(release["pass_rate"], 0.9)
        # "(no baseline at the current version key -- advisory)" carries no
        # numbers, so the comparison stays unset rather than defaulting to 0.
        self.assertIsNone(release["baseline_rate"])
        self.assertIsNone(release["margin"])
        results = [t["result"] for t in release["tasks"]]
        self.assertEqual((results.count("pass"), results.count("fail"), results.count("infra")), (15, 10, 1))

    def test_release_field_names(self):
        release = collect.releases_from_dir(RC_TESTDATA)[0]
        self.assertEqual(
            list(release),
            [
                "build_id", "rc_tag", "commit", "tier", "verdict", "result",
                "started", "finished", "duration_s", "project",
                "artifacts_url", "pass_rate", "baseline_rate", "margin", "tasks",
            ],
        )

    def test_target_banner_alone_is_not_a_verdict_banner(self):
        """A run that died after resolve-rc-target.sh printed its banner but
        before ci-eval-rc.sh printed its own has no banner at all."""
        head = (RC_TESTDATA / BUILD_RC_GREEN / "build-log.txt").read_text().splitlines()
        truncated = "\n".join(head[:20])
        self.assertIn("RELEASE CANDIDATE EVAL TARGET", truncated)
        banner = collect.parse_rc_banner(truncated)
        self.assertFalse(banner["banner"])
        self.assertIsNone(banner["rc_tag"])
        self.assertIsNone(banner["verdict"])

    def test_admitted_rate_with_a_baseline_carries_the_comparison(self):
        banner = collect.parse_rc_banner(
            "Admitted-case pass rate: 88.5% (main: 91.0%, margin -2.5%)\n"
            "🏷️ RELEASE CANDIDATE EVAL\n"
            "Verdict:     RED (advisory: this lane gates nothing)\n"
        )
        self.assertEqual(banner["pass_rate"], 0.885)
        self.assertEqual(banner["baseline_rate"], 0.91)
        self.assertEqual(banner["margin"], -0.025)
        self.assertEqual(banner["verdict"], "RED")

    def test_build_without_a_banner_still_records_the_run(self):
        """A driver that exited on an early guard is a release with no verdict,
        not a missing release -- the dashboard has to show the attempt."""
        with tempfile.TemporaryDirectory() as tmp:
            build = pathlib.Path(tmp) / "999"
            build.mkdir()
            (build / "build-log.txt").write_text("=== boom ===\n")
            (build / "started.json").write_text('{"timestamp": 1789011301}')
            (build / "finished.json").write_text('{"timestamp": 1789011401, "result": "FAILURE"}')
            release = collect.releases_from_dir(pathlib.Path(tmp))[0]
        self.assertEqual(release["result"], "FAILURE")
        self.assertIsNone(release["verdict"])
        self.assertIsNone(release["rc_tag"])
        self.assertIsNone(release["pass_rate"])


class TestReleaseCollection(_MergeBase):
    """releases[] through collect(): the trim, the carry-forward, the skip."""

    def _rc_dir(self, build_ids) -> pathlib.Path:
        """A local RC archive holding copies of the real fixture, renamed."""
        root = self.tmp / "rc"
        root.mkdir(exist_ok=True)
        for build_id in build_ids:
            shutil.copytree(RC_TESTDATA / BUILD_RC_GREEN, root / build_id, dirs_exist_ok=True)
        return root

    def test_releases_are_newest_first_and_absent_when_none(self):
        plain = collect.collect(from_dir=TESTDATA)
        self.assertNotIn("releases", plain)
        data, _ = self.quiet_collect(from_dir=TESTDATA, rc_from_dir=self._rc_dir(["10", "30", "20"]))
        # Same started.json in every copy, so the build id breaks the tie.
        self.assertEqual([r["build_id"] for r in data["releases"]], ["30", "20", "10"])

    def test_rc_limit_trims_the_written_list(self):
        data, _ = self.quiet_collect(
            from_dir=TESTDATA, rc_from_dir=self._rc_dir(["10", "20", "30"]), rc_limit=2
        )
        self.assertEqual([r["build_id"] for r in data["releases"]], ["30", "20"])

    def write_release_prior(self, releases) -> str:
        """A prior data.json load_prior will accept, carrying `releases`."""
        prior = collect.collect(from_dir=TESTDATA)
        prior["releases"] = releases
        return self.write_prior(prior)

    def test_prior_releases_are_carried_forward(self):
        prior = self.write_release_prior(
            [{"build_id": "5", "verdict": "RED", "started": "2020-01-01T00:00:00+00:00"}]
        )
        data, _ = self.quiet_collect(
            from_dir=TESTDATA, rc_from_dir=self._rc_dir(["10"]), merge_with=prior
        )
        self.assertEqual([r["build_id"] for r in data["releases"]], ["10", "5"])

    def test_a_retyped_prior_started_sorts_instead_of_crashing(self):
        """The prior filter validates `build_id` and nothing else, so a
        hand-edited data.json can carry a non-string `started`. Sorting that
        against a real entry's string raised TypeError and lost the whole
        merge; both halves of the key are coerced now."""
        prior = self.write_release_prior(
            [{"build_id": "5", "started": 20260101},
             {"build_id": "6", "started": None},
             {"build_id": "7", "started": {"nested": "nonsense"}}]
        )
        data, _ = self.quiet_collect(
            from_dir=TESTDATA, rc_from_dir=self._rc_dir(["10"]), merge_with=prior
        )
        self.assertEqual(
            sorted(r["build_id"] for r in data["releases"]), ["10", "5", "6", "7"]
        )

    def test_corrupt_prior_releases_are_discarded_not_fatal(self):
        prior = self.write_release_prior([{"no_build_id": True}, "nonsense", 7])
        data, _ = self.quiet_collect(
            from_dir=TESTDATA, rc_from_dir=self._rc_dir(["10"]), merge_with=prior
        )
        self.assertEqual([r["build_id"] for r in data["releases"]], ["10"])

    def test_known_releases_are_never_re_read_from_the_bucket(self):
        """A recorded release is final, so the sweep must not pay for it again."""
        gsutil, log = self.fake_rc_gsutil(["10", "20"])
        prior = self.write_release_prior(
            [{"build_id": "20", "started": "2026-09-10T03:35:01+00:00"}]
        )
        data, _ = self.quiet_collect(rc_globs=[RC_FAKE_GLOB], merge_with=prior, gsutil=gsutil)
        self.assertEqual({r["build_id"] for r in data["releases"]}, {"10", "20"})
        calls = log.read_text()
        self.assertIn("/10/build-log.txt", calls)
        self.assertNotIn("/20/build-log.txt", calls)

    def test_rc_limit_is_applied_before_the_reads(self):
        gsutil, log = self.fake_rc_gsutil(["10", "20", "30"])
        data, _ = self.quiet_collect(rc_globs=[RC_FAKE_GLOB], rc_limit=1, gsutil=gsutil)
        self.assertEqual([r["build_id"] for r in data["releases"]], ["30"])
        calls = log.read_text()
        self.assertIn("/30/build-log.txt", calls)
        self.assertNotIn("/10/build-log.txt", calls)
        self.assertNotIn("/20/build-log.txt", calls)

    def test_a_failed_listing_warns_and_keeps_the_rest_of_the_collect(self):
        gsutil, _ = self.fake_rc_gsutil([])
        data, err = self.quiet_collect(
            from_dir=TESTDATA, rc_globs=["gs://fake-prow/nowhere/*"], gsutil=gsutil
        )
        self.assertNotIn("releases", data)
        self.assertIn("gsutil ls failed", err)
        self.assertEqual(len(data["runs"]), 3)


class TestRepParsing(unittest.TestCase):
    """tasks[].reps from the multi-repetition grading blocks, both formats.

    The fixtures are REAL builds: PR 1075's serial run (`--- <task>
    repetition N/3` launch markers), and PR 1057's / PR 1089's parallel
    fan-out runs (`>>> launching <task> rep N/3` / `<<< finished ...`). The
    verdicts come from the `rep N:` grading lines in every format, so both
    fixtures must parse identically apart from which tasks they reached.
    """

    @classmethod
    def setUpClass(cls):
        cls.runs = {run["build_id"]: run for run in collect.runs_from_dir(REPS_TESTDATA)}

    def tasks(self, build_id):
        return {t["name"]: t for t in self.runs[build_id]["tasks"]}

    def test_parallel_green_reps_all_pass_with_null_reason(self):
        task = self.tasks(BUILD_1057_PARALLEL)["reliability-pdb-probe"]
        self.assertEqual(task["result"], "pass")
        self.assertEqual(
            task["reps"],
            [
                {"n": 1, "result": "pass", "reason": None},
                {"n": 2, "result": "pass", "reason": None},
                {"n": 3, "result": "pass", "reason": None},
            ],
        )

    def test_unstable_task_grades_as_fail_and_keeps_the_rep_split(self):
        """[UNSTABLE] (passed some but not all reps) is not a clean pass."""
        task = self.tasks(BUILD_1057_PARALLEL)["upgrades-lagging-master-probe"]
        self.assertEqual(task["result"], "fail")
        self.assertEqual([r["result"] for r in task["reps"]], ["pass", "fail", "pass"])

    def test_reason_survives_its_own_delimiter_and_drops_the_scores_tail(self):
        """Fail reasons contain ` -- ` themselves; only the first one splits."""
        rep = self.tasks(BUILD_1057_PARALLEL)["upgrades-lagging-master-probe"]["reps"][1]
        self.assertTrue(rep["reason"].startswith("VerificationCorrectness=0.5 (floor 1.0) --"))
        self.assertIn("the-probe-identifies-the-version-lag", rep["reason"])
        self.assertNotIn("OutcomeScore", rep["reason"])  # metrics, not reason

    def test_infra_verdict_rep_with_marker(self):
        task = self.tasks(BUILD_1057_PARALLEL)["compliance-rbac-overgrant"]
        self.assertEqual([r["result"] for r in task["reps"]], ["pass", "infra", "fail"])
        self.assertEqual(
            task["reps"][1]["reason"],
            "the harness exhausted its retries without reaching the agent"
            " (KUBE_AGENTS_INFRA_FAILURE): the record is scored, but there is"
            " no answer in it to grade",
        )

    def test_infra_verdict_rep_without_the_marker(self):
        """The `infra` verdict token alone is enough; not every infra rep
        carries the KUBE_AGENTS_INFRA_FAILURE literal."""
        task = self.tasks(BUILD_1089_MIXED)["autoops-warning-event-triage"]
        rep = task["reps"][0]
        self.assertEqual(rep["result"], "infra")
        self.assertNotIn("KUBE_AGENTS_INFRA_FAILURE", rep["reason"])
        self.assertIn("devops-bench wrote no results.json", rep["reason"])

    def test_blocked_rep_grades_as_fail(self):
        """`blocked` (inadmissible record, e.g. an empty trajectory) is a
        fail: the case did not pass, and the schema vocabulary is closed."""
        rep = self.tasks(BUILD_1089_MIXED)["security-overgrant-probe"]["reps"][2]
        self.assertEqual(rep["result"], "fail")
        self.assertTrue(rep["reason"].startswith("the record is not evidence of a real agent run"))

    def test_reason_is_truncated_to_the_cap(self):
        rep = self.tasks(BUILD_1089_MIXED)["compliance-rbac-overgrant"]["reps"][1]
        self.assertEqual(len(rep["reason"]), collect.REP_REASON_MAX_CHARS)

    def test_serial_format_parses_like_the_parallel_one(self):
        tasks = self.tasks(BUILD_1075_SERIAL)
        self.assertEqual(
            [r["result"] for r in tasks["capacity-pinned-pool-probe"]["reps"]],
            ["fail", "fail", "pass"],
        )
        # The abort cut the run mid-task: security-overgrant-probe launched
        # (its repetition markers are in the log) but was never graded, so it
        # yields no task entry at all -- launch markers alone carry no verdict.
        self.assertEqual(
            sorted(tasks), ["capacity-pinned-pool-probe", "reliability-pdb-probe"]
        )

    def test_rep_entry_field_names_and_order(self):
        rep = self.tasks(BUILD_1057_PARALLEL)["reliability-pdb-probe"]["reps"][0]
        self.assertEqual(list(rep), ["n", "result", "reason"])
        task = self.tasks(BUILD_1057_PARALLEL)["reliability-pdb-probe"]
        self.assertEqual(
            list(task), ["name", "result", "duration_s", "outcome_validity", "reps"]
        )

    def test_single_rep_era_logs_omit_reps_entirely(self):
        """Absence means unknown: no `reps` key on logs with no rep lines,
        never a fabricated or empty list."""
        for run in collect.runs_from_dir(TESTDATA):
            for task in run["tasks"]:
                self.assertNotIn("reps", task, f"{run['build_id']}/{task['name']}")

    def test_stray_rep_lines_do_not_attach_across_a_section_header(self):
        log = (
            "Task some-case Result: [PASSED] passed all 3 repetitions\n"
            "  rep 1: pass -- VerificationCorrectness=1.0 [OutcomeScore=1.0]\n"
            ">>> [2026-08-31T17:17:00Z] Grading Task: other-case (./tasks/other-case/task.yaml) x3 <<<\n"
            "  rep 2: fail -- looks like a grading line, belongs to nothing\n"
        )
        (task,) = collect.parse_build_log(log)["tasks"]
        self.assertEqual(task["reps"], [{"n": 1, "result": "pass", "reason": None}])

    def test_rep_line_before_any_task_is_ignored(self):
        parsed = collect.parse_build_log("  rep 1: fail -- orphan line\n")
        self.assertEqual(parsed["tasks"], [])


# A stand-in gh for the pr_merged tests: logs every argv so a test can count
# calls, and answers `gh pr view <pr> --repo ... --json state,mergedAt` from
# the FAKE_GH_STATES env mapping. A PR missing from the mapping exits 1 --
# the shape of a real gh failure (no auth, deleted PR, rate limit).
_FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys

with open(os.environ["FAKE_GH_LOG"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
states = json.loads(os.environ["FAKE_GH_STATES"])
pr = sys.argv[3]  # ["pr", "view", "<pr>", "--repo", ...]
state = states.get(pr)
if state is None:
    sys.exit(1)
merged_at = "2026-08-31T18:47:42Z" if state == "MERGED" else None
print(json.dumps({"state": state, "mergedAt": merged_at}))
"""


class TestPrMerged(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def fake_gh(self, states: dict) -> tuple[str, pathlib.Path]:
        gh = self.tmp / "fake-gh"
        gh.write_text(_FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        log = self.tmp / "gh-calls.log"
        log.write_text("")
        os.environ["FAKE_GH_LOG"] = str(log)
        os.environ["FAKE_GH_STATES"] = json.dumps(states)
        self.addCleanup(os.environ.pop, "FAKE_GH_LOG", None)
        self.addCleanup(os.environ.pop, "FAKE_GH_STATES", None)
        return str(gh), log

    @staticmethod
    def collect_quietly(**kwargs):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            data = collect.collect(**kwargs)
        return data, stderr.getvalue()

    def test_without_gh_no_run_carries_the_key(self):
        """Library callers that pass no gh stay hermetic: absent, not null."""
        data = collect.collect(from_dir=TESTDATA)
        for run in data["runs"]:
            self.assertNotIn("pr_merged", run)

    def test_true_false_and_null_from_gh_answers(self):
        gh, _ = self.fake_gh({"1057": "MERGED", "1075": "OPEN", "1089": "CLOSED"})
        now = datetime(2026, 9, 2, tzinfo=timezone.utc)  # fixtures start 08-31/09-01
        data, stderr = self.collect_quietly(from_dir=REPS_TESTDATA, gh=gh, now=now)
        by_pr = {run["pr"]: run["pr_merged"] for run in data["runs"]}
        # MERGED -> true; OPEN and CLOSED-unmerged -> false.
        self.assertEqual(by_pr, {1057: True, 1075: False, 1089: False})
        self.assertNotIn("warning", stderr)

    def test_one_gh_call_per_distinct_pr(self):
        gh, log = self.fake_gh({"956": "MERGED", "998": "MERGED"})
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)  # fixtures start 08-26/27
        data, _ = self.collect_quietly(from_dir=TESTDATA, gh=gh, now=now)
        self.assertEqual(len(data["runs"]), 3)  # two of them share PR 998
        self.assertTrue(all(run["pr_merged"] is True for run in data["runs"]))
        self.assertEqual(len(log.read_text().splitlines()), 2)

    def test_gh_failure_degrades_to_null_with_one_warning(self):
        gh, log = self.fake_gh({})  # every pr view exits 1
        now = datetime(2026, 9, 2, tzinfo=timezone.utc)
        data, stderr = self.collect_quietly(from_dir=REPS_TESTDATA, gh=gh, now=now)
        self.assertTrue(all(run["pr_merged"] is None for run in data["runs"]))
        warnings = [l for l in stderr.splitlines() if "pr_merged unresolved" in l]
        self.assertEqual(len(warnings), 1)
        self.assertIn("3 PR(s)", warnings[0])

    def test_missing_gh_binary_degrades_to_null_with_one_warning(self):
        """The OSError path (no binary at all) trips the circuit breaker:
        everything degrades to null with the single summary warning."""
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        runs = [
            {"build_id": "1", "pr": 10, "started": "2026-08-30T00:00:00+00:00"},
            {"build_id": "2", "pr": 20, "started": "2026-08-30T00:00:00+00:00"},
        ]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            collect.annotate_pr_merged(runs, str(self.tmp / "no-such-gh"), now=now)
        self.assertTrue(all(run["pr_merged"] is None for run in runs))
        warnings = [l for l in stderr.getvalue().splitlines() if "pr_merged unresolved" in l]
        self.assertEqual(len(warnings), 1)
        self.assertIn("2 PR(s)", warnings[0])

    def test_resolution_is_bounded_to_the_display_window(self):
        """true is terminal at any age; anything else resolves (or
        re-resolves) only while the run started within the 14-day window --
        an older run keeps its carried value, or gets null without a call."""
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        runs = [
            {"build_id": "1", "pr": 10, "started": "2026-08-30T00:00:00+00:00", "pr_merged": True},
            {"build_id": "2", "pr": 20, "started": "2026-06-01T00:00:00+00:00", "pr_merged": False},
            {"build_id": "3", "pr": 30, "started": "2026-08-30T00:00:00+00:00", "pr_merged": False},
            {"build_id": "4", "pr": 40, "started": "2026-06-01T00:00:00+00:00"},
            {"build_id": "5", "pr": None, "started": "2026-08-30T00:00:00+00:00"},
            {"build_id": "6", "pr": 60, "started": "2026-08-30T00:00:00+00:00"},
        ]
        gh, log = self.fake_gh({"30": "MERGED", "60": "OPEN"})
        collect.annotate_pr_merged(runs, gh, now=now)
        self.assertIs(runs[0]["pr_merged"], True)  # terminal, not re-asked
        self.assertIs(runs[1]["pr_merged"], False)  # outside window: kept as-is
        self.assertIs(runs[2]["pr_merged"], True)  # recent false: re-asked
        self.assertIsNone(runs[3]["pr_merged"])  # outside window: null, no call
        self.assertIsNone(runs[4]["pr_merged"])  # no PR number: null, no call
        self.assertIs(runs[5]["pr_merged"], False)  # recent first resolution
        asked = {line.split()[2] for line in log.read_text().splitlines()}
        self.assertEqual(asked, {"30", "60"})


if __name__ == "__main__":
    unittest.main()
