"""Unit tests for scripts/release/publish_helm_chart.sh.

Tests argument validation, pure numeric SemVer enforcement, CLI detection
in CI vs local environments, packaging, dry-run safety, and OCI chart publishing.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_DEFAULT_REGISTRY_PREFIX,
    MOCK_DEFAULT_RELEASE_REPO,
    create_minimal_tools_bin,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_GH_TOKEN,
    MOCK_GH_USER,
    MOCK_SAMPLE_COMMIT_SHA,
    MOCK_TARGET_RELEASE_TAG,
    create_mock_cosign_binary,
    create_mock_docker_binary,
    create_mock_git_binary,
    create_mock_helm_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PUBLISH_HELM_CHART_SH = _REPO_ROOT / "scripts" / "release" / "publish_helm_chart.sh"


class PublishHelmChartScriptTest(unittest.TestCase):
    def _run_script(self, args, env=None, bin_dir=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", str(_PUBLISH_HELM_CHART_SH)] + args,
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

    def test_missing_helm_in_ci(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = create_minimal_tools_bin(temp_dir.name, exclude=("helm",))
            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true", "PATH": str(bin_dir)},
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("'helm' CLI is mandatory in CI", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_missing_helm_in_local_execution(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = create_minimal_tools_bin(temp_dir.name, exclude=("helm",))
            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"PATH": str(bin_dir)},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("'helm' CLI not found in PATH", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_dry_run_local_execution(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: Helm chart packaged at", proc.stdout)
            self.assertIn(f"Remote push to oci://{MOCK_DEFAULT_REGISTRY_PREFIX}/charts and signing skipped", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_ci_full_publish_and_sign(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={
                    "CI": "true",
                    "GITHUB_REPOSITORY": MOCK_DEFAULT_RELEASE_REPO,
                    "GH_TOKEN": MOCK_GH_TOKEN,
                    "GH_USER": MOCK_GH_USER,
                },
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Logging in to ghcr.io via Helm", proc.stdout)
            self.assertIn("Successfully logged in to ghcr.io via Helm", proc.stdout)
            self.assertIn("PUBLISHING AND SIGNING HELM CHART (OCI)", proc.stdout)
            self.assertIn("Successfully signed Helm chart digest", proc.stdout)
            self.assertIn("Successfully published Helm chart", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_missing_cosign_in_ci(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = create_minimal_tools_bin(temp_dir.name, exclude=("cosign",))
            create_mock_helm_binary(bin_dir)
            create_mock_docker_binary(bin_dir)
            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true", "PATH": str(bin_dir)},
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("'cosign' CLI is mandatory in CI", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_packaging_failure(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir, fail_package=True)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                bin_dir=str(bin_dir),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to package Helm chart", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_push_failure_in_ci(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir, fail_push=True)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={
                    "CI": "true",
                    "GITHUB_REPOSITORY": MOCK_DEFAULT_RELEASE_REPO,
                    "GH_TOKEN": MOCK_GH_TOKEN,
                    "GH_USER": MOCK_GH_USER,
                },
                bin_dir=str(bin_dir),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to push Helm chart", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_env_var_invocation(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [],
                env={"RELEASE_VERSION": MOCK_TARGET_RELEASE_TAG},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: Helm chart packaged at", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_ci_idempotent_skip_when_chart_exists(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            existing_chart_oci = f"{MOCK_DEFAULT_REGISTRY_PREFIX}/charts/kube-agents:{MOCK_TARGET_RELEASE_TAG}"
            create_mock_docker_binary(bin_dir, existing_images=[existing_chart_oci])

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={
                    "CI": "true",
                    "GITHUB_REPOSITORY": MOCK_DEFAULT_RELEASE_REPO,
                    "GH_TOKEN": MOCK_GH_TOKEN,
                    "GH_USER": MOCK_GH_USER,
                },
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("already exists in registry", proc.stdout)
            self.assertIn("Skipping duplicate push", proc.stdout)
            self.assertIn("Successfully signed Helm chart", proc.stdout)
            helm_log = (bin_dir / "helm.log").read_text()
            self.assertNotIn("mock helm: push", helm_log)
            cosign_log = (bin_dir / "cosign.log").read_text()
            self.assertIn("mock cosign: sign --yes", cosign_log)
        finally:
            temp_dir.cleanup()

    def test_ci_signing_failure_when_chart_already_exists(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir, fail_sign=True)
            existing_chart_oci = f"{MOCK_DEFAULT_REGISTRY_PREFIX}/charts/kube-agents:{MOCK_TARGET_RELEASE_TAG}"
            create_mock_docker_binary(bin_dir, existing_images=[existing_chart_oci])

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={
                    "CI": "true",
                    "GITHUB_REPOSITORY": MOCK_DEFAULT_RELEASE_REPO,
                    "GH_TOKEN": MOCK_GH_TOKEN,
                    "GH_USER": MOCK_GH_USER,
                },
                bin_dir=str(bin_dir),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("already exists in registry", proc.stdout)
            self.assertIn("Skipping duplicate push", proc.stdout)
            self.assertIn("Failed to sign Helm chart", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_ci_extract_chart_from_release_commit(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "HEAD"],
                env={
                    "CI": "true",
                    "GITHUB_REPOSITORY": MOCK_DEFAULT_RELEASE_REPO,
                    "GH_TOKEN": MOCK_GH_TOKEN,
                    "GH_USER": MOCK_GH_USER,
                },
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Extracting Helm chart from release commit", proc.stdout)
            self.assertIn("PUBLISHING AND SIGNING HELM CHART (OCI)", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_ci_extract_chart_from_release_commit_env_var(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [],
                env={
                    "CI": "true",
                    "RELEASE_VERSION": MOCK_TARGET_RELEASE_TAG,
                    "RELEASE_COMMIT": "HEAD",
                    "GITHUB_REPOSITORY": MOCK_DEFAULT_RELEASE_REPO,
                    "GH_TOKEN": MOCK_GH_TOKEN,
                    "GH_USER": MOCK_GH_USER,
                },
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Extracting Helm chart from release commit", proc.stdout)
            self.assertIn("PUBLISHING AND SIGNING HELM CHART (OCI)", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_dry_run_local_execution_with_release_commit(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "HEAD"],
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Extracting Helm chart from release commit", proc.stdout)
            self.assertIn("Dry-run: Helm chart packaged at", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_invalid_release_commit_fails_fast(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, "invalid-nonexistent-commit-sha-12345"],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Cannot resolve valid Git commit for Helm chart packaging", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_chart_extraction_fails_when_commit_has_no_charts_dir(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)
            create_mock_git_binary(bin_dir, fail_archive=True)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, MOCK_SAMPLE_COMMIT_SHA],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to extract charts/kube-agents from commit", proc.stderr)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()

