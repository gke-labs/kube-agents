"""The eval job's verdict tail announces a not-evaluated run only on the JSON's word.

`hack/ci-eval-pr.sh` ends by running `bench-gate suite`, printing one final
line and exiting with the job's status. Since the suite gained a third outcome
that tail has a branch the bench tests cannot reach: status 2 from the suite is
announced as NOT EVALUATED, and the job exits 2, only when `eval-verdict.json`
carries `outcome: not_evaluated` -- because argparse also exits 2 on a bad
flag, and `uv run` can exit 2 without reaching bench-gate at all. Anything else
that is not 0 is the plain red line and exit 1.

This runs the real function out of the real file, with the script's own two
constants, rather than grepping for the branch: it fails if the confirmation is
removed, if the key or the word it checks drifts from what `scoring.py` writes,
or if the final line loses an anchor the dashboard collector matches.

`hack/ci-eval-rc.sh` carries a second hand-synced copy of the status and the
outcome word, read when it maps the presubmit's exit 2 to its `NOT RUN`. The
drift check here covers that copy too: `tests/test_ci_eval_rc.py` pins the RC
driver through a stub that emits the literals, so a Python-side rename that the
presubmit copy is forced to follow would otherwise leave the RC driver green and
comparing against the old value.
"""

import json
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
RC_SCRIPT = REPO_ROOT / "hack" / "ci-eval-rc.sh"
SCORING = REPO_ROOT / "bench" / "kube_agents_bench" / "scoring.py"
GATE = REPO_ROOT / "bench" / "kube_agents_bench" / "gate.py"
FUNCTION = "announce_suite_verdict"
CONSTANTS = ("EVAL_SUITE_NOT_EVALUATED_STATUS", "EVAL_VERDICT_OUTCOME_NOT_EVALUATED")
RC_CONSTANTS = ("EVAL_NOT_EVALUATED_STATUS", "EVAL_NOT_EVALUATED_OUTCOME")

# What scripts/eval_dashboard/collect.py's `_FINAL_VERDICT` needs from the
# final line: the verdict word, then the duration in this exact shape. Held
# here as the expected value so a reworded line that no longer parses fails
# this test rather than dropping the run's verdict from the dashboard.
FINAL_LINE = re.compile(
    r"PR Smoke Test Evaluation (?P<verdict>Succeeded|Failed)"
    r".*\(Total Duration:\s*(?P<duration>\d+)s\)"
)


def _lifted(pattern: str, what: str, flags: int = re.M, path: pathlib.Path = SCRIPT) -> str:
    src = path.read_text(encoding="utf-8")
    match = re.search(pattern, src, flags)
    if match is None:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"{what} not found in {path}")
    return match.group(0)


def function_body() -> str:
    return _lifted(rf"^{FUNCTION}\(\) \{{\n.*?^\}}$", f"{FUNCTION}()", re.S | re.M)


def constants() -> list[str]:
    return [_lifted(rf"^readonly {name}=.*$", name) for name in CONSTANTS]


def rc_constants() -> list[str]:
    return [_lifted(rf"^readonly {name}=.*$", name, path=RC_SCRIPT) for name in RC_CONSTANTS]


def _shell_value(line: str) -> str:
    return line.split("=", 1)[1].strip('"')


def _python_constant(path: pathlib.Path, name: str) -> str:
    match = re.search(rf"^{name} = (.+)$", path.read_text(encoding="utf-8"), re.M)
    if match is None:  # pragma: no cover
        raise AssertionError(f"{name} not found in {path}")
    return match.group(1).strip()


class AnnounceSuiteVerdictTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.artifacts = pathlib.Path(self._tmp.name)
        self.verdict_json = self.artifacts / "eval-verdict.json"
        self.verdict_md = self.artifacts / "eval-verdict.md"

    def announce(self, status: int, outcome: str | None) -> subprocess.CompletedProcess:
        """Run the lifted function under the script's own shell options."""
        if outcome is not None:
            self.verdict_json.write_text(json.dumps({"outcome": outcome}), encoding="utf-8")
        script = "\n".join(
            [
                "set -euo pipefail",
                *constants(),
                function_body(),
                f'{FUNCTION} "$@"',
            ]
        )
        return subprocess.run(
            ["bash", "-c", script, "bash", str(status), str(self.verdict_json), str(self.verdict_md), "7"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_green_prints_succeeded_and_returns_zero(self):
        result = self.announce(0, "green")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        match = FINAL_LINE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        self.assertEqual(match.group("verdict"), "Succeeded")
        self.assertEqual(match.group("duration"), "7")

    def test_red_prints_failed_and_returns_one(self):
        result = self.announce(1, "red")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(FINAL_LINE.search(result.stdout).group("verdict"), "Failed")
        self.assertNotIn("NOT EVALUATED", result.stdout)

    def test_status_2_with_the_json_confirming_is_announced_and_returns_two(self):
        result = self.announce(2, "not_evaluated")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("NOT EVALUATED", result.stdout)
        self.assertIn("rerun when the environment is healthy", result.stdout)
        # The dashboard still gets a final line it can parse.
        match = FINAL_LINE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        self.assertEqual(match.group("verdict"), "Failed")
        self.assertEqual(match.group("duration"), "7")

    def test_status_2_with_nothing_graded_names_the_transport_not_the_environment(self):
        """The inject lane's shape (#2039): `not_evaluated` with no lost case
        and the set-aside cases under `not_graded`. A rerun reproduces it, so
        the final line must not say rerun."""
        self.verdict_json.write_text(
            json.dumps({"outcome": "not_evaluated", "not_evaluated": [], "not_graded": ["a", "b"]}),
            encoding="utf-8",
        )
        result = self.announce(2, None)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("NOT EVALUATED", result.stdout)
        self.assertIn("not graded on this transport", result.stdout)
        self.assertNotIn("rerun when the environment is healthy", result.stdout)
        match = FINAL_LINE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        self.assertEqual(match.group("verdict"), "Failed")

    def test_status_2_with_lost_cases_beside_not_graded_ones_still_says_rerun(self):
        self.verdict_json.write_text(
            json.dumps({"outcome": "not_evaluated", "not_evaluated": ["a"], "not_graded": ["b"]}),
            encoding="utf-8",
        )
        result = self.announce(2, None)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("rerun when the environment is healthy", result.stdout)

    def test_status_2_without_the_json_is_the_plain_red(self):
        """argparse's 2, or a `uv run` that never reached bench-gate."""
        result = self.announce(2, None)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("NOT EVALUATED", result.stdout)
        self.assertEqual(FINAL_LINE.search(result.stdout).group("verdict"), "Failed")

    def test_status_2_with_the_json_saying_something_else_is_the_plain_red(self):
        result = self.announce(2, "red")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("NOT EVALUATED", result.stdout)

    def test_status_1_is_red_whatever_the_json_says(self):
        """The two have to agree; the JSON alone does not make a not-evaluated."""
        result = self.announce(1, "not_evaluated")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("NOT EVALUATED", result.stdout)

    def test_the_shell_constants_match_the_scorer_and_the_gate(self):
        """The word and the status are held on both sides of the hand-off and
        nothing at runtime checks they agree, so this does."""
        status_line, outcome_line = constants()
        self.assertEqual(
            _shell_value(status_line),
            _python_constant(GATE, "SUITE_EXIT_NOT_EVALUATED"),
        )
        self.assertEqual(
            _shell_value(outcome_line),
            _python_constant(SCORING, "SUITE_OUTCOME_NOT_EVALUATED").strip('"'),
        )

    def test_the_rc_driver_constants_match_the_scorer_and_the_gate(self):
        """The RC driver's own copy, one hand-off further down: it reads the
        presubmit's status and the JSON's word, and its tests stub both."""
        status_line, outcome_line = rc_constants()
        self.assertEqual(
            _shell_value(status_line),
            _python_constant(GATE, "SUITE_EXIT_NOT_EVALUATED"),
        )
        self.assertEqual(
            _shell_value(outcome_line),
            _python_constant(SCORING, "SUITE_OUTCOME_NOT_EVALUATED").strip('"'),
        )


if __name__ == "__main__":
    unittest.main()
