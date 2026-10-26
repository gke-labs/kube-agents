"""The presubmit's lease-time fixture scan, and where its result reaches the fan-out.

hack/ci-eval-pr.sh runs hack/fleet-fixture-state.py against the leased project
after it writes the fleet kubeconfigs (scan_lease_fixtures), maps the report
onto the matrix with kube_agents_bench.fixture_drift once STATE_DIR exists,
and run_one_unit records a marked case's repetitions as infrastructure before
it takes any other lock. What run_one_unit does with a mark is
test_ci_eval_ledger_mint.py's UnitNotRunTest; what the gate makes of the
record is bench/tests/test_fixture_drift.py's. Here: the scan never fails the
job and never leaves a stale or partial report behind, and the steps run in
the order that makes the mark mean something.
"""

import json
import pathlib
import re
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_CI_EVAL_PR = pathlib.Path(__file__).resolve().parents[1] / "hack" / "ci-eval-pr.sh"


def _text():
    return _CI_EVAL_PR.read_text(encoding="utf-8")


def _lift(pattern, what, flags=re.MULTILINE):
    match = re.search(pattern, _text(), flags)
    assert match, f"could not find {what} in hack/ci-eval-pr.sh"
    return match.group(0)


# A stand-in for hack/fleet-fixture-state.py under SCRIPT_DIR: it writes the
# report its --report names, records its arguments, then exits with SCAN_RC.
_SCAN_STUB = """\
import json, os, sys
args = sys.argv[1:]
report = args[args.index("--report") + 1]
with open(report, "w") as fh:
    json.dump({"schema_version": 1, "project": "p", "roles": {}, "summary": {}}, fh)
with open(os.environ["SCAN_ARGS"], "w") as fh:
    fh.write(" ".join(args))
sys.exit(int(os.environ.get("SCAN_RC", "0")))
"""


class ScanLeaseFixturesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        hack = self.tmp / "hack"
        hack.mkdir()
        (hack / "fleet-fixture-state.py").write_text(_SCAN_STUB)
        self.report = self.tmp / "artifacts" / "fixture-state-lease.json"
        self.report.parent.mkdir()
        self.args = self.tmp / "args"

    def _scan(self, rc="0", fleet_dir="set", stale=False):
        if stale:
            self.report.write_text('{"stale": true}')
        script = "\n".join(
            [
                "set -euo pipefail",
                _lift(r"^readonly EVAL_LEASE_FIXTURE_SCAN_TIMEOUT_SECONDS=.*$", "EVAL_LEASE_FIXTURE_SCAN_TIMEOUT_SECONDS"),
                _lift(r"^scan_lease_fixtures\(\) \{.*?^\}", "scan_lease_fixtures", re.DOTALL | re.MULTILINE),
                'scan_lease_fixtures "${REPORT}"',
                'echo "survived"',
            ]
        )
        env = get_isolated_test_env(
            overrides={
                "SCRIPT_DIR": str(self.tmp / "hack"),
                "PROJECT_ID": "kube-agents-evals-7",
                "REPORT": str(self.report),
                "SCAN_ARGS": str(self.args),
                "SCAN_RC": rc,
                "BENCH_FLEET_KUBECONFIG_DIR": str(self.tmp / "fleet") if fleet_dir == "set" else "",
            }
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("survived", proc.stdout, "the scan never stops the job")
        return proc

    def test_a_scan_that_ran_keeps_its_report(self):
        self._scan()
        self.assertEqual(1, json.loads(self.report.read_text())["schema_version"])
        args = self.args.read_text()
        self.assertIn("--wait 0", args)
        self.assertIn(f"--dir {self.tmp / 'fleet'}", args)
        self.assertIn("--project kube-agents-evals-7", args)

    def test_a_failed_scan_leaves_no_report_and_says_cases_grade_as_usual(self):
        proc = self._scan(rc="1")
        self.assertFalse(self.report.exists(), "a partial report must not mark cases")
        self.assertIn("every case is graded as usual", proc.stderr)

    def test_a_timed_out_scan_leaves_no_report(self):
        self._scan(rc="124")
        self.assertFalse(self.report.exists())

    def test_no_fleet_directory_skips_the_scan_and_any_old_report(self):
        proc = self._scan(fleet_dir="", stale=True)
        self.assertFalse(self.report.exists(), "a report from an earlier run must not be read as this lease's")
        self.assertFalse(self.args.exists(), "the scan did not run")
        self.assertIn("every case is graded as usual", proc.stderr)


class MatchStepTest(unittest.TestCase):
    """The top-level step that hands the matrix to kube_agents_bench.fixture_drift,
    lifted out of the script with `uv` and `git` stubbed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        (self.tmp / "state").mkdir()
        self.calls = self.tmp / "uv-args"

    def _step(self, pull_number="", base_sha="", git_ok=True, uv_ok=True, job_type="periodic"):
        script = "\n".join(
            [
                "set -euo pipefail",
                _lift(r"^readonly EVAL_FIXTURE_DRIFT_DIR=.*$", "EVAL_FIXTURE_DRIFT_DIR"),
                _lift(r"^readonly EVAL_FIXTURE_DRIFT_CHANGED_FILES=.*$", "EVAL_FIXTURE_DRIFT_CHANGED_FILES"),
                f'git() {{ {"printf \'bench/tasks/x/task.yaml\\n\'" if git_ok else "return 128"}; }}',
                f'uv() {{ printf "%s\\n" "$*" > "${{UV_ARGS}}"; touch "${{FIXTURE_DRIFT_DIR}}/marked-case"; {"return 0" if uv_ok else "return 1"}; }}',
                "TASKS=(./tasks/a/task.yaml ./tasks/b/task.yaml)",
                _lift(r"^FIXTURE_DRIFT_DIR=.*?^fi\n(?=\n)", "the fixture-drift match step", re.S | re.M),
                'ls "${FIXTURE_DRIFT_DIR}"',
            ]
        )
        env = get_isolated_test_env(
            overrides={
                "STATE_DIR": str(self.tmp / "state"),
                "BENCH_DIR": str(self.tmp),
                "SCRIPT_DIR": str(self.tmp / "hack"),
                "EVAL_LEASE_FIXTURE_REPORT_PATH": str(self.tmp / "report.json"),
                "UV_ARGS": str(self.calls),
                "PULL_NUMBER": pull_number,
                "PULL_BASE_SHA": base_sha,
                "JOB_TYPE": job_type,
            }
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(0, proc.returncode, proc.stderr)
        return proc

    def test_a_periodic_maps_without_a_change_list(self):
        proc = self._step()
        args = self.calls.read_text()
        self.assertIn("kube_agents_bench.fixture_drift", args)
        self.assertNotIn("--changed-files", args)
        self.assertIn("./tasks/a/task.yaml ./tasks/b/task.yaml", args)
        self.assertIn("marked-case", proc.stdout)

    def test_a_tide_batch_hands_over_the_files_its_pulls_change(self):
        # A batch carries PULL_BASE_SHA and JOB_TYPE=batch and no PULL_NUMBER;
        # its pulls are the change under test as much as a presubmit's one.
        self._step(base_sha="abc123", job_type="batch")
        self.assertIn("--changed-files", self.calls.read_text())

    def test_a_tide_batch_with_no_change_list_excuses_nothing(self):
        for base, git_ok in (("", True), ("abc123", False)):
            with self.subTest(base=base, git_ok=git_ok):
                self.calls.unlink(missing_ok=True)
                proc = self._step(base_sha=base, git_ok=git_ok, job_type="batch")
                self.assertFalse(self.calls.exists(), "the mapping did not run")
                self.assertIn("every case is graded as usual", proc.stderr)

    def test_a_pull_request_hands_over_the_files_it_changes(self):
        self._step(pull_number="3990", base_sha="abc123")
        args = self.calls.read_text()
        self.assertIn("--changed-files", args)
        changed = self.tmp / "state" / "fixture-drift-changed-files.txt"
        self.assertEqual("bench/tasks/x/task.yaml\n", changed.read_text())

    def test_a_pull_request_with_no_change_list_excuses_nothing(self):
        for base, git_ok in (("", True), ("abc123", False)):
            with self.subTest(base=base, git_ok=git_ok):
                self.calls.unlink(missing_ok=True)
                proc = self._step(pull_number="3990", base_sha=base, git_ok=git_ok)
                self.assertFalse(self.calls.exists(), "the mapping did not run")
                self.assertIn("every case is graded as usual", proc.stderr)

    def test_a_mapping_that_fails_leaves_no_mark(self):
        proc = self._step(uv_ok=False)
        self.assertNotIn("marked-case", proc.stdout)
        self.assertIn("every case is graded as usual", proc.stderr)


class OrderTest(unittest.TestCase):
    """The mark is computed from this lease's scan, before the fan-out, and
    read before a unit takes anything it would hold while running."""

    def test_the_scan_follows_the_fleet_kubeconfigs_and_precedes_the_mapping(self):
        text = _text()
        kubeconfigs = text.index("write_fleet_kubeconfigs || {")
        scan = text.index('scan_lease_fixtures "${EVAL_LEASE_FIXTURE_REPORT_PATH}"')
        state = text.index('STATE_DIR="$(mktemp -d)"')
        mapping = text.index("uv run python -m kube_agents_bench.fixture_drift")
        fan_out = text.index('launch_units "${UNIT_QUEUE}"')
        self.assertLess(kubeconfigs, scan)
        self.assertLess(scan, state)
        self.assertLess(state, mapping)
        self.assertLess(mapping, fan_out)

    def test_run_one_unit_reads_the_mark_before_the_stream_locks_and_the_launch(self):
        unit = _lift(r"^run_one_unit\(\) \{.*?^\}", "run_one_unit", re.DOTALL | re.MULTILINE)
        mark = unit.index('[ -s "${drift_file}" ]')
        self.assertLess(unit.index('lock_acquire "${STATE_DIR}/lock-task-${name}"'), mark)
        self.assertLess(mark, unit.index('lock_acquire "${STATE_DIR}/lock-stream-${s}"'))
        self.assertLess(mark, unit.index("wait_platform_runs"))


if __name__ == "__main__":
    unittest.main()
