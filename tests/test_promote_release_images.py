"""Unit tests for scripts/release/promote_release_images.sh.

Tests argument validation, pure numeric SemVer enforcement, and image promotion
execution with mock Docker CLI fixtures.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_REQUIRED_RELEASE_IMAGES,
    MOCK_SAMPLE_COMMIT_SHA,
    MOCK_TARGET_RELEASE_TAG,
    create_mock_docker_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROMOTE_RELEASE_IMAGES_SH = _REPO_ROOT / "scripts" / "release" / "promote_release_images.sh"


class PromoteReleaseImagesScriptTest(unittest.TestCase):
    def _run_script(self, args, env=None, bin_dir=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", str(_PROMOTE_RELEASE_IMAGES_SH)] + args,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(_REPO_ROOT),
        )

    def test_missing_arguments(self):
        proc = self._run_script([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RELEASE_COMMIT and RELEASE_VERSION are required", proc.stderr)

    def test_invalid_tag_format(self):
        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_script([MOCK_SAMPLE_COMMIT_SHA, bad_tag])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_local_dry_run_skips_promotion(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_SAMPLE_COMMIT_SHA, MOCK_TARGET_RELEASE_TAG],
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: Remote image promotion", proc.stdout)
            self.assertIn("skipped (runs only in CI)", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_execution(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_SAMPLE_COMMIT_SHA, MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("PROMOTING RELEASE CONTAINER IMAGES", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoting {img}", proc.stdout)
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
            docker_log = pathlib.Path(temp_dir.name) / "bin" / "docker.log"
            self.assertIn("--prefer-index=false", docker_log.read_text())
        finally:
            temp_dir.cleanup()

    def test_promote_execution_swapped_arguments(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, MOCK_SAMPLE_COMMIT_SHA],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("PROMOTING RELEASE CONTAINER IMAGES", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_idempotent_skip_when_target_exists(self):
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

            proc = self._run_script(
                [MOCK_SAMPLE_COMMIT_SHA, MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn("already exists in registry and matches source image", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_idempotent_skip_when_target_is_index_wrapping_source_manifest(self):
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

            proc = self._run_script(
                [MOCK_SAMPLE_COMMIT_SHA, MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn("already exists in registry and matches source image", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_fails_when_target_exists_with_mismatched_digest(self):
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

            proc = self._run_script(
                [MOCK_SAMPLE_COMMIT_SHA, MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does NOT match source image", proc.stderr)
            self.assertIn("Release promotion blocked", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_promote_execution_env_vars(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [],
                env={"CI": "true", "RELEASE_COMMIT": MOCK_SAMPLE_COMMIT_SHA, "RELEASE_VERSION": MOCK_TARGET_RELEASE_TAG},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("PROMOTING RELEASE CONTAINER IMAGES", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
