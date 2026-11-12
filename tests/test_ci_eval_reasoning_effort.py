"""hack/ci-eval-pr.sh checks the reasoning effort before the first unit and keeps such a run out of the store.

The block between its marker comments refuses an effort the chart would not
take, stops a run whose served model (MODEL_DEFAULT_NAME) and recorded model
(AGENT_MODEL) differ, and echoes the setup. Below the fan-out a non-empty
effort makes the run a reader of the baseline store, never a writer: the
version key carries no effort, so its lines would pool with the default-effort
window. gke-labs/kube-agents#3332.
"""

import pathlib
import subprocess
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL = _REPO_ROOT / "hack" / "ci-eval-pr.sh"

_BEGIN_MARKER = "# --- reasoning effort: begin"
_END_MARKER = "# --- reasoning effort: end"
_STORE_RULE = (
    'if [ -n "${EVAL_REASONING_EFFORT:-}" ]; then\n'
    '  EVAL_IS_MAIN_RUN="false"\n'
    "fi\n"
)
_STORE_ECHO = 'elif [ -n "${EVAL_REASONING_EFFORT:-}" ] && [ -z "${PULL_NUMBER:-}" ]; then'
_CERTIFICATION_RULE = 'if [ "${EVAL_TIER}" = "certification" ]; then\n  EVAL_IS_MAIN_RUN="false"\nfi\n'
_DEFAULT_MODEL = "gemini-3.1-pro-preview"

_HARNESS = """set -euo pipefail
export AGENT_MODEL="${{AGENT_MODEL_OVERRIDE:-{default_model}}}"
eval "$(sed -n '/^{begin}/,/^{end}/p' "$CI_EVAL")"
printf 'EFFORT=[%s]\\n' "${{EVAL_REASONING_EFFORT}}"
"""


def _text() -> str:
    return _CI_EVAL.read_text(encoding="utf-8")


class ReasoningEffortBlockTest(unittest.TestCase):
    def _run(self, effort: str | None, served: str | None = None, override: str | None = None) -> subprocess.CompletedProcess:
        harness = _HARNESS.format(default_model=_DEFAULT_MODEL, begin=_BEGIN_MARKER, end=_END_MARKER)
        overrides = {"CI_EVAL": str(_CI_EVAL), "MODEL_PROVIDER": "vertex_ai"}
        absent = ["EVAL_REASONING_EFFORT", "MODEL_DEFAULT_NAME", "AGENT_MODEL_OVERRIDE"]
        for name, value in (("EVAL_REASONING_EFFORT", effort), ("MODEL_DEFAULT_NAME", served), ("AGENT_MODEL_OVERRIDE", override)):
            if value is not None:
                overrides[name] = value
                absent.remove(name)
        env = get_isolated_test_env(overrides=overrides, absent=tuple(absent))
        return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env=env)

    def test_the_block_sits_after_the_agent_model_line(self) -> None:
        text = _text()
        self.assertEqual(text.count(_BEGIN_MARKER), 1)
        self.assertLess(text.index('export AGENT_MODEL="${AGENT_MODEL_OVERRIDE:-'), text.index(_BEGIN_MARKER))
        self.assertLess(text.index(_END_MARKER), text.index("unset NAMESPACE"))

    def test_empty_or_unset_is_the_model_default(self) -> None:
        for effort in (None, ""):
            with self.subTest(effort=effort):
                proc = self._run(effort)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("EFFORT=[]", proc.stdout)
                self.assertIn("reasoning effort: model default", proc.stdout)

    def test_the_three_values_pass(self) -> None:
        for effort in ("low", "medium", "high"):
            with self.subTest(effort=effort):
                proc = self._run(effort)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(f"reasoning effort: {effort}", proc.stdout)

    def test_another_value_stops_before_any_unit(self) -> None:
        for effort in ("xhigh", "LOW", "low,high"):
            with self.subTest(effort=effort):
                proc = self._run(effort)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("ERROR: EVAL_REASONING_EFFORT=", proc.stderr)
                self.assertNotIn("EFFORT=[", proc.stdout)

    def test_a_served_model_that_differs_from_the_recorded_one_stops(self) -> None:
        # The job sets the override equal to the served name; a job that set the
        # name alone would leave AGENT_MODEL at the script's default.
        proc = self._run("", served="served-model")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("MODEL_DEFAULT_NAME=served-model", proc.stderr)
        self.assertIn(f"AGENT_MODEL={_DEFAULT_MODEL}", proc.stderr)

    def test_a_served_model_equal_to_the_recorded_one_passes(self) -> None:
        proc = self._run("low", served="served-model", override="served-model")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("vertex_ai/served-model, reasoning effort: low", proc.stdout)

    def test_no_served_model_means_a_local_run_and_passes(self) -> None:
        proc = self._run("high")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"/{_DEFAULT_MODEL}, reasoning effort: high", proc.stdout)


class ReasoningEffortStoreRuleTest(unittest.TestCase):
    def test_a_named_effort_never_appends_to_the_store(self) -> None:
        text = _text()
        self.assertEqual(text.count(_STORE_RULE), 1, "the effort rule must sit once beside the other recorder rules")
        self.assertLess(text.index(_CERTIFICATION_RULE), text.index(_STORE_RULE))
        self.assertLess(text.index(_STORE_RULE), text.index("Recording baseline evidence from main"))

    def test_the_record_step_names_the_effort_as_the_reason(self) -> None:
        text = _text()
        self.assertEqual(text.count(_STORE_ECHO), 1)
        self.assertLess(text.index('elif [ "${EVAL_ROLLBACK_LANE:-}" = "1" ] && [ -z "${PULL_NUMBER:-}" ]; then'), text.index(_STORE_ECHO))
        self.assertLess(text.index(_STORE_ECHO), text.index("Not a main-branch recorder run"))

    def test_the_rule_flips_the_decision(self) -> None:
        body = "\n".join(['EVAL_IS_MAIN_RUN="true"', _STORE_RULE, 'printf "%s\\n" "$EVAL_IS_MAIN_RUN"'])
        for effort, expected in (("", "true"), ("low", "false"), ("high", "false")):
            with self.subTest(effort=effort):
                env = get_isolated_test_env(overrides={"EVAL_REASONING_EFFORT": effort})
                proc = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)
                self.assertEqual(proc.stdout.strip(), expected, proc.stderr)


if __name__ == "__main__":
    unittest.main()
