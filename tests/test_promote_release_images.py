"""Unit tests for scripts/release/promote_release_images.sh.

Tests argument validation, pure numeric SemVer enforcement, and image promotion
execution with mock Docker CLI fixtures.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    create_mock_git_repo,
    get_isolated_test_env,
)
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
    def _run_script(self, args, env=None, bin_dir=None, cwd=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", str(_PROMOTE_RELEASE_IMAGES_SH)] + args,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=cwd or str(_REPO_ROOT),
        )

    def test_missing_arguments(self):
        proc = self._run_script([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RELEASE_VERSION is required as first argument or environment variable", proc.stderr)

    def test_invalid_tag_format(self):
        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_script([bad_tag])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_local_dry_run_skips_promotion(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)
            git("tag", MOCK_TARGET_RELEASE_TAG, "HEAD")

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: Remote image promotion", proc.stdout)
            self.assertIn("skipped (runs only in CI)", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_execution(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            candidate_commit = git("rev-parse", "HEAD").stdout.strip()
            git("checkout", "--detach", candidate_commit)
            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore: stamp 0.2.0")
            release_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, release_commit)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("PROMOTING RELEASE CONTAINER IMAGES", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoting {img}", proc.stdout)
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
            docker_log = pathlib.Path(temp_dir.name) / "bin" / "docker.log"
            self.assertIn("--prefer-index=false", docker_log.read_text())
        finally:
            temp_dir.cleanup()

    def test_promote_idempotent_skip_when_target_exists(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            candidate_commit = git("rev-parse", "HEAD").stdout.strip()
            git("checkout", "--detach", candidate_commit)
            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore: stamp 0.2.0")
            release_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, release_commit)

            existing = [
                f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"
                for img in MOCK_REQUIRED_RELEASE_IMAGES
            ] + [
                f"ghcr.io/gke-labs/kube-agents/{img}:{candidate_commit}"
                for img in MOCK_REQUIRED_RELEASE_IMAGES
            ]
            create_mock_docker_binary(bin_dir, existing_images=existing)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn("already exists in registry and matches source image", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_idempotent_skip_when_target_is_index_wrapping_source_manifest(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            candidate_commit = git("rev-parse", "HEAD").stdout.strip()
            git("checkout", "--detach", candidate_commit)
            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore: stamp 0.2.0")
            release_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, release_commit)

            source_sha = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
            target_index_sha = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            raw_index = f'{{"mediaType":"application/vnd.oci.image.index.v1+json","digest":"{target_index_sha}","manifests":[{{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"{source_sha}"}}]}}'
            digests = {}
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"] = {
                    "format": target_index_sha,
                    "raw": raw_index,
                }
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{candidate_commit}"] = {
                    "format": source_sha,
                    "raw": f'{{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"{source_sha}"}}',
                }
            create_mock_docker_binary(bin_dir, image_digests=digests)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn("already exists in registry and matches source image", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_fails_when_target_exists_with_mismatched_digest(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            candidate_commit = git("rev-parse", "HEAD").stdout.strip()
            git("checkout", "--detach", candidate_commit)
            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore: stamp 0.2.0")
            release_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, release_commit)

            digests = {}
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"] = (
                    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                )
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{candidate_commit}"] = (
                    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                )
            create_mock_docker_binary(bin_dir, image_digests=digests)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does NOT match source image", proc.stderr)
            self.assertIn("Release promotion blocked", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_promote_execution_env_vars(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)
            git("tag", MOCK_TARGET_RELEASE_TAG, "HEAD")

            proc = self._run_script(
                [],
                env={"CI": "true", "RELEASE_VERSION": MOCK_TARGET_RELEASE_TAG},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("PROMOTING RELEASE CONTAINER IMAGES", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_execution_single_release_version_argument(self):
        """Verifies promote_release_images resolves candidate commit from tag's parent when only version is passed."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            # 1. Candidate commit on main
            candidate_commit = git("rev-parse", "HEAD").stdout.strip()

            # 2. Release commit on detached HEAD with tag
            git("checkout", "--detach", candidate_commit)
            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore(release): stamp 0.2.0")
            release_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, release_commit)

            # 3. Call promote_release_images with only version argument
            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("PROMOTING RELEASE CONTAINER IMAGES", proc.stdout)
            self.assertIn(f"Release Commit:  {candidate_commit}", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_execution_direct_unstamped_tag_resolves_exact_commit(self):
        """Verifies promote_release_images resolves exact tag commit when tag is placed directly on a commit."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            direct_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, direct_commit)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("PROMOTING RELEASE CONTAINER IMAGES", proc.stdout)
            self.assertIn(f"Release Commit:  {direct_commit}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_missing_release_tag_fails_fast_without_head_fallback(self):
        """Verifies promote_release_images fails fast with exit code 1 when tag does not exist (no HEAD fallback)."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                ["9.9.9"],
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Cannot resolve source image commit for version '9.9.9'", proc.stderr)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
