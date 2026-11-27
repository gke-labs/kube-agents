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
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX,
    MOCK_AR_ACCESS_TOKEN,
    MOCK_GH_TOKEN,
    MOCK_GH_USER,
    MOCK_NONEXISTENT_TAG,
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
    def _run_script(self, args, env=None, bin_dir=None, cwd=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", str(_PUBLISH_HELM_CHART_SH)] + args,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=cwd or str(_REPO_ROOT),
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
            create_mock_git_binary(bin_dir)
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
            create_mock_git_binary(bin_dir)
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
            create_mock_git_binary(bin_dir)
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
            create_mock_git_binary(bin_dir)
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
            create_mock_git_binary(bin_dir)
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
            create_mock_git_binary(bin_dir)
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
            create_mock_git_binary(bin_dir)
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
            create_mock_git_binary(bin_dir)
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
            create_mock_git_binary(bin_dir)
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
            create_mock_git_binary(bin_dir)
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
            create_mock_git_binary(bin_dir)
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
            self.assertIn("Extracting Helm chart from release tag", proc.stdout)
            self.assertIn("PUBLISHING AND SIGNING HELM CHART (OCI)", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_ci_extract_chart_from_release_commit_env_var(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_git_binary(bin_dir)
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [],
                env={
                    "CI": "true",
                    "RELEASE_VERSION": MOCK_TARGET_RELEASE_TAG,
                    "GITHUB_REPOSITORY": MOCK_DEFAULT_RELEASE_REPO,
                    "GH_TOKEN": MOCK_GH_TOKEN,
                    "GH_USER": MOCK_GH_USER,
                },
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Extracting Helm chart from release tag", proc.stdout)
            self.assertIn("PUBLISHING AND SIGNING HELM CHART (OCI)", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_dry_run_local_execution_with_release_commit(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_git_binary(bin_dir)
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Extracting Helm chart from release tag", proc.stdout)
            self.assertIn("Dry-run: Helm chart packaged at", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_publish_fails_if_tag_does_not_exist_and_no_head_commit(self):
        """Verifies publish_helm_chart errors clearly if tag does not exist and no HEAD commit exists."""
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            repo_dir = pathlib.Path(temp_dir.name) / "empty_repo"
            repo_dir.mkdir()
            subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_NONEXISTENT_TAG],
                env={"CI": "true", "GH_TOKEN": MOCK_GH_TOKEN},
                bin_dir=str(bin_dir),
                cwd=str(repo_dir),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(f"Cannot resolve valid Git commit for release tag '{MOCK_NONEXISTENT_TAG}'", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_publish_fails_if_tag_does_not_exist_even_when_head_commit_present(self):
        """Verifies publish_helm_chart fails and does NOT fall back to HEAD commit when tag is absent."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_helm_binary(bin_dir)
            create_mock_cosign_binary(bin_dir)
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                [MOCK_NONEXISTENT_TAG],
                env={"CI": "true", "GH_TOKEN": MOCK_GH_TOKEN},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(f"Cannot resolve valid Git commit for release tag '{MOCK_NONEXISTENT_TAG}'", proc.stderr)
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

    def test_mock_helm_registry_login_reads_the_password_from_stdin(self):
        """The mock must consume --password-stdin the way real helm does.

        publish_helm_chart.sh runs `printf token | helm registry login
        --password-stdin` under `set -o pipefail`. A mock that exits without
        reading stdin races the writer: when helm wins, printf takes SIGPIPE and
        the pipeline reports failure. That was an intermittent CI red in the
        CI-mode tests above. The sleep makes the race deterministic -- helm has
        certainly exited before the write unless it is waiting on stdin.
        """
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            helm_path, _ = create_mock_helm_binary(bin_dir)
            proc = subprocess.run(
                [
                    "bash",
                    "-o",
                    "pipefail",
                    "-c",
                    '(sleep 0.5; printf "%s" "$1") | "$2" registry login ghcr.io -u user --password-stdin',
                    "_",
                    MOCK_GH_TOKEN,
                    str(helm_path),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        finally:
            temp_dir.cleanup()


class PublishToEachReleaseRegistryTest(unittest.TestCase):
    """With ADDITIONAL_RELEASE_REGISTRY_PREFIX set, the one packaged chart is
    pushed to the registry prefix and then to the additional registry, and
    signed in each. Helm logs in to the additional registry with the access
    token it is handed, read from stdin."""

    _ADDITIONAL_HOST = MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX.split("/")[0]
    _ADDITIONAL_CHART = f"{MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX}/charts/kube-agents:{MOCK_TARGET_RELEASE_TAG}"

    def _publish(self, env, existing_images=()):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        bin_dir = pathlib.Path(temp_dir.name) / "bin"
        create_mock_git_binary(bin_dir)
        _, helm_log = create_mock_helm_binary(bin_dir)
        _, cosign_log = create_mock_cosign_binary(bin_dir)
        create_mock_docker_binary(bin_dir, existing_images=existing_images)
        full_env = get_isolated_test_env(
            overrides={
                "CI": "true",
                "GITHUB_REPOSITORY": MOCK_DEFAULT_RELEASE_REPO,
                "GH_TOKEN": MOCK_GH_TOKEN,
                "GH_USER": MOCK_GH_USER,
                **env,
            },
            bin_dir=str(bin_dir),
        )
        proc = subprocess.run(
            ["bash", str(_PUBLISH_HELM_CHART_SH), MOCK_TARGET_RELEASE_TAG],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(_REPO_ROOT),
        )
        helm = pathlib.Path(helm_log).read_text().splitlines()
        cosign = pathlib.Path(cosign_log).read_text().splitlines() if pathlib.Path(cosign_log).exists() else []
        return proc, helm, cosign

    def test_one_package_is_pushed_and_signed_in_both_registries(self):
        proc, helm, cosign = self._publish(
            {
                "ADDITIONAL_RELEASE_REGISTRY_PREFIX": MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX,
                "ADDITIONAL_RELEASE_REGISTRY_TOKEN": MOCK_AR_ACCESS_TOKEN,
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pushes = [line.split() for line in helm if " push " in line]
        self.assertEqual(
            [words[-1] for words in pushes],
            [f"oci://{MOCK_DEFAULT_REGISTRY_PREFIX}/charts", f"oci://{MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX}/charts"],
        )
        self.assertEqual(pushes[0][-2], pushes[1][-2], "both pushes take the one package")
        logins = [line for line in helm if "registry login" in line]
        self.assertEqual(len(logins), 2)
        self.assertIn(f"registry login ghcr.io -u {MOCK_GH_USER} --password-stdin", logins[0])
        self.assertIn(f"registry login {self._ADDITIONAL_HOST} -u oauth2accesstoken --password-stdin", logins[1])
        self.assertEqual(len([line for line in cosign if " sign " in line]), 2)
        self.assertIn(MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX + "/charts/kube-agents@sha256:", cosign[-1])
        for text in (proc.stdout, proc.stderr, "\n".join(helm), "\n".join(cosign)):
            self.assertNotIn(MOCK_AR_ACCESS_TOKEN, text)
            self.assertNotIn(MOCK_GH_TOKEN, text)

    def test_no_access_token_means_no_helm_login_to_the_additional_registry(self):
        proc, helm, _ = self._publish({"ADDITIONAL_RELEASE_REGISTRY_PREFIX": MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len([line for line in helm if "registry login" in line]), 1)
        self.assertEqual(len([line for line in helm if " push " in line]), 2)

    def test_a_chart_already_in_the_additional_registry_is_signed_and_not_pushed_again(self):
        proc, helm, cosign = self._publish(
            {
                "ADDITIONAL_RELEASE_REGISTRY_PREFIX": MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX,
                "ADDITIONAL_RELEASE_REGISTRY_TOKEN": MOCK_AR_ACCESS_TOKEN,
            },
            existing_images=[self._ADDITIONAL_CHART],
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [line.split()[-1] for line in helm if " push " in line], [f"oci://{MOCK_DEFAULT_REGISTRY_PREFIX}/charts"]
        )
        self.assertEqual(len([line for line in cosign if " sign " in line]), 2)

    def test_the_access_token_goes_to_no_registry_that_is_not_artifact_registry(self):
        """The token is a Google credential. A second registry on another host
        is pushed to on whatever login the caller already holds."""
        other = "registry.example.com/kube-agents"
        proc, helm, cosign = self._publish(
            {"ADDITIONAL_RELEASE_REGISTRY_PREFIX": other, "ADDITIONAL_RELEASE_REGISTRY_TOKEN": MOCK_AR_ACCESS_TOKEN}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        logins = [line for line in helm if "registry login" in line]
        self.assertEqual(len(logins), 1)
        self.assertIn("registry login ghcr.io", logins[0])
        self.assertEqual([line.split()[-1] for line in helm if " push " in line][-1], f"oci://{other}/charts")
        self.assertEqual(len([line for line in cosign if " sign " in line]), 2)

    def test_a_signing_failure_in_the_additional_registry_fails_the_run(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        bin_dir = pathlib.Path(temp_dir.name) / "bin"
        create_mock_git_binary(bin_dir)
        create_mock_helm_binary(bin_dir)
        create_mock_docker_binary(bin_dir)
        cosign = bin_dir / "cosign"
        cosign.write_text(
            f'#!/bin/sh\ncase "$*" in *"{MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX}"*) exit 1 ;; esac\nexit 0\n'
        )
        cosign.chmod(0o755)
        proc = subprocess.run(
            ["bash", str(_PUBLISH_HELM_CHART_SH), MOCK_TARGET_RELEASE_TAG],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "CI": "true",
                    "GITHUB_REPOSITORY": MOCK_DEFAULT_RELEASE_REPO,
                    "GH_TOKEN": MOCK_GH_TOKEN,
                    "GH_USER": MOCK_GH_USER,
                    "ADDITIONAL_RELEASE_REGISTRY_PREFIX": MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX,
                    "ADDITIONAL_RELEASE_REGISTRY_TOKEN": MOCK_AR_ACCESS_TOKEN,
                },
                bin_dir=str(bin_dir),
            ),
            cwd=str(_REPO_ROOT),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"Failed to sign Helm chart {MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX}/", proc.stderr)
        self.assertIn(f"Successfully published Helm chart {MOCK_TARGET_RELEASE_TAG} to oci://{MOCK_DEFAULT_REGISTRY_PREFIX}/charts", proc.stdout)
        self.assertNotIn(f"to oci://{MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX}/charts.", proc.stdout)

    def test_a_push_failure_in_the_additional_registry_fails_the_run(self):
        """With the chart already published to and signed in the registry prefix."""
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        bin_dir = pathlib.Path(temp_dir.name) / "bin"
        create_mock_git_binary(bin_dir)
        helm_path, _ = create_mock_helm_binary(bin_dir)
        _, cosign_log = create_mock_cosign_binary(bin_dir)
        create_mock_docker_binary(bin_dir)
        refusing = (
            "#!/bin/sh\n"
            f'if [ "$1" = "push" ] && [ "$3" = "oci://{MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX}/charts" ]; then\n'
            '  echo "mock push refused" >&2\n  exit 1\nfi\n'
        )
        helm_path.write_text(refusing + helm_path.read_text().split("\n", 1)[1])
        proc = subprocess.run(
            ["bash", str(_PUBLISH_HELM_CHART_SH), MOCK_TARGET_RELEASE_TAG],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "CI": "true",
                    "GITHUB_REPOSITORY": MOCK_DEFAULT_RELEASE_REPO,
                    "GH_TOKEN": MOCK_GH_TOKEN,
                    "GH_USER": MOCK_GH_USER,
                    "ADDITIONAL_RELEASE_REGISTRY_PREFIX": MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX,
                    "ADDITIONAL_RELEASE_REGISTRY_TOKEN": MOCK_AR_ACCESS_TOKEN,
                },
                bin_dir=str(bin_dir),
            ),
            cwd=str(_REPO_ROOT),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"Failed to push Helm chart to oci://{MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX}/charts", proc.stderr)
        self.assertIn(f"to oci://{MOCK_DEFAULT_REGISTRY_PREFIX}/charts.", proc.stdout)
        self.assertEqual(len([line for line in pathlib.Path(cosign_log).read_text().splitlines() if " sign " in line]), 1)

    def test_unset_publishes_to_the_registry_prefix_alone(self):
        for additional in ("", MOCK_DEFAULT_REGISTRY_PREFIX):
            with self.subTest(additional=additional):
                proc, helm, cosign = self._publish(
                    {"ADDITIONAL_RELEASE_REGISTRY_PREFIX": additional, "ADDITIONAL_RELEASE_REGISTRY_TOKEN": MOCK_AR_ACCESS_TOKEN}
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(len([line for line in helm if " push " in line]), 1)
                self.assertEqual(len([line for line in helm if "registry login" in line]), 1)
                self.assertEqual(len([line for line in cosign if " sign " in line]), 1)


if __name__ == "__main__":
    unittest.main()

