"""Unit tests for scripts/release/generate_release_sbom.sh.

Tests CLI arguments validation, syft presence checks in CI vs local,
SPDX 2.3 and CycloneDX 1.5 JSON filesystem SBOM generation, and OCI image SBOM generation.
"""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_DEFAULT_REGISTRY_PREFIX,
    create_minimal_tools_bin,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_RELEASE_BUNDLE_VERSION,
    MOCK_REQUIRED_RELEASE_IMAGES,
    MOCK_TARGET_RELEASE_VERSION,
    create_mock_syft_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SBOM_SCRIPT = _REPO_ROOT / "scripts" / "release" / "generate_release_sbom.sh"


class GenerateReleaseSbomTest(unittest.TestCase):
    def _run_script(self, args=None, env=None, bin_dir=None, cwd=None):
        cmd = ["bash", str(_SBOM_SCRIPT)] + (args or [])
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=cwd or str(_REPO_ROOT),
        )

    def test_missing_tag_fails(self):
        proc = self._run_script([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("TAG_NAME must be specified", proc.stderr)

    def test_invalid_semver_fails(self):
        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_script([bad_tag])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_nonexistent_target_dir_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = pathlib.Path(temp_dir) / "bin"
            create_mock_syft_binary(bin_dir)
            nonexistent = pathlib.Path(temp_dir) / "does-not-exist"

            proc = self._run_script([MOCK_TARGET_RELEASE_VERSION, str(nonexistent)], bin_dir=bin_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Target directory", proc.stderr)

    def test_syft_missing_in_ci_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = create_minimal_tools_bin(temp_dir, exclude=("syft",))
            proc = self._run_script(
                [MOCK_TARGET_RELEASE_VERSION],
                env={"CI": "true", "PATH": str(bin_dir)},
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("syft' CLI is mandatory in CI", proc.stderr)

    def test_syft_missing_off_ci_warns_and_succeeds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = create_minimal_tools_bin(temp_dir, exclude=("syft",))
            proc = self._run_script(
                [MOCK_TARGET_RELEASE_VERSION],
                env={"CI": "", "GITHUB_ACTIONS": "", "PATH": str(bin_dir)},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Skipping local SBOM generation", proc.stderr)

    def test_successful_sbom_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            bin_dir = temp_path / "bin"
            dist_dir = temp_path / "dist"
            target_dir = temp_path / "stage"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "sample.txt").write_text("sample content")

            create_mock_syft_binary(bin_dir)

            proc = self._run_script(
                [MOCK_RELEASE_BUNDLE_VERSION, str(target_dir)],
                env={
                    "DIST_DIR": str(dist_dir),
                    "CI": "true",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                },
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            # Verify filesystem SBOM files
            spdx_fs = dist_dir / f"kube-agents-{MOCK_RELEASE_BUNDLE_VERSION}.spdx.json"
            cdx_fs = dist_dir / f"kube-agents-{MOCK_RELEASE_BUNDLE_VERSION}.cdx.json"
            self.assertTrue(spdx_fs.exists(), "SPDX filesystem SBOM should exist")
            self.assertTrue(cdx_fs.exists(), "CycloneDX filesystem SBOM should exist")

            spdx_data = json.loads(spdx_fs.read_text())
            self.assertEqual(spdx_data.get("spdxVersion"), "SPDX-2.3")

            cdx_data = json.loads(cdx_fs.read_text())
            self.assertEqual(cdx_data.get("bomFormat"), "CycloneDX")

            # Verify container image SBOM files
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                img_sbom = dist_dir / f"{img}-{MOCK_RELEASE_BUNDLE_VERSION}.spdx.json"
                self.assertTrue(img_sbom.exists(), f"Image SBOM for {img} should exist")
                img_data = json.loads(img_sbom.read_text())
                self.assertEqual(img_data.get("spdxVersion"), "SPDX-2.3")

    def test_image_sbom_failure_in_ci_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            bin_dir = temp_path / "bin"
            dist_dir = temp_path / "dist"
            target_dir = temp_path / "stage"
            target_dir.mkdir(parents=True, exist_ok=True)

            failing_img = MOCK_REQUIRED_RELEASE_IMAGES[0]
            create_mock_syft_binary(bin_dir, fail_on_images=[failing_img])

            proc = self._run_script(
                [MOCK_RELEASE_BUNDLE_VERSION, str(target_dir)],
                env={
                    "DIST_DIR": str(dist_dir),
                    "CI": "true",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                },
                bin_dir=bin_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to generate SBOM for container image", proc.stderr)

    def test_image_sbom_failure_off_ci_warns_and_continues(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            bin_dir = temp_path / "bin"
            dist_dir = temp_path / "dist"
            target_dir = temp_path / "stage"
            target_dir.mkdir(parents=True, exist_ok=True)

            failing_img = MOCK_REQUIRED_RELEASE_IMAGES[0]
            create_mock_syft_binary(bin_dir, fail_on_images=[failing_img])

            proc = self._run_script(
                [MOCK_RELEASE_BUNDLE_VERSION, str(target_dir)],
                env={
                    "DIST_DIR": str(dist_dir),
                    "CI": "",
                    "GITHUB_ACTIONS": "",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                },
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Could not generate remote image SBOM", proc.stdout + proc.stderr)

    def test_image_failure_in_ci_leaves_dist_clean(self):
        """Verifies that an image generation failure in CI does not publish partial artifacts to dist_dir."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            bin_dir = temp_path / "bin"
            dist_dir = temp_path / "dist"
            target_dir = temp_path / "stage"
            target_dir.mkdir(parents=True, exist_ok=True)

            failing_img = MOCK_REQUIRED_RELEASE_IMAGES[0]
            create_mock_syft_binary(bin_dir, fail_on_images=[failing_img])

            proc = self._run_script(
                [MOCK_RELEASE_BUNDLE_VERSION, str(target_dir)],
                env={
                    "DIST_DIR": str(dist_dir),
                    "CI": "true",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                },
                bin_dir=bin_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            if dist_dir.exists():
                json_files = list(dist_dir.glob("*.json"))
                self.assertEqual(len(json_files), 0, f"Expected 0 files on failure, found: {json_files}")

    def test_idempotent_rerun(self):
        """Verifies running the script multiple times succeeds cleanly and produces identical valid artifacts."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            bin_dir = temp_path / "bin"
            dist_dir = temp_path / "dist"
            target_dir = temp_path / "stage"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "app.txt").write_text(f"v{MOCK_RELEASE_BUNDLE_VERSION} content")

            create_mock_syft_binary(bin_dir)

            # First run
            proc1 = self._run_script(
                [MOCK_RELEASE_BUNDLE_VERSION, str(target_dir)],
                env={
                    "DIST_DIR": str(dist_dir),
                    "CI": "true",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                },
                bin_dir=bin_dir,
            )
            self.assertEqual(proc1.returncode, 0, proc1.stderr)
            files_run1 = sorted([f.name for f in dist_dir.glob("*.json")])
            self.assertGreater(len(files_run1), 0)

            # Second run (idempotent overwrite)
            proc2 = self._run_script(
                [MOCK_RELEASE_BUNDLE_VERSION, str(target_dir)],
                env={
                    "DIST_DIR": str(dist_dir),
                    "CI": "true",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                },
                bin_dir=bin_dir,
            )
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            files_run2 = sorted([f.name for f in dist_dir.glob("*.json")])
            self.assertEqual(files_run1, files_run2)

    def _create_mock_swap_tools(
        self,
        bin_dir,
        log_file,
        initial_swap_mb=0,
        fail_fallocate=False,
        fail_sudo=False,
    ):
        """Hermetically creates mock sudo, free, fallocate, mkswap, swapon, swapoff in bin_dir."""
        bin_path = pathlib.Path(bin_dir)
        bin_path.mkdir(parents=True, exist_ok=True)

        sudo_path = bin_path / "sudo"
        sudo_code = f"""#!/bin/sh
echo "mock sudo: $@" >> "{log_file}"
if [ "{fail_sudo}" = "True" ]; then
  exit 1
fi
if [ "$1" = "-n" ]; then
  shift
fi
if [ "$1" = "true" ]; then
  exit 0
fi
"$@"
"""
        sudo_path.write_text(sudo_code)
        sudo_path.chmod(0o755)

        free_path = bin_path / "free"
        free_code = f"""#!/bin/sh
echo "Swap:        {initial_swap_mb}          0          {initial_swap_mb}"
"""
        free_path.write_text(free_code)
        free_path.chmod(0o755)

        fallocate_path = bin_path / "fallocate"
        fallocate_exit = "echo 'mock fallocate failure' >&2; exit 1" if fail_fallocate else 'touch "$3"; exit 0'
        fallocate_code = f"""#!/bin/sh
echo "mock fallocate: $@" >> "{log_file}"
{fallocate_exit}
"""
        fallocate_path.write_text(fallocate_code)
        fallocate_path.chmod(0o755)

        for tool in ("mkswap", "swapon", "swapoff"):
            t_path = bin_path / tool
            t_path.write_text(f"""#!/bin/sh
echo "mock {tool}: $@" >> "{log_file}"
exit 0
""")
            t_path.chmod(0o755)

    def test_syft_squashed_scope_and_resource_limits(self):
        """Verifies that Syft is called with --scope squashed for images and exports resource limits."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            bin_dir = temp_path / "bin"
            dist_dir = temp_path / "dist"
            target_dir = temp_path / "stage"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "app.txt").write_text("sample content")

            syft_log = temp_path / "syft.log"
            create_mock_syft_binary(bin_dir, log_file=syft_log)

            proc = self._run_script(
                [MOCK_RELEASE_BUNDLE_VERSION, str(target_dir)],
                env={
                    "DIST_DIR": str(dist_dir),
                    "CI": "true",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                    "SYFT_PARALLELISM": "3",
                    "GOMAXPROCS": "3",
                    "GOMEMLIMIT": "3GiB",
                },
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            # Check syft log to ensure --scope squashed was passed for every image
            syft_calls = syft_log.read_text().splitlines()
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                matching = [c for c in syft_calls if f"{img}:{MOCK_RELEASE_BUNDLE_VERSION}" in c]
                self.assertTrue(matching, f"Expected syft call for image {img}")
                self.assertIn("--scope squashed", matching[0], f"Image {img} must use --scope squashed")

    def test_ci_swap_allocation_and_trap_cleanup(self):
        """Verifies that Linux CI dynamically allocates swap and cleans it up reliably via EXIT trap."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            bin_dir = temp_path / "bin"
            dist_dir = temp_path / "dist"
            target_dir = temp_path / "stage"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "app.txt").write_text("sample content")

            swap_file = temp_path / "mock_swapfile"
            swap_log = temp_path / "swap.log"

            create_mock_syft_binary(bin_dir)
            self._create_mock_swap_tools(bin_dir, swap_log, initial_swap_mb=512)

            proc = self._run_script(
                [MOCK_RELEASE_BUNDLE_VERSION, str(target_dir)],
                env={
                    "DIST_DIR": str(dist_dir),
                    "CI": "true",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                    "SWAP_FILE_PATH": str(swap_file),
                },
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Configuring temporary 10G swap space in CI", proc.stdout)
            self.assertIn("Disabling and removing temporary CI swap space...", proc.stdout)

            # Verify the sequence of calls logged
            log_content = swap_log.read_text()
            self.assertIn(f"mock fallocate: -l 10G {swap_file}", log_content)
            self.assertIn(f"mock mkswap: {swap_file}", log_content)
            self.assertIn(f"mock swapon: {swap_file}", log_content)
            self.assertIn(f"mock swapoff: {swap_file}", log_content)
            # Verify file was cleaned up on exit
            self.assertFalse(swap_file.exists(), "Swap file should be removed on exit")

    def test_ci_swap_allocation_failure_warns_and_continues(self):
        """Verifies that failure during swap allocation logs a warning and allows SBOM generation to continue."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            bin_dir = temp_path / "bin"
            dist_dir = temp_path / "dist"
            target_dir = temp_path / "stage"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "app.txt").write_text("sample content")

            swap_file = temp_path / "mock_swapfile"
            swap_log = temp_path / "swap.log"

            create_mock_syft_binary(bin_dir)
            self._create_mock_swap_tools(bin_dir, swap_log, initial_swap_mb=512, fail_fallocate=True)

            proc = self._run_script(
                [MOCK_RELEASE_BUNDLE_VERSION, str(target_dir)],
                env={
                    "DIST_DIR": str(dist_dir),
                    "CI": "true",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                    "SWAP_FILE_PATH": str(swap_file),
                },
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Warning: Failed to configure swap space", proc.stderr)
            self.assertNotIn("Disabling and removing temporary CI swap space...", proc.stdout)

    def test_ci_swap_skipped_when_sufficient_swap(self):
        """Verifies that CI does not attempt swap creation when system already has >= 4096MB swap."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            bin_dir = temp_path / "bin"
            dist_dir = temp_path / "dist"
            target_dir = temp_path / "stage"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "app.txt").write_text("sample content")

            swap_file = temp_path / "mock_swapfile"
            swap_log = temp_path / "swap.log"

            create_mock_syft_binary(bin_dir)
            self._create_mock_swap_tools(bin_dir, swap_log, initial_swap_mb=8192)

            proc = self._run_script(
                [MOCK_RELEASE_BUNDLE_VERSION, str(target_dir)],
                env={
                    "DIST_DIR": str(dist_dir),
                    "CI": "true",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                    "SWAP_FILE_PATH": str(swap_file),
                },
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("Configuring temporary 10G swap space", proc.stdout)
            if swap_log.exists():
                self.assertNotIn("mock fallocate", swap_log.read_text())

    def test_ci_swap_skipped_when_sudo_fails(self):
        """Verifies that CI skips swap creation when passwordless sudo is unavailable."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            bin_dir = temp_path / "bin"
            dist_dir = temp_path / "dist"
            target_dir = temp_path / "stage"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "app.txt").write_text("sample content")

            swap_file = temp_path / "mock_swapfile"
            swap_log = temp_path / "swap.log"

            create_mock_syft_binary(bin_dir)
            self._create_mock_swap_tools(bin_dir, swap_log, initial_swap_mb=512, fail_sudo=True)

            proc = self._run_script(
                [MOCK_RELEASE_BUNDLE_VERSION, str(target_dir)],
                env={
                    "DIST_DIR": str(dist_dir),
                    "CI": "true",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                    "SWAP_FILE_PATH": str(swap_file),
                },
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("Configuring temporary 10G swap space", proc.stdout)
            if swap_log.exists():
                self.assertNotIn("mock fallocate", swap_log.read_text())

    def test_staging_directory_cleaned_up_on_failure(self):
        """Verifies that the intermediate TMP_SBOM_DIR is completely deleted on failure."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            bin_dir = temp_path / "bin"
            dist_dir = temp_path / "dist"
            target_dir = temp_path / "stage"
            target_dir.mkdir(parents=True, exist_ok=True)
            staging_parent = temp_path / "staging_parent"
            staging_parent.mkdir(parents=True, exist_ok=True)

            failing_img = MOCK_REQUIRED_RELEASE_IMAGES[0]
            create_mock_syft_binary(bin_dir, fail_on_images=[failing_img])

            proc = self._run_script(
                [MOCK_RELEASE_BUNDLE_VERSION, str(target_dir)],
                env={
                    "DIST_DIR": str(dist_dir),
                    "CI": "true",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                    "TMPDIR": str(staging_parent),
                },
                bin_dir=bin_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            leaked_dirs = list(staging_parent.glob("kube-agents-sbom-*"))
            self.assertEqual(len(leaked_dirs), 0, f"Expected 0 leaked staging directories, found: {leaked_dirs}")


if __name__ == "__main__":
    unittest.main()
