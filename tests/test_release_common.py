"""Unit tests for scripts/release/common.sh helper routines and registries.

Tests boolean parsing, SemVer validation, SemVer comparison, repository and registry prefix
resolution, Git tag lookup, and declarative release registries.
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
    VALID_GA_RELEASE_TAGS,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_REQUIRED_RELEASE_IMAGES,
    MOCK_SAMPLE_COMMIT_SHA,
    MOCK_SAMPLE_SHORT_SHA,
    MOCK_TARGET_RELEASE_TAG,
    create_mock_docker_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_COMMON_SH = _REPO_ROOT / "scripts" / "release" / "common.sh"


class ReleaseCommonTest(unittest.TestCase):
    def _run_common_func(self, func_call, env=None, bin_dir=None, cwd=None):
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
            cwd=cwd or str(_REPO_ROOT),
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

    def test_validate_pure_numeric_semver(self):
        for tag in VALID_GA_RELEASE_TAGS:
            with self.subTest(tag=tag):
                proc = self._run_common_func(f'validate_pure_numeric_semver "{tag}"')
                self.assertEqual(proc.returncode, 0)

        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_common_func(f'validate_pure_numeric_semver "{bad_tag}"')
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_compare_semver(self):
        test_cases = [
            ("0.2.0", "0.1.0", "1"),
            ("0.1.1", "0.1.0", "1"),
            ("1.0.0", "0.9.9", "1"),
            ("0.2.0", "0.2.0", "0"),
            ("0.1.0", "0.2.0", "-1"),
            ("0.1.0", "0.1.1", "-1"),
            ("0.9.9", "1.0.0", "-1"),
        ]
        for v1, v2, expected in test_cases:
            with self.subTest(v1=v1, v2=v2):
                proc = self._run_common_func(f'compare_semver "{v1}" "{v2}"')
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout.strip(), expected)

    def test_get_latest_ga_tag(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            # Initially no tags
            proc = self._run_common_func('get_latest_ga_tag', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            # Initially no tags, explicit fallback provided
            proc_default = self._run_common_func('get_latest_ga_tag "0.1.0"', cwd=repo_dir)
            self.assertEqual(proc_default.returncode, 0)
            self.assertEqual(proc_default.stdout.strip(), "0.1.0")

            # Add mixed tags
            git("tag", "-a", "0.1.0", "-m", "Release 0.1.0")
            git("tag", "-a", "0.2.0", "-m", "Release 0.2.0")
            git("tag", "-a", "0.1.5", "-m", "Release 0.1.5")
            git("tag", "-a", "rc_0.3.0_validated", "-m", "RC tag")
            git("tag", "-a", "v1.0.0", "-m", "v-tag")

            proc = self._run_common_func('get_latest_ga_tag', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.2.0")
        finally:
            temp_dir.cleanup()

    def test_get_latest_validated_rc_tag(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            # Initially no validated tags
            proc = self._run_common_func('get_latest_validated_rc_tag', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            # Add mixed tags including older and newer validated RC tags
            git("tag", "-a", "rc_2608181000_1111111_validated", "-m", "Older RC")
            git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Newer RC")
            git("tag", "-a", "rc_2608191300_3333333", "-m", "Unvalidated RC")
            git("tag", "-a", "0.2.0", "-m", "GA tag")

            proc = self._run_common_func('get_latest_validated_rc_tag', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "rc_2608191200_2222222_validated")
        finally:
            temp_dir.cleanup()

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
        self.assertIn("commit_sha and release_version are required", proc.stderr)

        # Invalid target_tag format
        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_common_func(f'promote_release_images "{MOCK_SAMPLE_SHORT_SHA}" "{bad_tag}"')
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_promote_release_images_local_dry_run(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: Remote image promotion", proc.stdout)
            self.assertIn("skipped (runs only in CI)", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_execution(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Promoting verified container images", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoting {img}", proc.stdout)
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_swapped_arguments(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_TARGET_RELEASE_TAG}" "{MOCK_SAMPLE_COMMIT_SHA}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_idempotent_skip(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            existing = [
                f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"
                for img in MOCK_REQUIRED_RELEASE_IMAGES
            ] + [
                f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_SAMPLE_COMMIT_SHA}"
                for img in MOCK_REQUIRED_RELEASE_IMAGES
            ]
            create_mock_docker_binary(bin_dir, existing_images=existing)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn("already exists in registry and matches source image", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_idempotent_skip_when_target_is_index_wrapping_source_manifest(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            source_sha = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
            target_index_sha = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            raw_index = f'{{"mediaType":"application/vnd.oci.image.index.v1+json","digest":"{target_index_sha}","manifests":[{{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"{source_sha}"}}]}}'
            digests = {}
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"] = {
                    "format": target_index_sha,
                    "raw": raw_index,
                }
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_SAMPLE_COMMIT_SHA}"] = {
                    "format": source_sha,
                    "raw": f'{{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"{source_sha}"}}',
                }
            create_mock_docker_binary(bin_dir, image_digests=digests)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn("already exists in registry and matches source image", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_fails_when_mismatched_digest(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            digests = {}
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"] = (
                    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                )
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_SAMPLE_COMMIT_SHA}"] = (
                    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                )
            create_mock_docker_binary(bin_dir, image_digests=digests)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does NOT match source image", proc.stderr)
            self.assertIn("Release promotion blocked", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_ensure_git_tag_hermetic_local_execution(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head_commit = git("rev-parse", "HEAD").stdout.strip()

            # Local execution should create tag without remote operations
            proc = self._run_common_func(
                f'ensure_git_tag "{MOCK_TARGET_RELEASE_TAG}" "{head_commit}" "Test release {MOCK_TARGET_RELEASE_TAG}"',
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn(f"Dry-run: Git tag '{MOCK_TARGET_RELEASE_TAG}' created locally", proc.stdout)

            # Tag is locally created
            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertEqual(tag_commit, head_commit)

            # Idempotent skip on second run
            proc2 = self._run_common_func(
                f'ensure_git_tag "{MOCK_TARGET_RELEASE_TAG}" "{head_commit}" "Test release {MOCK_TARGET_RELEASE_TAG}"',
                cwd=repo_dir,
            )
            self.assertEqual(proc2.returncode, 0)
            self.assertIn("Idempotent skip", proc2.stdout)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
