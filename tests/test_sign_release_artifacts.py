"""Unit tests for scripts/release/sign_release_artifacts.sh.

Tests argument validation, pure numeric SemVer enforcement, CLI detection
in CI vs local environments, checksums presence checks, and Cosign blob signing.
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
    create_mock_cosign_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SIGN_RELEASE_ARTIFACTS_SH = _REPO_ROOT / "scripts" / "release" / "sign_release_artifacts.sh"


class SignReleaseArtifactsScriptTest(unittest.TestCase):
    def _run_script(self, args, env=None, bin_dir=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", str(_SIGN_RELEASE_ARTIFACTS_SH)] + args,
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

    def test_missing_checksums_in_ci(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            dist_dir = temp_path / "dist"
            dist_dir.mkdir()
            bin_dir = temp_path / "bin"
            create_mock_cosign_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true", "DIST_DIR": str(dist_dir)},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Checksums file", proc.stderr)
            self.assertIn("not found", proc.stderr)

    def test_missing_checksums_locally_warns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            dist_dir = temp_path / "dist"
            dist_dir.mkdir()
            bin_dir = temp_path / "bin"
            create_mock_cosign_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"DIST_DIR": str(dist_dir)},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Skipping local artifact signing", proc.stdout)

    def test_missing_cosign_in_ci(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            dist_dir = temp_path / "dist"
            dist_dir.mkdir()
            (dist_dir / "checksums.txt").write_text("dummy-checksums")
            bin_dir = create_minimal_tools_bin(temp_dir, exclude=("cosign",))

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true", "PATH": str(bin_dir), "DIST_DIR": str(dist_dir)},
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("'cosign' CLI is mandatory in CI", proc.stderr)

    def test_missing_cosign_locally_warns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            dist_dir = temp_path / "dist"
            dist_dir.mkdir()
            (dist_dir / "checksums.txt").write_text("dummy-checksums")
            bin_dir = create_minimal_tools_bin(temp_dir, exclude=("cosign",))

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"PATH": str(bin_dir), "DIST_DIR": str(dist_dir)},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Skipping local artifact signing", proc.stderr)

    def test_local_dry_run_skips_signing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            dist_dir = temp_path / "dist"
            dist_dir.mkdir()
            (dist_dir / "checksums.txt").write_text("dummy-checksums")
            bin_dir = temp_path / "bin"
            create_mock_cosign_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"DIST_DIR": str(dist_dir)},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: Cosign artifact signing", proc.stdout)

    def test_sign_execution_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            dist_dir = temp_path / "dist"
            dist_dir.mkdir()
            (dist_dir / "checksums.txt").write_text("dummy-checksums")
            bin_dir = temp_path / "bin"
            create_mock_cosign_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true", "DIST_DIR": str(dist_dir)},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
            self.assertIn("SIGNING RELEASE ARTIFACTS", proc.stdout)
            self.assertIn("Successfully signed release checksums", proc.stdout)

            # Verify bundle was created
            bundle_file = dist_dir / "checksums.txt.bundle"
            self.assertTrue(bundle_file.exists(), "checksums.txt.bundle should exist after signing")

            # Verify cosign log
            cosign_log = (bin_dir / "cosign.log").read_text()
            self.assertIn("mock cosign: sign-blob --yes --bundle", cosign_log)
            self.assertIn(str(bundle_file), cosign_log)
            self.assertIn(str(dist_dir / "checksums.txt"), cosign_log)

    def test_sign_failure_in_ci(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            dist_dir = temp_path / "dist"
            dist_dir.mkdir()
            (dist_dir / "checksums.txt").write_text("dummy-checksums")
            bin_dir = temp_path / "bin"
            create_mock_cosign_binary(bin_dir, fail_sign=True)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true", "DIST_DIR": str(dist_dir)},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Failed to sign", proc.stderr)


if __name__ == "__main__":
    unittest.main()
