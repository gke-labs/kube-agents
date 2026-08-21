"""Unit tests for scripts/release/verify_release_eligibility.sh gatekeeper.

Tests release eligibility verification: RC validated tag checking, auto-resolving
latest validated commits, idempotent re-runs, collision detection, image checks, and emergency overrides.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_COLLIDING_RELEASE_TAG,
    MOCK_EMERGENCY_OVERRIDE_REASON,
    MOCK_NONEXISTENT_REF,
    MOCK_RC_VALIDATED_TAG,
    MOCK_TARGET_RELEASE_TAG,
    create_mock_gh_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_VERIFY_SCRIPT = _REPO_ROOT / "scripts" / "release" / "verify_release_eligibility.sh"


class VerifyReleaseEligibilityTest(unittest.TestCase):
    def _create_mock_repo(self, mock_docker_succeeds=True):
        """Creates a temporary git repository with hermetic mock CLI tools."""
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        repo_dir = temp_dir.name

        # Create hermetic bin directory with mock docker CLI
        bin_dir = pathlib.Path(repo_dir) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)

        docker_script = bin_dir / "docker"
        docker_exit = "0" if mock_docker_succeeds else "1"
        docker_script.write_text(f"""#!/bin/sh
exit {docker_exit}
""")
        docker_script.chmod(0o755)

        def git(*args):
            return subprocess.run(
                ["git"] + list(args),
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=True,
            )

        git("init", "-b", "main")
        git("config", "user.name", "Test Bot")
        git("config", "user.email", "bot@example.com")
        git("config", "commit.gpgsign", "false")

        (pathlib.Path(repo_dir) / "README.md").write_text("Test commit")
        git("add", "README.md")
        git("commit", "-m", "feat: initial commit")

        commit_sha = git("rev-parse", "HEAD").stdout.strip()

        return temp_dir, repo_dir, git, commit_sha, bin_dir

    def _run_verify_script(self, repo_dir, args=None, env=None, bin_dir=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", str(_VERIFY_SCRIPT)] + (args or []),
            cwd=repo_dir,
            capture_output=True,
            text=True,
            env=full_env,
        )

    def test_missing_target_tag_fails(self):
        temp_dir, repo_dir, _, _, bin_dir = self._create_mock_repo()
        try:
            proc = self._run_verify_script(repo_dir, args=[], bin_dir=bin_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("TARGET_VERSION is required", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_ambient_github_ref_name_not_used_as_fallback(self):
        temp_dir, repo_dir, _, commit_sha, bin_dir = self._create_mock_repo()
        try:
            # Even if GITHUB_REF_NAME and GITHUB_SHA are set in environment,
            # invoking without explicit args or TARGET_TAG must fail fast.
            proc = self._run_verify_script(
                repo_dir,
                args=[],
                env={"GITHUB_REF_NAME": "0.2.0", "GITHUB_SHA": commit_sha},
                bin_dir=bin_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("TARGET_VERSION is required", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_invalid_target_tag_format_fails(self):
        temp_dir, repo_dir, _, commit_sha, bin_dir = self._create_mock_repo()
        try:
            for bad_tag in INVALID_GA_RELEASE_TAGS:
                with self.subTest(bad_tag=bad_tag):
                    proc = self._run_verify_script(repo_dir, args=[bad_tag, commit_sha], bin_dir=bin_dir)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn("not a valid pure numeric SemVer", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_unresolvable_target_commit_fails_fast(self):
        temp_dir, repo_dir, _, _, bin_dir = self._create_mock_repo()
        try:
            for bad_commit in [MOCK_NONEXISTENT_REF, "latest", "0.9.9", "12345"]:
                with self.subTest(bad_commit=bad_commit):
                    proc = self._run_verify_script(
                        repo_dir,
                        args=[MOCK_TARGET_RELEASE_TAG, bad_commit],
                        bin_dir=bin_dir,
                    )
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn("Cannot resolve valid Git commit", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_auto_resolve_latest_validated_commit(self):
        temp_dir, repo_dir, git, first_sha, bin_dir = self._create_mock_repo()
        try:
            # Tag first commit with validated RC tag
            git("tag", "-a", MOCK_RC_VALIDATED_TAG, first_sha, "-m", f"Validated {MOCK_RC_VALIDATED_TAG}")

            # Create second unvalidated commit on main
            (pathlib.Path(repo_dir) / "file2.txt").write_text("Unvalidated change")
            git("add", "file2.txt")
            git("commit", "-m", "feat: unvalidated commit")

            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            # Call without explicit commit parameter -> should auto-resolve to first_sha
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG],
                env={"GITHUB_OUTPUT": str(gh_out)},
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Auto-resolved latest validated commit", proc.stdout)

            outputs = gh_out.read_text()
            self.assertIn("eligible=true", outputs)
            self.assertIn(f"validated_rc_tag={MOCK_RC_VALIDATED_TAG}", outputs)
            self.assertIn(f"release_commit={first_sha}", outputs)
        finally:
            temp_dir.cleanup()

    def test_auto_resolve_tag_sorting_prefers_newest(self):
        temp_dir, repo_dir, git, first_sha, bin_dir = self._create_mock_repo()
        try:
            # Tag first commit with older validated RC tag
            older_tag = "rc_2608181000_1111111_validated"
            git("tag", "-a", older_tag, first_sha, "-m", f"Validated {older_tag}")

            # Create second commit with newer validated RC tag
            (pathlib.Path(repo_dir) / "file2.txt").write_text("Second change")
            git("add", "file2.txt")
            git("commit", "-m", "feat: second commit")
            second_sha = git("rev-parse", "HEAD").stdout.strip()
            newer_tag = "rc_2608191200_2222222_validated"
            git("tag", "-a", newer_tag, second_sha, "-m", f"Validated {newer_tag}")

            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            # Auto-resolve should deterministically pick newer_tag (second_sha)
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG],
                env={"GITHUB_OUTPUT": str(gh_out)},
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Auto-resolved latest validated commit", proc.stdout)

            outputs = gh_out.read_text()
            self.assertIn("eligible=true", outputs)
            self.assertIn(f"validated_rc_tag={newer_tag}", outputs)
            self.assertIn(f"release_commit={second_sha}", outputs)
        finally:
            temp_dir.cleanup()

    def test_no_validated_commits_and_no_param_fails(self):
        temp_dir, repo_dir, _, _, bin_dir = self._create_mock_repo()
        try:
            # No rc_*_validated tags exist in repo and no commit param passed -> hard fail
            proc = self._run_verify_script(repo_dir, args=[MOCK_TARGET_RELEASE_TAG], bin_dir=bin_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("No validated RC commit found in history", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_emergency_override_with_reason_succeeds(self):
        temp_dir, repo_dir, _, commit_sha, bin_dir = self._create_mock_repo()
        try:
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            env = {
                "SKIP_RC_VALIDATION": "true",
                "EMERGENCY_OVERRIDE_REASON": MOCK_EMERGENCY_OVERRIDE_REASON,
                "GITHUB_OUTPUT": str(gh_out),
            }
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                env=env,
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("explicitly bypassed via emergency override", proc.stderr)

            outputs = gh_out.read_text()
            self.assertIn("eligible=true", outputs)
            self.assertIn("emergency_override=true", outputs)
            self.assertIn(f"release_commit={commit_sha}", outputs)
        finally:
            temp_dir.cleanup()

    def test_emergency_override_without_reason_fails(self):
        temp_dir, repo_dir, _, commit_sha, bin_dir = self._create_mock_repo()
        try:
            # Empty reason
            env = {
                "SKIP_RC_VALIDATION": "true",
                "EMERGENCY_OVERRIDE_REASON": "",
            }
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                env=env,
                bin_dir=bin_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("requires an explicit non-whitespace EMERGENCY_OVERRIDE_REASON", proc.stderr)

            # Whitespace-only reason
            env["EMERGENCY_OVERRIDE_REASON"] = "   "
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                env=env,
                bin_dir=bin_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("requires an explicit non-whitespace EMERGENCY_OVERRIDE_REASON", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_unresolvable_target_commit_fails_even_with_emergency_override(self):
        temp_dir, repo_dir, _, _, bin_dir = self._create_mock_repo()
        try:
            env = {
                "SKIP_RC_VALIDATION": "true",
                "EMERGENCY_OVERRIDE_REASON": MOCK_EMERGENCY_OVERRIDE_REASON,
            }
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, "latest"],
                env=env,
                bin_dir=bin_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Cannot resolve valid Git commit", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_emergency_override_still_blocks_collision(self):
        temp_dir, repo_dir, git, commit_sha, bin_dir = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_COLLIDING_RELEASE_TAG, commit_sha, "-m", f"Release {MOCK_COLLIDING_RELEASE_TAG}")
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                env={
                    "SKIP_RC_VALIDATION": "true",
                    "EMERGENCY_OVERRIDE_REASON": MOCK_EMERGENCY_OVERRIDE_REASON,
                },
                bin_dir=bin_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Collision detected", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_emergency_override_still_skips_idempotent(self):
        temp_dir, repo_dir, git, commit_sha, bin_dir = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_TARGET_RELEASE_TAG, commit_sha, "-m", f"Release {MOCK_TARGET_RELEASE_TAG}")
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                env={
                    "SKIP_RC_VALIDATION": "true",
                    "EMERGENCY_OVERRIDE_REASON": MOCK_EMERGENCY_OVERRIDE_REASON,
                    "GITHUB_OUTPUT": str(gh_out),
                },
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("IDEMPOTENT SKIP", proc.stdout)
            outputs = gh_out.read_text()
            self.assertIn("already_released=true", outputs)
            self.assertIn("skip_release=true", outputs)
        finally:
            temp_dir.cleanup()

    def test_missing_container_images_fails(self):
        temp_dir, repo_dir, _, commit_sha, bin_dir = self._create_mock_repo(mock_docker_succeeds=False)
        try:
            env = {
                "SKIP_RC_VALIDATION": "true",
                "EMERGENCY_OVERRIDE_REASON": MOCK_EMERGENCY_OVERRIDE_REASON,
            }
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                env=env,
                bin_dir=bin_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Required container images", proc.stderr)
            self.assertIn("do not exist in registry", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_idempotent_skip_when_same_release_tag_exists(self):
        temp_dir, repo_dir, git, commit_sha, bin_dir = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_TARGET_RELEASE_TAG, commit_sha, "-m", f"Release {MOCK_TARGET_RELEASE_TAG}")
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                env={"GITHUB_OUTPUT": str(gh_out)},
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("IDEMPOTENT SKIP", proc.stdout)

            outputs = gh_out.read_text()
            self.assertIn("eligible=false", outputs)
            self.assertIn("already_released=true", outputs)
            self.assertIn("skip_release=true", outputs)
        finally:
            temp_dir.cleanup()

    def test_collision_detection_when_different_release_tag_exists(self):
        temp_dir, repo_dir, git, commit_sha, bin_dir = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_COLLIDING_RELEASE_TAG, commit_sha, "-m", f"Release {MOCK_COLLIDING_RELEASE_TAG}")
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                bin_dir=bin_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Collision detected", proc.stderr)
            self.assertIn(f"already published under release {MOCK_COLLIDING_RELEASE_TAG}", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_eligible_when_rc_validated_tag_exists(self):
        temp_dir, repo_dir, git, commit_sha, bin_dir = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_RC_VALIDATED_TAG, commit_sha, "-m", f"Validated {MOCK_RC_VALIDATED_TAG}")
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                env={"GITHUB_OUTPUT": str(gh_out)},
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("ELIGIBLE: Found validated RC tag", proc.stdout)

            outputs = gh_out.read_text()
            self.assertIn("eligible=true", outputs)
            self.assertIn(f"validated_rc_tag={MOCK_RC_VALIDATED_TAG}", outputs)
        finally:
            temp_dir.cleanup()

    def test_blocked_when_no_rc_validated_tag(self):
        temp_dir, repo_dir, _, commit_sha, bin_dir = self._create_mock_repo()
        try:
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                bin_dir=bin_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("BLOCKED: Commit", proc.stderr)
            self.assertIn("has NOT passed live RC E2E validation", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_env_vars_invocation_without_args(self):
        temp_dir, repo_dir, git, commit_sha, bin_dir = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_RC_VALIDATED_TAG, commit_sha, "-m", f"Validated {MOCK_RC_VALIDATED_TAG}")
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            proc = self._run_verify_script(
                repo_dir,
                args=[],
                env={
                    "TARGET_VERSION": MOCK_TARGET_RELEASE_TAG,
                    "TARGET_COMMIT": commit_sha,
                    "GITHUB_OUTPUT": str(gh_out),
                },
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("ELIGIBLE: Found validated RC tag", proc.stdout)

            outputs = gh_out.read_text()
            self.assertIn("eligible=true", outputs)
            self.assertIn(f"release_commit={commit_sha}", outputs)
        finally:
            temp_dir.cleanup()

    def test_resumes_release_when_tag_exists_but_github_release_missing(self):
        temp_dir, repo_dir, git, commit_sha, bin_dir = self._create_mock_repo()
        try:
            # Commit has validated RC tag and GA tag, but mock gh CLI returns 1 on gh release view (release not published)
            git("tag", "-a", MOCK_RC_VALIDATED_TAG, commit_sha, "-m", f"Validated {MOCK_RC_VALIDATED_TAG}")
            git("tag", "-a", MOCK_TARGET_RELEASE_TAG, commit_sha, "-m", f"Release {MOCK_TARGET_RELEASE_TAG}")
            create_mock_gh_binary(bin_dir, existing_releases=[])
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                env={"GH_TOKEN": "mock-token", "GITHUB_OUTPUT": str(gh_out)},
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Resuming release workflow", proc.stdout)
            self.assertIn("ELIGIBLE: Found validated RC tag", proc.stdout)

            outputs = gh_out.read_text()
            self.assertIn("eligible=true", outputs)
            self.assertIn("resuming=true", outputs)
            self.assertIn(f"validated_rc_tag={MOCK_RC_VALIDATED_TAG}", outputs)
            self.assertIn(f"release_commit={commit_sha}", outputs)
        finally:
            temp_dir.cleanup()

    def test_resumes_release_blocked_when_no_rc_validated_tag(self):
        temp_dir, repo_dir, git, commit_sha, bin_dir = self._create_mock_repo()
        try:
            # GA tag exists, but commit was NEVER validated and has no rc_*_validated tag
            git("tag", "-a", MOCK_TARGET_RELEASE_TAG, commit_sha, "-m", f"Release {MOCK_TARGET_RELEASE_TAG}")
            create_mock_gh_binary(bin_dir, existing_releases=[])
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                env={"GH_TOKEN": "mock-token"},
                bin_dir=bin_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("BLOCKED: Commit", proc.stderr)
            self.assertIn("has NOT passed live RC E2E validation", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_resumes_release_with_emergency_override_when_tag_exists_and_github_release_missing(self):
        temp_dir, repo_dir, git, commit_sha, bin_dir = self._create_mock_repo()
        try:
            # GA tag exists, commit has no RC tag, gh release view fails, but emergency override is provided
            git("tag", "-a", MOCK_TARGET_RELEASE_TAG, commit_sha, "-m", f"Release {MOCK_TARGET_RELEASE_TAG}")
            create_mock_gh_binary(bin_dir, existing_releases=[])
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                env={
                    "GH_TOKEN": "mock-token",
                    "SKIP_RC_VALIDATION": "true",
                    "EMERGENCY_OVERRIDE_REASON": "Emergency security CVE patch",
                    "GITHUB_OUTPUT": str(gh_out),
                },
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("RC E2E validation check is explicitly bypassed", proc.stderr)

            outputs = gh_out.read_text()
            self.assertIn("eligible=true", outputs)
            self.assertIn("emergency_override=true", outputs)
            self.assertIn(f"release_commit={commit_sha}", outputs)
        finally:
            temp_dir.cleanup()

    def test_idempotent_skip_when_both_tag_and_github_release_exist(self):
        temp_dir, repo_dir, git, commit_sha, bin_dir = self._create_mock_repo()
        try:
            # Both git tag and GitHub release exist
            git("tag", "-a", MOCK_TARGET_RELEASE_TAG, commit_sha, "-m", f"Release {MOCK_TARGET_RELEASE_TAG}")
            create_mock_gh_binary(bin_dir, existing_releases=[MOCK_TARGET_RELEASE_TAG])
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            proc = self._run_verify_script(
                repo_dir,
                args=[MOCK_TARGET_RELEASE_TAG, commit_sha],
                env={"GH_TOKEN": "mock-token", "GITHUB_OUTPUT": str(gh_out)},
                bin_dir=bin_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("IDEMPOTENT SKIP", proc.stdout)
            self.assertIn("Release version 0.2.0 and GitHub Release for commit", proc.stdout)

            outputs = gh_out.read_text()
            self.assertIn("eligible=false", outputs)
            self.assertIn("already_released=true", outputs)
            self.assertIn("skip_release=true", outputs)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
