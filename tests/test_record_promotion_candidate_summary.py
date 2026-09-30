"""Unit tests for scripts/release/record_promotion_candidate_summary.sh.

Step 1 of the staging promotion pipeline has two skips that mean different things, and
this summary is where a reader tells them apart: SKIP_PIPELINE means no
candidate and no run, SKIP_PROMOTION means the matrix runs but a pass nominates
nothing because the commit has already been through the promotion path.
Rendering one as the other would report a run that did nothing as a run that
tested something.

The "Promotes" line has its own trap. A green matrix stopped being sufficient
when the release-candidate eval became a gate, so wording that credits the
matrix alone would tell a reader the tag follows from the run they are looking
at, when it follows from an eval that has not started yet.
"""

import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "record_promotion_candidate_summary.sh"

_COMMIT = "1234567890abcdef1234567890abcdef12345678"
_RC_TAG = "rc_20260830_120000_1234567_validated"
_EVALCAND_TAG = "evalcand_20260830_120000_1234567"
_STAGING_TAG = "staging_20260830_120000_1234567"


class RecordPromotionCandidateSummaryTest(unittest.TestCase):
    def _run(self, overrides=None, with_summary_file=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)

        env_overrides = {
            "COMMIT_SHA": _COMMIT,
            "RC_TAG": _RC_TAG,
            "EVALCAND_TAG": _EVALCAND_TAG,
            "STAGING_TAG": _STAGING_TAG,
            "SKIP_PIPELINE": "false",
            "SKIP_PROMOTION": "false",
            "SKIP_REASON": "",
            **(overrides or {}),
        }

        summary = tmp_dir / "step_summary.md"
        if with_summary_file:
            summary.touch()
            env_overrides["GITHUB_STEP_SUMMARY"] = str(summary)

        proc = subprocess.run(
            ["bash", str(_SCRIPT)],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=env_overrides),
            cwd=str(tmp_dir),
        )
        written = summary.read_text() if with_summary_file else ""
        return proc, written

    def test_reports_the_candidate_it_will_test(self):
        proc, summary = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"| Candidate | `{_RC_TAG}` |", summary)
        self.assertIn(f"| Commit | `{_COMMIT}` |", summary)
        self.assertIn(f"| Eval-candidate tag | `{_EVALCAND_TAG}` |", summary)
        self.assertIn(f"| Staging tag | `{_STAGING_TAG}` |", summary)
        self.assertIn("| Promotes | yes, if the matrix and the eval pass |", summary)

    def test_skip_pipeline_reports_no_matrix_and_no_table(self):
        proc, summary = self._run(
            {"SKIP_PIPELINE": "true", "SKIP_REASON": "no validated candidate"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("No matrix this run: no validated candidate", summary)
        self.assertNotIn("| Candidate |", summary)

    def test_skip_pipeline_carries_a_reason_that_names_a_candidate(self):
        """SKIP_PIPELINE has two causes and the line must not assert either.

        A refused candidate is a real, validated one, so wording that says no
        candidate exists would contradict the reason printed beside it.
        """
        _, summary = self._run(
            {
                "SKIP_PIPELINE": "true",
                "SKIP_REASON": f"Candidate '{_RC_TAG}' predates the shared-pipeline restructure.",
            }
        )
        self.assertIn(f"No matrix this run: Candidate '{_RC_TAG}' predates", summary)
        self.assertNotIn("| Candidate |", summary)

    def test_skip_promotion_still_reports_the_candidate(self):
        """The matrix runs; the nomination and the tag push are skipped."""
        _, summary = self._run({"SKIP_PROMOTION": "true"})
        self.assertIn(f"| Candidate | `{_RC_TAG}` |", summary)
        self.assertIn("| Promotes | no — already nominated or promoted |", summary)

    def test_the_promotes_line_does_not_credit_the_matrix_alone(self):
        """A green matrix nominates a candidate; the eval is what promotes it."""
        _, summary = self._run()
        self.assertNotIn("| Promotes | yes, if the matrix passes |", summary)
        self.assertIn("eval", summary)

    def test_reason_is_appended_when_the_pipeline_runs(self):
        _, summary = self._run(
            {"SKIP_PROMOTION": "true", "SKIP_REASON": "already carries a staging tag"}
        )
        self.assertIn("already carries a staging tag", summary)

    def test_appends_rather_than_truncating(self):
        """GITHUB_STEP_SUMMARY accumulates across steps; a clobber loses them."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        summary = pathlib.Path(tmp.name) / "step_summary.md"
        summary.write_text("### An earlier step\n")

        subprocess.run(
            ["bash", str(_SCRIPT)],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "COMMIT_SHA": _COMMIT,
                    "RC_TAG": _RC_TAG,
                    "EVALCAND_TAG": _EVALCAND_TAG,
                    "STAGING_TAG": _STAGING_TAG,
                    "SKIP_PIPELINE": "false",
                    "SKIP_PROMOTION": "false",
                    "SKIP_REASON": "",
                    "GITHUB_STEP_SUMMARY": str(summary),
                }
            ),
            cwd=tmp.name,
        )
        self.assertIn("### An earlier step", summary.read_text())
        self.assertIn("### Promotion candidate", summary.read_text())

    def test_runs_outside_actions_without_a_summary_file(self):
        """No GITHUB_STEP_SUMMARY: print instead of failing on an unset path."""
        proc, _ = self._run(with_summary_file=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("### Promotion candidate", proc.stdout)

    def test_unset_inputs_do_not_abort(self):
        """Actions defines an `env:` key even when its expression is empty."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proc = subprocess.run(
            ["bash", str(_SCRIPT)],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(),
            cwd=tmp.name,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
