"""Unit tests for scripts/release/dispatch_release_scheduler.sh.

Step 6b of staging-promotion-pipeline.yml runs it right after the run pushed
its staging tag, so a promotion that finishes after the GA scheduler's daily
tick still ships the same day. These pin that it starts the scheduler and
nothing else, passes no inputs (the scheduler declares none), says in an
annotation what a failed dispatch costs, and refuses to run on a missing input.
"""

import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import create_minimal_tools_bin, get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DISPATCH_SCRIPT = _REPO_ROOT / "scripts" / "release" / "dispatch_release_scheduler.sh"
_STAGING_TAG = "staging_2611172334_ac3e399"
_CALLS_LOG = "gh_calls.log"


class DispatchReleaseSchedulerTest(unittest.TestCase):
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
            "STAGING_TAG": _STAGING_TAG,
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

    def test_dispatches_the_scheduler_with_no_inputs(self):
        """The scheduler's workflow_dispatch declares no inputs; gh refuses one it does not."""
        proc, recorded, _ = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("workflow run release-scheduler.yml", recorded)
        self.assertNotIn(" -f ", recorded)
        self.assertNotIn("release-publish.yml", recorded)

    def test_passes_the_promotions_own_ref_through(self):
        """The script does not pick a ref; the job that runs it is gated to main.

        release-scheduler.yml evaluates on main only, so step 6b runs only there
        (test_promotion_pipeline_wiring.py pins the guard). The script carries
        whatever ref it is given, as the sibling dispatchers do.
        """
        _, recorded, _ = self._run(overrides={"GITHUB_REF_NAME": "main"})
        self.assertIn("--ref main", recorded)
        self.assertIn("--repo gke-labs/kube-agents", recorded)

    def test_records_the_dispatch_in_the_job_summary(self):
        _, _, summary = self._run()
        self.assertIn("### GA release scheduler dispatched", summary)
        self.assertIn(f"| Gate tag | `{_STAGING_TAG}` |", summary)
        self.assertIn("The scheduler decides whether this promotion publishes", summary)

    def test_a_failed_dispatch_says_what_it_costs(self):
        """A day's delay, not a missed release: the annotation says both."""
        proc, _, summary = self._run(gh_exit=1)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error title=GA release scheduler dispatch failed", proc.stderr)
        self.assertIn("ships a day late", proc.stderr)
        self.assertIn(_STAGING_TAG, proc.stderr)
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

    def test_dispatches_cleanly_without_a_staging_tag(self):
        """The tag is for the messages only; the dispatch does not depend on it."""
        proc, recorded, summary = self._run(overrides={"STAGING_TAG": ""})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("workflow run release-scheduler.yml", recorded)
        self.assertNotIn("| Gate tag |", summary)

    def test_missing_gh_cli_aborts_with_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)
        bin_dir = create_minimal_tools_bin(tmp_dir)
        env = get_isolated_test_env(
            overrides={
                "PATH": str(bin_dir),
                "STAGING_TAG": _STAGING_TAG,
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


if __name__ == "__main__":
    unittest.main()
