"""Unit tests for scripts/release/sign_release_images.sh.

Tests argument validation, pure numeric SemVer enforcement, CLI detection
in CI vs local environments, and Cosign signing execution.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import create_minimal_tools_bin, get_isolated_test_env
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_REQUIRED_RELEASE_IMAGES,
    MOCK_TARGET_RELEASE_TAG,
    create_mock_cosign_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SIGN_RELEASE_IMAGES_SH = _REPO_ROOT / "scripts" / "release" / "sign_release_images.sh"


class SignReleaseImagesScriptTest(unittest.TestCase):
    def _run_script(self, args, env=None, bin_dir=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", str(_SIGN_RELEASE_IMAGES_SH)] + args,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(_REPO_ROOT),
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
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Signed ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}", proc.stdout)
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
