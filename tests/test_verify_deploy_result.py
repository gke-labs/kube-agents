"""Unit tests for scripts/release/verify_deploy_result.sh."""

import os
import pathlib
import subprocess
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_VERIFY_SCRIPT = _REPO_ROOT / "scripts" / "release" / "verify_deploy_result.sh"


class VerifyDeployResultTest(unittest.TestCase):
    def _run(self, env_overrides=None, args=None):
        env = get_isolated_test_env(env_overrides or {})
        cmd = [str(_VERIFY_SCRIPT)] + (args or [])
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(_REPO_ROOT),
        )

    def test_applied_success_autopush(self):
        proc = self._run(env_overrides={
            "TARGET_ENVIRONMENT": "autopush",
            "DEPLOY_RESULT": "applied",
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("autopush deployment completed successfully (applied)", proc.stdout)

    def test_applied_success_staging(self):
        proc = self._run(env_overrides={
            "TARGET_ENVIRONMENT": "staging",
            "DEPLOY_RESULT": "applied",
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("staging deployment completed successfully (applied)", proc.stdout)

    def test_deferred_fails_staging(self):
        proc = self._run(env_overrides={
            "TARGET_ENVIRONMENT": "staging",
            "DEPLOY_RESULT": "deferred",
        })
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error", proc.stderr)
        self.assertIn("Deferrals are not permitted", proc.stderr)

    def test_deferred_fails_autopush(self):
        proc = self._run(env_overrides={
            "TARGET_ENVIRONMENT": "autopush",
            "DEPLOY_RESULT": "deferred",
        })
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error", proc.stderr)
        self.assertIn("deferred due to held live-test lease", proc.stderr)

    def test_failed_result_fails(self):
        proc = self._run(env_overrides={
            "TARGET_ENVIRONMENT": "staging",
            "DEPLOY_RESULT": "failed",
        })
        self.assertEqual(proc.returncode, 1)
        self.assertIn("staging deployment failed", proc.stderr)

    def test_unexpected_result_fails(self):
        proc = self._run(env_overrides={
            "TARGET_ENVIRONMENT": "staging",
            "DEPLOY_RESULT": "unknown_status",
        })
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Unexpected deployment result 'unknown_status'", proc.stderr)

    def test_missing_environment_fails(self):
        proc = self._run(env_overrides={
            "DEPLOY_RESULT": "applied",
        })
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Target environment (autopush or staging) must be specified", proc.stderr)

    def test_invalid_environment_fails(self):
        proc = self._run(env_overrides={
            "TARGET_ENVIRONMENT": "production",
            "DEPLOY_RESULT": "applied",
        })
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Invalid target environment: 'production'", proc.stderr)

    def test_missing_deploy_result_fails(self):
        proc = self._run(env_overrides={
            "TARGET_ENVIRONMENT": "staging",
        })
        self.assertEqual(proc.returncode, 1)
        self.assertIn("DEPLOY_RESULT is required", proc.stderr)

    def test_cli_arguments_supported(self):
        proc = self._run(args=["autopush", "applied"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("autopush deployment completed successfully", proc.stdout)


if __name__ == "__main__":
    unittest.main()
