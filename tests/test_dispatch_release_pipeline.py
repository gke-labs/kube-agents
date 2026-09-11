"""Unit tests for scripts/release/dispatch_release_pipeline.sh and its skip counterpart.

release-scheduler.yml is the only scheduled mechanism that starts the GA
release pipeline, so a dispatch that fails quietly means no GA release is
published until somebody notices. These pin the annotation that says so, the
arguments that decide which workflow gets dispatched, and the refusal to run on
a missing input.
"""

import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import create_minimal_tools_bin, get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DISPATCH_SCRIPT = (
    _REPO_ROOT / "scripts" / "release" / "dispatch_release_pipeline.sh"
)
_SKIP_SCRIPT = (
    _REPO_ROOT / "scripts" / "release" / "record_release_scheduler_skip.sh"
)

_COMMIT = "1234567890abcdef1234567890abcdef12345678"
_GATE_TAG = "staging_20260830_120000_1234567"
_CALLS_LOG = "gh_calls.log"


class DispatchReleasePipelineTest(unittest.TestCase):
    def _run(self, gh_exit=0, overrides=None, omit=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)

        calls = tmp_dir / _CALLS_LOG
        summary = tmp_dir / "step_summary.md"
        summary.touch()

        bin_dir = create_minimal_tools_bin(tmp_dir)
        mock_gh = bin_dir / "gh"
        mock_gh.write_text(f"""#!/usr/bin/env bash
echo "$*" >> "{calls}"
exit {gh_exit}
""")
        mock_gh.chmod(0o755)

        env_overrides = {
            "RELEASE_COMMIT": _COMMIT,
            "GATE_TAG": _GATE_TAG,
            "GITHUB_REPOSITORY": "gke-labs/kube-agents",
            "GITHUB_REF_NAME": "main",
            "GITHUB_STEP_SUMMARY": str(summary),
            **(overrides or {}),
        }
        for key in omit:
            env_overrides.pop(key, None)

        proc = subprocess.run(
            ["bash", str(_DISPATCH_SCRIPT)],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides=env_overrides, bin_dir=str(bin_dir)
            ),
            cwd=str(tmp_dir),
        )
        recorded = calls.read_text() if calls.exists() else ""
        return proc, recorded, summary.read_text()

    def test_dispatches_the_pipeline_with_schedule_gate_evaluate(self):
        proc, recorded, _ = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("workflow run release-publish.yml", recorded)
        self.assertIn("schedule_gate=evaluate", recorded)

    def test_dispatches_against_the_ref_the_scheduler_ran_on(self):
        _, recorded, _ = self._run(overrides={"GITHUB_REF_NAME": "release-1.2"})
        self.assertIn("--ref release-1.2", recorded)
        self.assertIn("--repo gke-labs/kube-agents", recorded)

    def test_records_the_dispatch_in_the_job_summary(self):
        _, _, summary = self._run()
        self.assertIn("### GA release pipeline dispatched", summary)
        self.assertIn(f"| Commit | `{_COMMIT[:7]}` |", summary)
        self.assertIn(f"| Gate tag | `{_GATE_TAG}` |", summary)
        self.assertIn("| Mode | `evaluate` |", summary)

    def test_a_failed_dispatch_is_an_error_annotation_not_a_bare_exit(self):
        """This failure means no GA release is being published; it must say so."""
        proc, _, summary = self._run(gh_exit=1)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            "::error title=GA release pipeline dispatch failed", proc.stderr
        )
        self.assertIn("No GA release is being published", proc.stderr)
        self.assertNotIn("dispatched", summary)

    def test_missing_github_repository_aborts_before_calling_gh(self):
        proc, recorded, _ = self._run(omit=("GITHUB_REPOSITORY",))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(recorded, "")
        self.assertIn("GITHUB_REPOSITORY", proc.stderr)

    def test_missing_github_ref_name_aborts_before_calling_gh(self):
        proc, recorded, _ = self._run(omit=("GITHUB_REF_NAME",))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(recorded, "")
        self.assertIn("GITHUB_REF_NAME", proc.stderr)

    def test_missing_gh_cli_aborts_with_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)
        # Sterile PATH with standard tools but NO gh
        bin_dir = create_minimal_tools_bin(tmp_dir)
        env = get_isolated_test_env(
            overrides={
                "PATH": str(bin_dir),
                "RELEASE_COMMIT": _COMMIT,
                "GATE_TAG": _GATE_TAG,
                "GITHUB_REPOSITORY": "gke-labs/kube-agents",
                "GITHUB_REF_NAME": "main",
            },
        )
        proc = subprocess.run(
            ["bash", str(_DISPATCH_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_dir),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("::error title=Missing dependency::", proc.stderr)
        self.assertIn("gh CLI is required", proc.stderr)

    def test_dispatches_cleanly_when_commit_and_tag_are_omitted(self):
        proc, recorded, summary = self._run(
            overrides={"RELEASE_COMMIT": "", "GATE_TAG": ""}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("workflow run release-publish.yml", recorded)
        self.assertIn("### GA release pipeline dispatched", summary)
        self.assertNotIn("Commit", summary)
        self.assertNotIn("Gate tag", summary)

    def test_dispatches_with_overridden_workflow_file(self):
        proc, recorded, _ = self._run(
            overrides={"WORKFLOW_FILE": "custom-release.yml"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("workflow run custom-release.yml", recorded)

    def test_runs_outside_actions_without_step_summary_file(self):
        proc, recorded, _ = self._run(omit=("GITHUB_STEP_SUMMARY",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("workflow run release-publish.yml", recorded)


class RecordReleaseSchedulerSkipTest(unittest.TestCase):
    def _run(self, with_summary_file=True, overrides=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)

        env_overrides = {
            "RELEASE_COMMIT": _COMMIT,
            "GATE_TAG": _GATE_TAG,
            **(overrides or {}),
        }
        summary = tmp_dir / "step_summary.md"
        if with_summary_file:
            summary.touch()
            env_overrides["GITHUB_STEP_SUMMARY"] = str(summary)

        proc = subprocess.run(
            ["bash", str(_SKIP_SCRIPT)],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=env_overrides),
            cwd=str(tmp_dir),
        )
        return proc, (summary.read_text() if with_summary_file else "")

    def test_names_the_commit_and_tag_and_says_it_is_not_a_verdict(self):
        """A quiet tick leaves no pipeline run, so this text is the only trace."""
        proc, summary = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("### No GA release required", summary)
        self.assertIn(f"`{_GATE_TAG}`", summary)
        self.assertIn(f"`{_COMMIT[:7]}`", summary)
        self.assertIn(
            "says nothing about the last pipeline run's result", summary
        )

    def test_renders_custom_skip_reason_when_provided(self):
        reason = "No candidate has passed the gate — no 'staging_<ts>_<sha>' tag exists."
        proc, summary = self._run(overrides={"SKIP_REASON": reason})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("### No GA release required", summary)
        self.assertIn(reason, summary)
        self.assertIn(
            "says nothing about the last pipeline run's result", summary
        )

    def test_renders_fallback_when_no_tag_or_reason_provided(self):
        proc, summary = self._run(
            overrides={"GATE_TAG": "", "RELEASE_COMMIT": "", "SKIP_REASON": ""}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("### No GA release required", summary)
        self.assertIn(
            "No eligible staging promotion candidate exists to release", summary
        )
        self.assertIn(
            "says nothing about the last pipeline run's result", summary
        )

    def test_runs_outside_actions_without_a_summary_file(self):
        proc, _ = self._run(with_summary_file=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("### No GA release required", proc.stdout)

    def test_renders_tag_fallback_when_commit_is_omitted(self):
        proc, summary = self._run(
            overrides={"RELEASE_COMMIT": "", "SKIP_REASON": ""}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("### No GA release required", summary)
        self.assertIn(f"`{_GATE_TAG}`", summary)
        self.assertNotIn(" / ``", summary)
        self.assertIn(
            "says nothing about the last pipeline run's result", summary
        )


if __name__ == "__main__":
    unittest.main()
