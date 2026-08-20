"""Unit tests for scripts/release/calculate_next_version.sh SemVer 2.0 engine.

Tests Conventional Commits parsing, SemVer 2.0 Clause 4 for 0.y.z,
precedence rules (breaking > feat > fix), and GitHub Actions outputs.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_BASE_TAG_1_X,
    MOCK_BASE_TAG_PRE_1_0,
    MOCK_COMMIT_MSG_BREAKING_1_X,
    MOCK_COMMIT_MSG_BREAKING_BODY,
    MOCK_COMMIT_MSG_BREAKING_PRE_1_0,
    MOCK_COMMIT_MSG_DOCS,
    MOCK_COMMIT_MSG_FEAT,
    MOCK_COMMIT_MSG_FIX,
    MOCK_INITIAL_VERSION,
    MOCK_NONEXISTENT_REF,
    MOCK_NONEXISTENT_TAG,
    MOCK_RC_VALIDATED_TAG,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CALC_SCRIPT = _REPO_ROOT / "scripts" / "release" / "calculate_next_version.sh"


class CalculateNextVersionTest(unittest.TestCase):
    def _create_mock_repo(self):
        """Creates a temporary git repository for testing version calculation."""
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        repo_dir = temp_dir.name

        def git(*args):
            return subprocess.run(
                ["git"] + list(args),
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=True,
            )

        git("init", "-b", "main")
        git("config", "user.name", "Test Bot")
        git("config", "user.email", "bot@example.com")
        git("config", "commit.gpgsign", "false")

        # Initial commit
        (pathlib.Path(repo_dir) / "README.md").write_text("Initial")
        git("add", "README.md")
        git("commit", "-m", "chore: initial commit")

        return temp_dir, repo_dir, git

    def _run_calc_script(self, repo_dir, args=None, env=None):
        full_env = get_isolated_test_env(overrides=env)
        return subprocess.run(
            ["bash", str(_CALC_SCRIPT)] + (args or []),
            cwd=repo_dir,
            capture_output=True,
            text=True,
            env=full_env,
        )

    def test_baseline_initialization_when_no_tags(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            proc = self._run_calc_script(repo_dir, env={"GITHUB_OUTPUT": str(gh_out)})
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), MOCK_INITIAL_VERSION)

            outputs = gh_out.read_text()
            self.assertIn(f"version={MOCK_INITIAL_VERSION}", outputs)
            self.assertIn("has_changes=true", outputs)
            self.assertIn("bump_type=initial", outputs)
        finally:
            temp_dir.cleanup()

    def test_no_new_commits_keeps_version(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_INITIAL_VERSION, "-m", f"Release {MOCK_INITIAL_VERSION}")
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            proc = self._run_calc_script(repo_dir, env={"GITHUB_OUTPUT": str(gh_out)})
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), MOCK_INITIAL_VERSION)

            outputs = gh_out.read_text()
            self.assertIn(f"version={MOCK_INITIAL_VERSION}", outputs)
            self.assertIn("has_changes=false", outputs)
            self.assertIn("bump_type=none", outputs)
        finally:
            temp_dir.cleanup()

    def test_patch_bump_for_fix_and_chore(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_INITIAL_VERSION, "-m", f"Release {MOCK_INITIAL_VERSION}")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("fix")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_FIX)

            (pathlib.Path(repo_dir) / "file2.txt").write_text("docs")
            git("add", "file2.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_DOCS)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.1.1")
        finally:
            temp_dir.cleanup()

    def test_minor_bump_for_feat_in_pre_1_0(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_BASE_TAG_PRE_1_0, "-m", f"Release {MOCK_BASE_TAG_PRE_1_0}")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("feat")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_FEAT)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.2.0")
        finally:
            temp_dir.cleanup()

    def test_breaking_change_in_pre_1_0_semver_clause_4(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_BASE_TAG_PRE_1_0, "-m", f"Release {MOCK_BASE_TAG_PRE_1_0}")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("breaking")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_BREAKING_PRE_1_0)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.2.0")
        finally:
            temp_dir.cleanup()

    def test_breaking_change_in_body_footer(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", "0.2.0", "-m", "Release 0.2.0")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("breaking body")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_BREAKING_BODY)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.3.0")
        finally:
            temp_dir.cleanup()

    def test_breaking_change_in_1_x_x_bumps_major(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_BASE_TAG_1_X, "-m", f"Release {MOCK_BASE_TAG_1_X}")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("major")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_BREAKING_1_X)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "2.0.0")
        finally:
            temp_dir.cleanup()

    def test_feat_in_1_x_x_bumps_minor(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_BASE_TAG_1_X, "-m", f"Release {MOCK_BASE_TAG_1_X}")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("feat")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_FEAT)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "1.3.0")
        finally:
            temp_dir.cleanup()

    def test_ignores_rc_and_non_semver_tags(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_INITIAL_VERSION, "-m", f"Release {MOCK_INITIAL_VERSION}")
            git("tag", "-a", MOCK_RC_VALIDATED_TAG, "-m", "RC tag")
            git("tag", "-a", "random-tag", "-m", "Non semver")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("fix")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_FIX)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            # Should detect 0.1.0 as baseline and calculate 0.1.1
            self.assertEqual(proc.stdout.strip(), "0.1.1")
        finally:
            temp_dir.cleanup()

    def test_invalid_base_tag_format_fails(self):
        temp_dir, repo_dir, _ = self._create_mock_repo()
        try:
            for bad_tag in INVALID_GA_RELEASE_TAGS:
                with self.subTest(bad_tag=bad_tag):
                    proc = self._run_calc_script(repo_dir, args=[bad_tag])
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn("not a valid pure numeric SemVer", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_nonexistent_base_tag_fails(self):
        temp_dir, repo_dir, _ = self._create_mock_repo()
        try:
            # Base tag does not exist in repo -> must fail fast with error
            proc = self._run_calc_script(repo_dir, args=[MOCK_NONEXISTENT_TAG])
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does not exist in git repository", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_nonexistent_target_ref_fails(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_INITIAL_VERSION, "-m", f"Release {MOCK_INITIAL_VERSION}")
            # Target ref does not exist -> must fail fast
            proc = self._run_calc_script(repo_dir, args=[MOCK_INITIAL_VERSION, MOCK_NONEXISTENT_REF])
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(f"Target ref '{MOCK_NONEXISTENT_REF}' does not exist", proc.stderr)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
