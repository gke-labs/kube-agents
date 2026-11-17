"""hack/ci-deploy.sh: inside a Prow job the lane's config sets the agent
provider and model (from the build cluster's eval-model Secret), and the
script refuses a job that arrives without them, before the image build,
instead of falling back to its local default, a model nobody chose. A local
run (no JOB_NAME) keeps the default. hack/ci-eval-pr.sh's AGENT_MODEL_OVERRIDE
is checked in the same place, since the deploy runs first in every lane.

The guard is lifted between its markers plus the default line that follows,
and run under `set -euo pipefail`, the way the script runs."""

import pathlib
import subprocess
import unittest

from scripts.eval_dashboard import periodics

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CI_DEPLOY = REPO_ROOT / "hack" / "ci-deploy.sh"
CI_EVAL = REPO_ROOT / "hack" / "ci-eval-pr.sh"
BEGIN = "# --- agent model required in a Prow job: begin"
END = "# --- agent model required in a Prow job: end"
JOB = {"JOB_NAME": "pull-kube-agents-smoke-test"}
ALL_SET = {**JOB, "MODEL_PROVIDER": "gemini", "MODEL_DEFAULT_NAME": "some-model", "AGENT_MODEL_OVERRIDE": "some-model"}


def guard_and_default() -> str:
    """The guard block and the `export MODEL_PROVIDER=` default line right after it."""
    text = CI_DEPLOY.read_text()
    if BEGIN not in text or END not in text:
        raise AssertionError(f"ci-deploy.sh has no block between {BEGIN!r} and {END!r}")
    end = text.index(END)
    rest = text[end:].split("\n")
    return text[text.index(BEGIN) : end] + "\n".join(rest[:2]) + "\n"


def run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + script], env={"PATH": "/usr/bin:/bin", **env}, capture_output=True, text=True, check=False)


class DeployRequiresTheModelInAProwJob(unittest.TestCase):
    def assert_stopped_naming(self, result: subprocess.CompletedProcess, variable: str) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("REACHED", result.stdout)
        self.assertIn(variable, result.stderr)
        self.assertIn("eval-model", result.stderr, "the message says where a lane sets it")
        # The dashboard's error-line regex (scripts/eval_dashboard/periodics.py) keys on this
        # prefix and quotes the line cut at LAST_LINE_MAX_CHARS, so the pointer fits in it.
        first_line = result.stderr.splitlines()[0]
        self.assertTrue(first_line.startswith("FATAL: "), first_line)
        self.assertLessEqual(len(first_line), periodics.LAST_LINE_MAX_CHARS, first_line)
        self.assertIn("ci-pool-projects.md 5.10", first_line)

    def test_a_prow_job_without_a_provider_stops_before_deploying(self) -> None:
        env = {k: v for k, v in ALL_SET.items() if k != "MODEL_PROVIDER"}
        self.assert_stopped_naming(run(guard_and_default() + 'echo "REACHED"\n', env), "MODEL_PROVIDER")

    def test_a_prow_job_without_a_model_name_stops_before_deploying(self) -> None:
        env = {k: v for k, v in ALL_SET.items() if k != "MODEL_DEFAULT_NAME"}
        self.assert_stopped_naming(run(guard_and_default() + 'echo "REACHED"\n', env), "MODEL_DEFAULT_NAME")

    def test_a_prow_job_without_the_eval_override_stops_before_deploying(self) -> None:
        # hack/ci-eval-pr.sh's variable, checked here so the lease is not spent first.
        env = {k: v for k, v in ALL_SET.items() if k != "AGENT_MODEL_OVERRIDE"}
        self.assert_stopped_naming(run(guard_and_default() + 'echo "REACHED"\n', env), "AGENT_MODEL_OVERRIDE")

    def test_an_empty_value_counts_as_unset(self) -> None:
        result = run(guard_and_default() + 'echo "REACHED"\n', {**ALL_SET, "MODEL_PROVIDER": ""})
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_a_prow_job_with_all_three_set_passes_through_unchanged(self) -> None:
        result = run(guard_and_default() + 'echo "REACHED ${MODEL_PROVIDER} ${MODEL_DEFAULT_NAME} ${AGENT_MODEL_OVERRIDE}"\n', ALL_SET)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REACHED gemini some-model some-model", result.stdout)

    def test_a_local_run_without_job_name_keeps_the_default(self) -> None:
        result = run(guard_and_default() + 'echo "REACHED ${MODEL_PROVIDER}"\n', {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REACHED vertex_ai", result.stdout, "the local default survives")


class EvalLeavesTheCheckToTheDeploy(unittest.TestCase):
    def test_ci_eval_pr_carries_no_guard_of_its_own(self) -> None:
        # One place checks the three variables (the deploy, first in every
        # lane); a second check in the eval would fire only after the lease
        # and the GitOps resets were spent.
        self.assertNotIn(BEGIN, CI_EVAL.read_text())
        self.assertIn('export AGENT_MODEL="${AGENT_MODEL_OVERRIDE:-', CI_EVAL.read_text(), "the local default stays")


if __name__ == "__main__":
    unittest.main()
