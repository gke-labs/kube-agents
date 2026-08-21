"""Unit tests for scripts/release/publish_github_release.sh.

Tests argument validation, pure numeric SemVer enforcement, commit SHA resolution,
missing CLI handling in CI vs local environments, idempotent skip, and GitHub release creation.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import create_minimal_tools_bin, get_isolated_test_env
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_TARGET_RELEASE_TAG,
    create_mock_gh_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PUBLISH_GITHUB_RELEASE_SH = _REPO_ROOT / "scripts" / "release" / "publish_github_release.sh"


class PublishGithubReleaseScriptTest(unittest.TestCase):
    def _run_script(self, args, env=None, bin_dir=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", str(_PUBLISH_GITHUB_RELEASE_SH)] + args,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(_REPO_ROOT),
        )

    def test_missing_arguments(self):
        proc = self._run_script([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RELEASE_VERSION and RELEASE_COMMIT are required", proc.stderr)

    def test_invalid_tag_format(self):
        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_script([bad_tag, "HEAD"])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_invalid_commit_sha(self):
        proc = self._run_script([MOCK_TARGET_RELEASE_TAG, "invalid-sha-nonexistent-12345"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Cannot resolve valid Git commit", proc.stderr)

    def test_missing_gh_cli_in_ci(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = create_minimal_tools_bin(temp_dir.name, exclude=("gh",))
            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "HEAD"],
                env={"CI": "true", "PATH": str(bin_dir)},
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("'gh' CLI is mandatory in CI", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_idempotent_skip_when_release_exists(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_gh_binary(bin_dir, existing_releases=[MOCK_TARGET_RELEASE_TAG])

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "HEAD"],
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("already exists", proc.stdout)
            self.assertIn("Idempotent skip", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_local_dry_run_without_gh_token(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_gh_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "HEAD"],
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: GitHub release", proc.stdout)
            self.assertIn("creation skipped (runs only in CI)", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_local_dry_run_with_gh_token_set(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_gh_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "HEAD"],
                env={"GH_TOKEN": "mock-token-123"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: GitHub release", proc.stdout)
            self.assertIn("creation skipped (runs only in CI)", proc.stdout)
            gh_log = (bin_dir / "gh.log").read_text()
            self.assertNotIn("mock gh: release create", gh_log)
        finally:
            temp_dir.cleanup()

    def test_publish_execution_with_gh_token(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_gh_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "HEAD"],
                env={"CI": "true", "GH_TOKEN": "mock-token-123"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("PUBLISHING GITHUB RELEASE", proc.stdout)
            self.assertIn(f"Successfully published GitHub Release '{MOCK_TARGET_RELEASE_TAG}'", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_publish_execution_with_env_vars(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_gh_binary(bin_dir)

            proc = self._run_script(
                [],
                env={
                    "RELEASE_VERSION": MOCK_TARGET_RELEASE_TAG,
                    "RELEASE_COMMIT": "HEAD",
                    "CI": "true",
                    "GH_TOKEN": "mock-token-123",
                },
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("PUBLISHING GITHUB RELEASE", proc.stdout)
            self.assertIn(f"Successfully published GitHub Release '{MOCK_TARGET_RELEASE_TAG}'", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_swapped_arguments_symmetry(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_gh_binary(bin_dir)

            proc = self._run_script(
                ["HEAD", MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true", "GH_TOKEN": "mock-token-123"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("PUBLISHING GITHUB RELEASE", proc.stdout)
            self.assertIn(f"Successfully published GitHub Release '{MOCK_TARGET_RELEASE_TAG}'", proc.stdout)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
