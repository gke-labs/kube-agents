"""Unit tests for scripts/release/publish_github_release.sh.

Tests argument validation, pure numeric SemVer enforcement, commit SHA resolution,
missing CLI handling in CI vs local environments, idempotent skip, and GitHub release creation.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    create_minimal_tools_bin,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_GH_TOKEN,
    MOCK_NONEXISTENT_TAG,
    MOCK_TARGET_RELEASE_TAG,
    create_mock_gh_binary,
    create_mock_git_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PUBLISH_GITHUB_RELEASE_SH = _REPO_ROOT / "scripts" / "release" / "publish_github_release.sh"


class PublishGithubReleaseScriptTest(unittest.TestCase):
    def _run_script(self, args, env=None, bin_dir=None, cwd=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", str(_PUBLISH_GITHUB_RELEASE_SH)] + args,
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

    def test_missing_gh_cli_in_ci(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = create_minimal_tools_bin(temp_dir.name, exclude=("gh",))
            create_mock_git_binary(bin_dir)
            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "HEAD"],
                env={"CI": "true", "PATH": str(bin_dir)},
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("'gh' CLI is mandatory in CI", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_idempotent_skip_when_release_exists(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_git_binary(bin_dir)
            create_mock_gh_binary(bin_dir, existing_releases=[MOCK_TARGET_RELEASE_TAG])

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "HEAD"],
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("already exists", proc.stdout)
            self.assertIn("Idempotent skip", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_local_dry_run_without_gh_token(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_git_binary(bin_dir)
            create_mock_gh_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "HEAD"],
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: GitHub release", proc.stdout)
            self.assertIn("creation skipped (runs only in CI)", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_local_dry_run_with_gh_token_set(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_git_binary(bin_dir)
            create_mock_gh_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "HEAD"],
                env={"GH_TOKEN": "mock-token-123"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: GitHub release", proc.stdout)
            self.assertIn("creation skipped (runs only in CI)", proc.stdout)
            gh_log = (bin_dir / "gh.log").read_text()
            self.assertNotIn("mock gh: release create", gh_log)
        finally:
            temp_dir.cleanup()

    def test_publish_execution_with_gh_token(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_git_binary(bin_dir)
            create_mock_gh_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "HEAD"],
                env={"CI": "true", "GH_TOKEN": "mock-token-123"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("PUBLISHING GITHUB RELEASE", proc.stdout)
            self.assertIn(f"Successfully published GitHub Release '{MOCK_TARGET_RELEASE_TAG}'", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_publish_execution_with_env_vars(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_git_binary(bin_dir)
            create_mock_gh_binary(bin_dir)

            proc = self._run_script(
                [],
                env={
                    "RELEASE_VERSION": MOCK_TARGET_RELEASE_TAG,
                    "CI": "true",
                    "GH_TOKEN": "mock-token-123",
                },
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("PUBLISHING GITHUB RELEASE", proc.stdout)
            self.assertIn(f"Successfully published GitHub Release '{MOCK_TARGET_RELEASE_TAG}'", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_publish_with_version_only_resolves_commit_from_git_tag(self):
        """Verifies publish_github_release resolves commit directly from tag when only version is passed."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_gh_binary(bin_dir)

            # Create a tag pointing to a specific commit
            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore: release 0.2.0")
            expected_tag_sha = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, expected_tag_sha)

            # Run with only the version argument (no commit argument)
            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true", "GH_TOKEN": "mock-token-123"},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("PUBLISHING GITHUB RELEASE", proc.stdout)
            self.assertIn(f"Release Commit:     {expected_tag_sha}", proc.stdout)
            self.assertIn(f"Successfully published GitHub Release '{MOCK_TARGET_RELEASE_TAG}'", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_publish_fails_if_tag_does_not_exist_and_no_commit_provided(self):
        """Verifies publish_github_release errors clearly if tag does not exist and no HEAD commit exists."""
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            repo_dir = pathlib.Path(temp_dir.name) / "empty_repo"
            repo_dir.mkdir()
            subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_gh_binary(bin_dir)

            proc = self._run_script(
                [MOCK_NONEXISTENT_TAG],
                env={"CI": "true", "GH_TOKEN": MOCK_GH_TOKEN},
                bin_dir=str(bin_dir),
                cwd=str(repo_dir),
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn(f"Cannot resolve valid Git commit for release tag '{MOCK_NONEXISTENT_TAG}'", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_publish_fails_if_tag_does_not_exist_even_when_head_commit_present(self):
        """Verifies publish_github_release fails and does NOT fall back to HEAD commit when tag is absent."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_gh_binary(bin_dir)

            proc = self._run_script(
                [MOCK_NONEXISTENT_TAG],
                env={"CI": "true", "GH_TOKEN": MOCK_GH_TOKEN},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn(f"Cannot resolve valid Git commit for release tag '{MOCK_NONEXISTENT_TAG}'", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_publish_attaches_distribution_artifacts_from_dist_dir(self):
        """Verifies publish_github_release finds and attaches all files from DIST_DIR to gh release create."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            _, gh_log = create_mock_gh_binary(bin_dir)

            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore: release 0.2.0")
            expected_tag_sha = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, expected_tag_sha)

            dist_dir = pathlib.Path(temp_dir.name) / "dist"
            dist_dir.mkdir()
            (dist_dir / f"kube-agents-{MOCK_TARGET_RELEASE_TAG}.tar.gz").write_bytes(b"tarball")
            (dist_dir / f"kube-agents-{MOCK_TARGET_RELEASE_TAG}.zip").write_bytes(b"zip")
            (dist_dir / f"kube-agents-{MOCK_TARGET_RELEASE_TAG}.tgz").write_bytes(b"chart")
            (dist_dir / "checksums.txt").write_text("checksums")

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={
                    "CI": "true",
                    "GH_TOKEN": "mock-token-123",
                    "DIST_DIR": str(dist_dir),
                },
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Release Artifacts: 4 files found to attach", proc.stdout)
            self.assertIn(f"Successfully published GitHub Release '{MOCK_TARGET_RELEASE_TAG}'", proc.stdout)

            gh_calls = gh_log.read_text()
            self.assertIn(f"release create {MOCK_TARGET_RELEASE_TAG}", gh_calls)
            self.assertIn(f"kube-agents-{MOCK_TARGET_RELEASE_TAG}.tar.gz", gh_calls)
            self.assertIn(f"kube-agents-{MOCK_TARGET_RELEASE_TAG}.zip", gh_calls)
            self.assertIn(f"kube-agents-{MOCK_TARGET_RELEASE_TAG}.tgz", gh_calls)
            self.assertIn("checksums.txt", gh_calls)
        finally:
            temp_dir.cleanup()

    def test_existing_release_in_ci_uploads_artifacts_with_clobber(self):
        """Verifies publish_github_release in CI uploads missing artifacts via gh release upload --clobber when release exists."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            _, gh_log = create_mock_gh_binary(bin_dir, existing_releases=[MOCK_TARGET_RELEASE_TAG])

            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore: release 0.2.0")
            expected_tag_sha = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, expected_tag_sha)

            dist_dir = pathlib.Path(temp_dir.name) / "dist"
            dist_dir.mkdir()
            (dist_dir / f"kube-agents-{MOCK_TARGET_RELEASE_TAG}.tar.gz").write_bytes(b"tarball")
            (dist_dir / "checksums.txt").write_text("checksums")

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={
                    "CI": "true",
                    "GH_TOKEN": "mock-token-123",
                    "DIST_DIR": str(dist_dir),
                },
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"GitHub Release '{MOCK_TARGET_RELEASE_TAG}' already exists", proc.stdout)
            self.assertIn("Uploading/updating release assets to existing release", proc.stdout)
            self.assertIn(f"Successfully uploaded 2 artifacts to existing release '{MOCK_TARGET_RELEASE_TAG}'", proc.stdout)

            gh_calls = gh_log.read_text()
            self.assertIn(f"release upload {MOCK_TARGET_RELEASE_TAG}", gh_calls)
            self.assertIn("--clobber", gh_calls)
            self.assertIn(f"kube-agents-{MOCK_TARGET_RELEASE_TAG}.tar.gz", gh_calls)
            self.assertIn("checksums.txt", gh_calls)
        finally:
            temp_dir.cleanup()

    def test_publish_without_dist_dir_uses_guarded_array_expansion_for_bash_32(self):
        """Verifies publish_github_release creates release cleanly when DIST_DIR is absent or empty (bash 3.2 guard)."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            _, gh_log = create_mock_gh_binary(bin_dir)

            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore: release 0.2.0")
            expected_tag_sha = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, expected_tag_sha)

            nonexistent_dist = pathlib.Path(temp_dir.name) / "nonexistent_dist"

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={
                    "CI": "true",
                    "GH_TOKEN": "mock-token-123",
                    "DIST_DIR": str(nonexistent_dist),
                },
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Release Artifacts: None found", proc.stdout)
            self.assertIn(f"Successfully published GitHub Release '{MOCK_TARGET_RELEASE_TAG}'", proc.stdout)

            gh_calls = gh_log.read_text()
            self.assertIn(f"release create {MOCK_TARGET_RELEASE_TAG}", gh_calls)
        finally:
            temp_dir.cleanup()

    def test_publish_script_uses_bash_32_guarded_array_syntax(self):
        """Verifies publish_github_release.sh uses ${dist_files[@]+"${dist_files[@]}"} to avoid macOS bash 3.2 unbound variable."""
        content = _PUBLISH_GITHUB_RELEASE_SH.read_text()
        self.assertIn('${dist_files[@]+"${dist_files[@]}"}', content)
        for line in content.splitlines():
            if "gh release create" in line or "gh release upload" in line:
                self.assertIn('${dist_files[@]+"${dist_files[@]}"}', line)


if __name__ == "__main__":
    unittest.main()
