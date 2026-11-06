"""Which runs of hack/ci-eval-pr.sh turn the two report-only judge scores off.

A pull request's run exports KUBE_AGENTS_SKIP_REPORT_ONLY_JUDGES=1, which the
bench harness reads to skip devops-bench's ToolInvocation and Checklist
scores. A run with no PULL_NUMBER records baselines and keeps both, and so
does a run whose EVAL_JUDGED_METRICS has rung 6 compare one of the scores the
skip removes or changes. Checked against the block lifted out of the script.
"""

import os
import pathlib
import re
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
BLOCK = re.compile(
    r'^if \[ -n "\$\{PULL_NUMBER:-\}" \] && .*EVAL_JUDGED_METRICS.*\n  export KUBE_AGENTS_SKIP_REPORT_ONLY_JUDGES=1\nfi$',
    re.M,
)


def skipped(env: dict[str, str]) -> bool:
    match = BLOCK.search(SCRIPT.read_text())
    assert match, "the judge-skip block is not in hack/ci-eval-pr.sh"
    probe = match.group(0) + '\necho "${KUBE_AGENTS_SKIP_REPORT_ONLY_JUDGES:-}"\n'
    out = subprocess.run(
        ["bash", "-c", probe], env={"PATH": os.environ["PATH"], **env}, capture_output=True, text=True, check=True
    ).stdout.strip()
    return out == "1"


class JudgeSkipTest(unittest.TestCase):
    def test_a_pull_request_skips(self):
        self.assertTrue(skipped({"PULL_NUMBER": "4937"}))

    def test_a_run_with_no_pull_number_keeps_both(self):
        self.assertFalse(skipped({}))

    def test_the_default_judged_metric_still_skips(self):
        self.assertTrue(skipped({"PULL_NUMBER": "4937", "EVAL_JUDGED_METRICS": "OutcomeValidity"}))

    def test_a_judged_metric_the_skip_touches_keeps_both(self):
        for metrics in ("OutcomeValidity ToolInvocation", "OutcomeValidity,ChecklistScore", "OutcomeScore"):
            with self.subTest(metrics=metrics):
                self.assertFalse(skipped({"PULL_NUMBER": "4937", "EVAL_JUDGED_METRICS": metrics}))


if __name__ == "__main__":
    unittest.main()
