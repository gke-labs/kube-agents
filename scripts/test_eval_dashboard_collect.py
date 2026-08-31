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

import json
import pathlib
import tempfile
import unittest

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
