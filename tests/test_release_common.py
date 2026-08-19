"""Unit tests for scripts/release/common.sh helper routines and registries.

Tests boolean parsing, version canonicalization, repository and registry prefix
resolution, and declarative release registries.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    FALSY_BOOLEAN_INPUTS,
    MOCK_CUSTOM_ORG,
    MOCK_CUSTOM_REGISTRY_PREFIX,
    MOCK_CUSTOM_REPO,
    MOCK_CUSTOM_TARGET_REPO,
    MOCK_DEFAULT_REGISTRY_PREFIX,
    MOCK_DEFAULT_RELEASE_REPO,
    TRUTHY_BOOLEAN_INPUTS,
    get_isolated_test_env,
)
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_REQUIRED_RELEASE_IMAGES,
    MOCK_SAMPLE_COMMIT_SHA,
    MOCK_SAMPLE_SHORT_SHA,
    MOCK_TARGET_RELEASE_TAG,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_COMMON_SH = _REPO_ROOT / "scripts" / "release" / "common.sh"


class ReleaseCommonTest(unittest.TestCase):
    def _run_common_func(self, func_call, env=None, bin_dir=None):
        """Source common.sh and execute the given bash snippet."""
        setup = f"""
source "{_COMMON_SH}"
{func_call}
"""
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(_REPO_ROOT),
        )

    def test_is_truthy(self):
        for val in TRUTHY_BOOLEAN_INPUTS:
            with self.subTest(val=val):
                proc = self._run_common_func(f'is_truthy "{val}"')
                self.assertEqual(proc.returncode, 0, f"Expected '{val}' to be truthy")

        for val in FALSY_BOOLEAN_INPUTS:
            with self.subTest(val=val):
                proc = self._run_common_func(f'is_truthy "{val}"')
                self.assertNotEqual(proc.returncode, 0, f"Expected '{val}' to be falsy")

    def test_get_target_repo(self):
        # Default
        proc = self._run_common_func('get_target_repo', env={"GH_ORG": "", "GH_REPO": "", "GITHUB_REPOSITORY": ""})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), MOCK_DEFAULT_RELEASE_REPO)

        # Via GITHUB_REPOSITORY
        proc = self._run_common_func('get_target_repo', env={"GITHUB_REPOSITORY": MOCK_CUSTOM_TARGET_REPO})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), MOCK_CUSTOM_TARGET_REPO)

        # Via GH_ORG and GH_REPO
        proc = self._run_common_func('get_target_repo', env={"GH_ORG": MOCK_CUSTOM_ORG, "GH_REPO": MOCK_CUSTOM_REPO})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), f"{MOCK_CUSTOM_ORG}/{MOCK_CUSTOM_REPO}")

    def test_get_registry_prefix(self):
        # Default
        proc = self._run_common_func('get_registry_prefix', env={"REGISTRY_PREFIX": "", "GH_ORG": "", "GH_REPO": "", "GITHUB_REPOSITORY": ""})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), MOCK_DEFAULT_REGISTRY_PREFIX)

        # Explicit REGISTRY_PREFIX
        proc = self._run_common_func('get_registry_prefix', env={"REGISTRY_PREFIX": MOCK_CUSTOM_REGISTRY_PREFIX})
        self.assertEqual(proc.stdout.strip(), MOCK_CUSTOM_REGISTRY_PREFIX)

    def test_required_release_images_registry(self):
        cmd = 'echo "IMAGES=${REQUIRED_RELEASE_IMAGES[*]}"'
        proc = self._run_common_func(cmd)
        self.assertEqual(proc.returncode, 0)
        for img in MOCK_REQUIRED_RELEASE_IMAGES:
            self.assertIn(img, proc.stdout)

    def test_is_ci_pipeline_behavior(self):
        # By default isolated env has CI stripped
        proc = self._run_common_func('is_ci_pipeline')
        self.assertNotEqual(proc.returncode, 0)

        # With explicit CI=true
        proc = self._run_common_func('is_ci_pipeline', env={"CI": "true"})
        self.assertEqual(proc.returncode, 0)

    def test_promote_release_images_validation(self):
        # Missing args
        proc = self._run_common_func('promote_release_images "" ""')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("commit_sha and target_tag are required", proc.stderr)

        # Invalid target_tag format
        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_common_func(f'promote_release_images "{MOCK_SAMPLE_SHORT_SHA}" "{bad_tag}"')
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_promote_release_images_execution(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            mock_docker = bin_dir / "docker"
            mock_docker.write_text("""#!/bin/sh
echo "mock docker: $@"
exit 0
""")
            mock_docker.chmod(0o755)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Promoting verified container images", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoting {img}", proc.stdout)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
