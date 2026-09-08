"""Unit tests for resolve_deploy.sh."""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from tests.test_release_common import create_mock_git_repo, get_isolated_test_env

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_RESOLVE_DEPLOY = _ROOT / "scripts" / "release" / "resolve_deploy.sh"


class ResolveAutopushDeployTest(unittest.TestCase):
    def _run(self, env_overrides):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = pathlib.Path(tmp) / "github_output"
            out_file.touch()
            env = get_isolated_test_env({
                "GITHUB_OUTPUT": str(out_file),
                "TARGET_ENVIRONMENT": "autopush",
                **env_overrides,
            })
            proc = subprocess.run(
                [str(_RESOLVE_DEPLOY)],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(_ROOT),
            )
            output_content = out_file.read_text()
            return proc, output_content

    def test_explicit_input_tag_resolves(self):
        proc, out = self._run({
            "INPUT_TAG": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("commit_sha=a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2", out)
        self.assertIn("lease_policy=fail", out)

    def test_workflow_run_resolves_head_sha(self):
        proc, out = self._run({
            "EVENT_NAME": "workflow_run",
            "RUN_HEAD_SHA": "1234567890abcdef1234567890abcdef12345678",
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("commit_sha=1234567890abcdef1234567890abcdef12345678", out)

    def test_workflow_dispatch_falls_back_to_github_sha(self):
        proc, out = self._run({
            "EVENT_NAME": "workflow_dispatch",
            "GITHUB_SHA": "abcdefabcdefabcdefabcdefabcdefabcdefabcd",
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("commit_sha=abcdefabcdefabcdefabcdefabcdefabcdefabcd", out)

    def test_custom_lease_policy(self):
        proc, out = self._run({
            "INPUT_TAG": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
            "INPUT_LEASE_POLICY": "defer",
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lease_policy=defer", out)

    def test_invalid_lease_policy_fails(self):
        proc, _ = self._run({
            "INPUT_TAG": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
            "INPUT_LEASE_POLICY": "invalid_policy",
        })
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Invalid lease_policy", proc.stderr)

    def test_missing_sha_fails(self):
        proc, _ = self._run({})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Unable to determine commit SHA", proc.stderr)


class ResolveStagingDeployTest(unittest.TestCase):
    def _create_git_repo(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        head = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", "staging_2608241820_b35543c", "-m", "staging promotion")

        # Copy the whole scripts directory so internal sources resolve cleanly
        shutil.copytree(_ROOT / "scripts", pathlib.Path(repo_dir) / "scripts", dirs_exist_ok=True)
        return pathlib.Path(repo_dir), head

    def test_resolves_annotated_staging_tag_to_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir, head = self._create_git_repo()
            out_file = pathlib.Path(tmp) / "github_output"
            out_file.touch()
            env = get_isolated_test_env({
                "GITHUB_OUTPUT": str(out_file),
                "TARGET_ENVIRONMENT": "staging",
                "INPUT_TAG": "staging_2608241820_b35543c",
            })

            script = repo_dir / "scripts" / "release" / "resolve_deploy.sh"
            proc = subprocess.run(
                [str(script)],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = out_file.read_text()
            self.assertIn(f"commit_sha={head}", out)
            self.assertIn("lease_policy=fail", out)

    def test_missing_tag_and_sha_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir, _ = self._create_git_repo()
            out_file = pathlib.Path(tmp) / "github_output"
            out_file.touch()
            env = get_isolated_test_env({
                "GITHUB_OUTPUT": str(out_file),
                "TARGET_ENVIRONMENT": "staging",
            })

            # delete the tag
            subprocess.run(["git", "tag", "-d", "staging_2608241820_b35543c"], cwd=repo_dir, check=True, env=env)

            script = repo_dir / "scripts" / "release" / "resolve_deploy.sh"
            proc = subprocess.run(
                [str(script)],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("No staging tag found", proc.stderr)

    def test_missing_or_invalid_env_arg_fails(self):
        proc = subprocess.run(
            [str(_RESOLVE_DEPLOY)],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
            env=get_isolated_test_env(),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Target environment (autopush or staging) must be specified", proc.stderr)

        proc = subprocess.run(
            [str(_RESOLVE_DEPLOY)],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
            env=get_isolated_test_env({"TARGET_ENVIRONMENT": "unknown_env"}),
        )
        self.assertIn("Invalid target environment", proc.stderr)


if __name__ == "__main__":
    unittest.main()
