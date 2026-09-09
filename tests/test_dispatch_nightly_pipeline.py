"""Unit tests for scripts/release/dispatch_nightly_pipeline.sh and its skip counterpart.

nightly-scheduler.yml is now the scheduled trigger that starts the nightly
promotion pipeline, so a dispatch that fails quietly means no candidate is tested
for staging promotion until somebody notices. These pin the annotation that says so,
the arguments that decide which candidate tag gets tested, and the refusal to run on
a missing input.
"""

import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import create_minimal_tools_bin, get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DISPATCH_SCRIPT = _REPO_ROOT / "scripts" / "release" / "dispatch_nightly_pipeline.sh"
_SKIP_SCRIPT = _REPO_ROOT / "scripts" / "release" / "record_nightly_scheduler_skip.sh"

_COMMIT = "1234567890abcdef1234567890abcdef12345678"
_RC_TAG = "rc_20260830_120000_1234567_validated"
_CALLS_LOG = "gh_calls.log"


class DispatchNightlyPipelineTest(unittest.TestCase):
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
            "COMMIT_SHA": _COMMIT,
            "RC_TAG": _RC_TAG,
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
            env=get_isolated_test_env(overrides=env_overrides, bin_dir=str(bin_dir)),
            cwd=str(tmp_dir),
        )
        recorded = calls.read_text() if calls.exists() else ""
        return proc, recorded, summary.read_text()

    def test_dispatches_the_pipeline_with_the_resolved_candidate(self):
        proc, recorded, _ = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("workflow run nightly-pipeline.yml", recorded)
        self.assertIn(f"rc_tag={_RC_TAG}", recorded)

    def test_dispatches_against_the_ref_the_scheduler_ran_on(self):
        _, recorded, _ = self._run(overrides={"GITHUB_REF_NAME": "release-1.2"})
        self.assertIn("--ref release-1.2", recorded)
        self.assertIn("--repo gke-labs/kube-agents", recorded)

    def test_records_the_dispatch_in_the_job_summary(self):
        _, _, summary = self._run()
        self.assertIn("### Nightly pipeline dispatched", summary)
        self.assertIn(f"| Commit | `{_COMMIT}` |", summary)
        self.assertIn(f"| Candidate tag | `{_RC_TAG}` |", summary)

    def test_a_failed_dispatch_is_an_error_annotation_not_a_bare_exit(self):
        """This failure means no nightly candidate is being tested; it must say so."""
        proc, _, summary = self._run(gh_exit=1)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error title=Nightly pipeline dispatch failed", proc.stderr)
        self.assertIn("No nightly candidate is being tested", proc.stderr)
        self.assertNotIn("dispatched", summary)

    def test_missing_commit_sha_aborts_before_calling_gh(self):
        proc, recorded, _ = self._run(omit=("COMMIT_SHA",))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(recorded, "")
        self.assertIn("COMMIT_SHA", proc.stderr)

    def test_missing_rc_tag_aborts_before_calling_gh(self):
        proc, recorded, _ = self._run(omit=("RC_TAG",))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(recorded, "")
        self.assertIn("RC_TAG", proc.stderr)

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
                "COMMIT_SHA": _COMMIT,
                "RC_TAG": _RC_TAG,
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
        self.assertIn("gh CLI is required", proc.stderr)

    def test_dispatches_with_overridden_workflow_file(self):
        proc, recorded, _ = self._run(overrides={"WORKFLOW_FILE": "custom-nightly.yml"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("workflow run custom-nightly.yml", recorded)

    def test_runs_outside_actions_without_step_summary_file(self):
        proc, recorded, _ = self._run(omit=("GITHUB_STEP_SUMMARY",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("workflow run nightly-pipeline.yml", recorded)


class RecordNightlySchedulerSkipTest(unittest.TestCase):
    def _run(self, with_summary_file=True, overrides=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)

        env_overrides = {"COMMIT_SHA": _COMMIT, "RC_TAG": _RC_TAG, **(overrides or {})}
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
        self.assertIn("### No nightly promotion required", summary)
        self.assertIn(f"`{_RC_TAG}`", summary)
        self.assertIn(f"`{_COMMIT}`", summary)
        self.assertIn("says nothing about the last pipeline run's result", summary)

    def test_renders_custom_skip_reason_when_provided(self):
        reason = "Commit is already promoted as 'staging_20260830_120000_1234567'."
        proc, summary = self._run(overrides={"SKIP_REASON": reason})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("### No nightly promotion required", summary)
        self.assertIn(reason, summary)
        self.assertIn("says nothing about the last pipeline run's result", summary)

    def test_renders_fallback_when_no_tag_or_reason_provided(self):
        proc, summary = self._run(overrides={"RC_TAG": "", "COMMIT_SHA": "", "SKIP_REASON": ""})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("### No nightly promotion required", summary)
        self.assertIn("No eligible validated candidate exists to promote", summary)
        self.assertIn("says nothing about the last pipeline run's result", summary)

    def test_runs_outside_actions_without_a_summary_file(self):
        proc, _ = self._run(with_summary_file=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("### No nightly promotion required", proc.stdout)


if __name__ == "__main__":
    unittest.main()
