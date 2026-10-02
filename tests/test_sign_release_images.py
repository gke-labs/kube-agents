"""Unit tests for scripts/release/sign_release_images.sh.

Tests argument validation, pure numeric SemVer enforcement, CLI detection
in CI vs local environments, and Cosign signing execution.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import create_minimal_tools_bin, create_mock_git_repo, get_isolated_test_env
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_CANDIDATE_RELEASE_IMAGES,
    MOCK_REQUIRED_RELEASE_IMAGES,
    MOCK_TARGET_RELEASE_TAG,
    commit_required_release_images,
    create_mock_cosign_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SIGN_RELEASE_IMAGES_SH = _REPO_ROOT / "scripts" / "release" / "sign_release_images.sh"


class SignReleaseImagesScriptTest(unittest.TestCase):
    def setUp(self):
        # A repository of its own: the script reads the release's image list at
        # the version's tag, and this checkout carries real tags by these names.
        self.repo_temp_dir, self.repo_dir, self.git = create_mock_git_repo()
        self.addCleanup(self.repo_temp_dir.cleanup)

    def _run_script(self, args, env=None, bin_dir=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", str(_SIGN_RELEASE_IMAGES_SH)] + args,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=self.repo_dir,
        )

    def test_missing_arguments(self):
        proc = self._run_script([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RELEASE_VERSION is required", proc.stderr)

    def test_invalid_tag_format(self):
        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_script([bad_tag])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_missing_cosign_in_ci(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = create_minimal_tools_bin(temp_dir.name, exclude=("cosign",))
            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true", "PATH": str(bin_dir)},
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("'cosign' CLI is mandatory in CI", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_missing_cosign_locally_warns(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = create_minimal_tools_bin(temp_dir.name, exclude=("cosign",))
            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"PATH": str(bin_dir)},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Skipping local image signing", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_local_dry_run_skips_signing(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_cosign_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: Cosign image signing", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_sign_execution(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_cosign_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("SIGNING RELEASE CONTAINER IMAGES", proc.stdout)
            # No tag by this name here, so the list is this checkout's, and it says so.
            self.assertIn(f"No tag '{MOCK_TARGET_RELEASE_TAG}' in this repository", proc.stderr)
            self.assertIn("no candidate commit named", proc.stderr)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Signed ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}", proc.stdout)
            self.assertIn(f"Successfully signed all {len(MOCK_REQUIRED_RELEASE_IMAGES)} container images", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_sign_execution_signs_the_releases_own_list(self):
        """The release's tag commit lists fewer images than this checkout; those,
        and only those, are signed (#2211)."""
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_cosign_binary(bin_dir)
            release_commit = commit_required_release_images(self.repo_dir, self.git, MOCK_CANDIDATE_RELEASE_IMAGES)
            self.git("tag", MOCK_TARGET_RELEASE_TAG, release_commit)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"lists at {release_commit[:7]}", proc.stderr)
            for img in MOCK_CANDIDATE_RELEASE_IMAGES:
                self.assertIn(f"Signed ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}", proc.stdout)
            for img in set(MOCK_REQUIRED_RELEASE_IMAGES) - set(MOCK_CANDIDATE_RELEASE_IMAGES):
                self.assertNotIn(f"/{img}:", proc.stdout, f"{img} is not in the release's list")
            self.assertIn(f"Successfully signed all {len(MOCK_CANDIDATE_RELEASE_IMAGES)} container images", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_sign_execution_env_vars(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_cosign_binary(bin_dir)

            proc = self._run_script(
                [],
                env={"CI": "true", "RELEASE_VERSION": MOCK_TARGET_RELEASE_TAG},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("SIGNING RELEASE CONTAINER IMAGES", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Signed ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
