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
        self.assertIn("RELEASE_VERSION and RELEASE_COMMIT are required", proc.stderr)

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
                env={"RELEASE_VERSION": MOCK_EXPLICIT_RELEASE_VERSION_NEXT, "RELEASE_COMMIT": head_commit},
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0)
            tag_commit = git("rev-parse", f"{MOCK_EXPLICIT_RELEASE_VERSION_NEXT}^{{commit}}").stdout.strip()
            self.assertEqual(tag_commit, head_commit)
        finally:
            temp_dir.cleanup()

    def test_swapped_args_symmetry(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script(
                [head_commit, MOCK_TARGET_RELEASE_TAG],
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0)
            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertEqual(tag_commit, head_commit)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
