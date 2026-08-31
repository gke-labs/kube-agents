"""Unit tests for scripts/release/run_optional_e2e_suites.sh.

The optional suites are the pipeline's tolerated coverage, so the thing worth
pinning is that tolerance does not become silence: every suite in the list runs
even after one fails, the exit status still reports that something failed, and
the job summary names which. A loop that stopped at the first failure would
quietly drop the suites behind it and the step would look the same.
"""

import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "run_optional_e2e_suites.sh"

_CALLS_LOG = "suites.log"


class RunOptionalE2ESuitesTest(unittest.TestCase):
    def _run(self, optional_suites, failing_suites=()):
        """Runs the script with a mock runner that fails for the named suites."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)

        calls = tmp_dir / _CALLS_LOG
        summary = tmp_dir / "step_summary.md"
        summary.touch()

        # The mock stands in for execute_e2e_tests.sh. It records the suite it
        # was asked for so the test can assert on order and completeness, and
        # exits non-zero for the suites this case wants to fail.
        runner = tmp_dir / "mock_runner.sh"
        failing = " ".join(failing_suites)
        runner.write_text(f"""#!/usr/bin/env bash
# Invoked as: mock_runner.sh --suite <name>
suite="$2"
echo "$suite" >> "{calls}"
for failing in {failing or '""'}; do
  if [ "$suite" = "$failing" ]; then
    exit 1
  fi
done
exit 0
""")
        runner.chmod(0o755)

        env = get_isolated_test_env(
            overrides={
                "OPTIONAL_SUITES": optional_suites,
                "E2E_RUNNER": str(runner),
                "GITHUB_STEP_SUMMARY": str(summary),
            }
        )
        proc = subprocess.run(
            ["bash", str(_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_dir),
        )
        ran = calls.read_text().splitlines() if calls.exists() else []
        return proc, ran, summary.read_text()

    def test_runs_every_suite_in_order(self):
        proc, ran, _ = self._run("agent-plugin,gchat,stockout-full")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(ran, ["agent-plugin", "gchat", "stockout-full"])

    def test_a_failing_suite_does_not_stop_the_ones_after_it(self):
        """The reason the input is a list: one failure must not hide the rest."""
        proc, ran, _ = self._run(
            "agent-plugin,gchat,stockout-full", failing_suites=("agent-plugin",)
        )
        self.assertEqual(ran, ["agent-plugin", "gchat", "stockout-full"])
        self.assertNotEqual(proc.returncode, 0)

    def test_exits_non_zero_when_any_suite_failed(self):
        proc, _, _ = self._run("gchat,stockout-full", failing_suites=("stockout-full",))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("stockout-full", proc.stderr)

    def test_warns_per_failing_suite(self):
        proc, _, _ = self._run(
            "agent-plugin,gchat", failing_suites=("agent-plugin", "gchat")
        )
        self.assertEqual(proc.stderr.count("::warning title=Optional E2E suite failed"), 2)

    def test_summary_names_each_suite_and_its_result(self):
        _, _, summary = self._run("agent-plugin,gchat", failing_suites=("gchat",))
        self.assertIn("| `agent-plugin` | ✅ passed |", summary)
        self.assertIn("| `gchat` | ❌ failed (tolerated) |", summary)

    def test_tolerates_whitespace_around_names(self):
        _, ran, _ = self._run(" agent-plugin , gchat ")
        self.assertEqual(ran, ["agent-plugin", "gchat"])

    def test_empty_list_is_a_no_op(self):
        """A caller with only a gate omits the input, which arrives as ''."""
        proc, ran, summary = self._run("")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(ran, [])
        self.assertEqual(summary, "")

    def test_separators_only_is_also_a_no_op(self):
        proc, ran, _ = self._run(" , , ")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(ran, [])

    def test_single_suite_needs_no_comma(self):
        proc, ran, _ = self._run("rc")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(ran, ["rc"])


if __name__ == "__main__":
    unittest.main()
