"""Unit tests for scripts/release/tag_ga_release.sh.

Tests argument validation, pure numeric SemVer enforcement, Git tag creation,
and idempotency on mock repositories.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_SAMPLE_COMMIT_SHA,
    VALID_GA_RELEASE_TAGS,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_EXPLICIT_RELEASE_VERSION_NEXT,
    MOCK_TARGET_RELEASE_TAG,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TAG_GA_RELEASE_SH = _REPO_ROOT / "scripts" / "release" / "tag_ga_release.sh"


class TagGAReleaseScriptTest(unittest.TestCase):
    def _run_script(self, args, env=None, cwd=None):
        full_env = get_isolated_test_env(overrides=env)
        return subprocess.run(
            ["bash", str(_TAG_GA_RELEASE_SH)] + args,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=cwd or str(_REPO_ROOT),
        )

    def test_missing_arguments(self):
        proc = self._run_script([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RELEASE_VERSION and RC candidate commit are required", proc.stderr)

    def test_invalid_tag_format(self):
        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_script([bad_tag, MOCK_SAMPLE_COMMIT_SHA])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_tag_creation_and_idempotency(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head_commit = git("rev-parse", "HEAD").stdout.strip()

            # First tag creation
            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, head_commit], cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("CREATING AND PUSHING GA RELEASE GIT TAG", proc.stdout)

            # Verify tag exists in repo
            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertEqual(tag_commit, head_commit)

            # Second execution: Idempotent skip
            proc2 = self._run_script([MOCK_TARGET_RELEASE_TAG, head_commit], cwd=repo_dir)
            self.assertEqual(proc2.returncode, 0)
            self.assertIn("Idempotent skip", proc2.stdout)
        finally:
            temp_dir.cleanup()

    def test_env_vars_invocation_without_args(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script(
                [],
                env={"RELEASE_VERSION": MOCK_EXPLICIT_RELEASE_VERSION_NEXT, "RC_CANDIDATE_COMMIT": head_commit},
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            tag_commit = git("rev-parse", f"{MOCK_EXPLICIT_RELEASE_VERSION_NEXT}^{{commit}}").stdout.strip()
            self.assertEqual(tag_commit, head_commit)
        finally:
            temp_dir.cleanup()

    def test_strict_argument_order_rejects_swapped_args(self):
        """Verifies tag_ga_release.sh strictly requires SemVer as first argument."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script(
                [head_commit, MOCK_TARGET_RELEASE_TAG],
                cwd=repo_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("not a valid pure numeric SemVer", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_stamps_baked_release_version_on_detached_head(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            # Create root installer script with empty baked version placeholder
            install_sh = pathlib.Path(repo_dir) / "install.sh"
            install_sh.write_text('#!/bin/bash\nBAKED_RELEASE_VERSION=""\necho "tag=$BAKED_RELEASE_VERSION"\n')
            git("add", "install.sh")
            git("commit", "-m", "feat: add installer")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, main_commit],
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            # 1. Main branch is untouched (still points to main_commit and HEAD remains on main)
            current_main = git("rev-parse", "main").stdout.strip()
            self.assertEqual(current_main, main_commit)
            current_branch = git("symbolic-ref", "--short", "HEAD").stdout.strip()
            self.assertEqual(current_branch, "main")

            # 2. Release tag exists and points to stamped commit (different from main)
            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertNotEqual(tag_commit, main_commit)

            # 3. Content at tag has BAKED_RELEASE_VERSION stamped with release tag
            tag_install_content = git("show", f"{MOCK_TARGET_RELEASE_TAG}:install.sh").stdout
            self.assertIn(f'BAKED_RELEASE_VERSION="{MOCK_TARGET_RELEASE_TAG}"', tag_install_content)
        finally:
            temp_dir.cleanup()

    def test_fails_loudly_if_candidate_commit_unresolvable(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            main_commit = git("rev-parse", "HEAD").stdout.strip()
            # Pass a nonexistent SHA as candidate commit
            bad_sha = "0123456789abcdef0123456789abcdef01234567"
            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, bad_sha], cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to checkout candidate commit", proc.stderr)

            # Ensure main branch is untouched and no tag was created
            current_main = git("rev-parse", "main").stdout.strip()
            self.assertEqual(current_main, main_commit)
            tag_check = git("tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip()
            self.assertEqual(tag_check, "")
        finally:
            temp_dir.cleanup()

    def test_fails_loudly_when_installer_lacks_baked_version_placeholder(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            # Create installer script WITHOUT BAKED_RELEASE_VERSION placeholder
            install_sh = pathlib.Path(repo_dir) / "install.sh"
            install_sh.write_text('#!/bin/bash\necho "no baked placeholder here"\n')
            git("add", "install.sh")
            git("commit", "-m", "feat: legacy installer without placeholder")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to stamp BAKED_RELEASE_VERSION in install.sh", proc.stderr)

            # Ensure no tag was created
            tag_check = git("tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip()
            self.assertEqual(tag_check, "")
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
