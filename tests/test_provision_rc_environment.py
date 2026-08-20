"""Unit tests for scripts/release/provision_rc_environment.sh.

Tests parameter forwarding to uninstall.sh and install.sh, error handling,
and strict environment variable validation.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    MOCK_GOOGLE_CHAT_MODE,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_CALLS_LOG,
    MOCK_CHAT_TOPIC_NAME,
    MOCK_GCP_PROJECT_ID,
    MOCK_GCP_REGION,
    MOCK_GEMINI_API_KEY,
    MOCK_GKE_CLUSTER_NAME,
    MOCK_IMAGE_TAG_SEMVER,
    MOCK_IMAGE_TAG_SHA,
    MOCK_INSTALL_SCRIPT,
    MOCK_INSTALL_SUCCESS_SIGNAL,
    MOCK_MODEL_DEFAULT_NAME,
    MOCK_MODEL_PROVIDER,
    MOCK_PERMISSION_SET,
    MOCK_REGISTRY_PREFIX,
    MOCK_UNINSTALL_FAIL_SIGNAL,
    MOCK_UNINSTALL_SCRIPT,
    MOCK_USER_PROFILE_ENABLED,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROVISION_RC_SCRIPT = _REPO_ROOT / "scripts" / "release" / "provision_rc_environment.sh"


class ProvisionRcEnvironmentTest(unittest.TestCase):
    def test_fails_when_required_env_vars_missing(self):
        """Ensures set -u aborts execution if required environment variables are absent."""
        proc = subprocess.run(
            ["bash", str(_PROVISION_RC_SCRIPT)],
            capture_output=True,
            text=True,
            env={},  # Empty environment
            cwd=str(_REPO_ROOT),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unbound variable", proc.stderr)

    def test_forwards_all_arguments_to_uninstall_and_install_scripts(self):
        """Verifies invocation sequence and comprehensive parameter forwarding to install.sh."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = pathlib.Path(tmp)

            recorded_calls = tmp_dir / MOCK_CALLS_LOG
            mock_uninstall = tmp_dir / MOCK_UNINSTALL_SCRIPT
            mock_uninstall.write_text(f"""#!/usr/bin/env bash
echo "uninstall: $*" >> "{recorded_calls}"
exit 0
""")
            mock_uninstall.chmod(0o755)

            mock_install = tmp_dir / MOCK_INSTALL_SCRIPT
            mock_install.write_text(f"""#!/usr/bin/env bash
echo "install: $*" >> "{recorded_calls}"
exit 0
""")
            mock_install.chmod(0o755)

            env = get_isolated_test_env(
                overrides={
                    "GCP_PROJECT_ID": MOCK_GCP_PROJECT_ID,
                    "GCP_REGION": MOCK_GCP_REGION,
                    "GKE_CLUSTER_NAME": MOCK_GKE_CLUSTER_NAME,
                    "IMAGE_TAG": MOCK_IMAGE_TAG_SHA,
                    "GOOGLE_CHAT_ENABLED": "true",
                    "GOOGLE_CHAT_MODE": MOCK_GOOGLE_CHAT_MODE,
                    "CHAT_TOPIC_NAME": MOCK_CHAT_TOPIC_NAME,
                    "MODEL_PROVIDER": MOCK_MODEL_PROVIDER,
                    "MODEL_DEFAULT_NAME": MOCK_MODEL_DEFAULT_NAME,
                    "GEMINI_API_KEY": MOCK_GEMINI_API_KEY,
                    "ENABLE_GVISOR": "true",
                    "PLATFORM_AGENT_PERMISSION_SET": MOCK_PERMISSION_SET,
                    "REGISTRY_PREFIX": MOCK_REGISTRY_PREFIX,
                    "MEMORY_PROVIDER": "kube_agents_memory",
                    "USER_PROFILE_ENABLED": MOCK_USER_PROFILE_ENABLED,
                }
            )

            proc = subprocess.run(
                ["bash", str(_PROVISION_RC_SCRIPT)],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(tmp_dir),
            )

            self.assertEqual(proc.returncode, 0, f"Script failed: {proc.stderr}")

            # Verify log contents
            calls = recorded_calls.read_text().splitlines()
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                calls[0],
                f"uninstall: --non-interactive -y --project-id={MOCK_GCP_PROJECT_ID} --region={MOCK_GCP_REGION} --cluster-name={MOCK_GKE_CLUSTER_NAME}",
            )
            expected_install_call = (
                f"install: --non-interactive -y "
                f"--project-id={MOCK_GCP_PROJECT_ID} "
                f"--region={MOCK_GCP_REGION} "
                f"--cluster-name={MOCK_GKE_CLUSTER_NAME} "
                f"--image-tag={MOCK_IMAGE_TAG_SHA} "
                f"--enable-google-chat "
                f"--google-chat-mode={MOCK_GOOGLE_CHAT_MODE} "
                f"--chat-topic-name={MOCK_CHAT_TOPIC_NAME} "
                f"--model-provider={MOCK_MODEL_PROVIDER} "
                f"--model-default-name={MOCK_MODEL_DEFAULT_NAME} "
                f"--gvisor=true "
                f"--permission-set={MOCK_PERMISSION_SET} "
                f"--registry-prefix={MOCK_REGISTRY_PREFIX} "
                f"--user-profile-enabled={MOCK_USER_PROFILE_ENABLED} "
                f"--memory=hindsight"
            )
            self.assertEqual(calls[1], expected_install_call)

    def test_memory_provider_mappings(self):
        """Verifies memory mode resolution for hindsight, file, and off."""
        test_cases = [
            ({"MEMORY_PROVIDER": "kube_agents_memory"}, "--memory=hindsight"),
            ({"MEMORY_PROVIDER": "hindsight"}, "--memory=hindsight"),
            ({"MEMORY_PROVIDER": "none"}, "--memory=off"),
            ({"MEMORY_PROVIDER": "off"}, "--memory=off"),
            ({"MEMORY_PROVIDER": "multiuser_memory"}, "--memory=file"),
            ({}, "--memory=file"),
        ]

        for env_overrides, expected_flag in test_cases:
            with self.subTest(env_overrides=env_overrides):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_dir = pathlib.Path(tmp)

                    recorded_calls = tmp_dir / MOCK_CALLS_LOG
                    mock_uninstall = tmp_dir / MOCK_UNINSTALL_SCRIPT
                    mock_uninstall.write_text("""#!/usr/bin/env bash
exit 0
""")
                    mock_uninstall.chmod(0o755)

                    mock_install = tmp_dir / MOCK_INSTALL_SCRIPT
                    mock_install.write_text(f"""#!/usr/bin/env bash
echo "install: $*" >> "{recorded_calls}"
exit 0
""")
                    mock_install.chmod(0o755)

                    env = get_isolated_test_env(
                        overrides={
                            "GCP_PROJECT_ID": MOCK_GCP_PROJECT_ID,
                            "GCP_REGION": MOCK_GCP_REGION,
                            "GKE_CLUSTER_NAME": MOCK_GKE_CLUSTER_NAME,
                            "IMAGE_TAG": MOCK_IMAGE_TAG_SEMVER,
                            **env_overrides,
                        }
                    )

                    proc = subprocess.run(
                        ["bash", str(_PROVISION_RC_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=env,
                        cwd=str(tmp_dir),
                    )

                    self.assertEqual(proc.returncode, 0, f"Script failed: {proc.stderr}")
                    calls = recorded_calls.read_text().splitlines()
                    self.assertIn(expected_flag, calls[0])

    def test_continues_to_install_if_uninstall_fails(self):
        """Verifies that teardown failure (e.g. cluster does not exist yet) does not abort install."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = pathlib.Path(tmp)

            recorded_calls = tmp_dir / MOCK_CALLS_LOG
            mock_uninstall = tmp_dir / MOCK_UNINSTALL_SCRIPT
            mock_uninstall.write_text(f"""#!/usr/bin/env bash
echo "{MOCK_UNINSTALL_FAIL_SIGNAL}" >> "{recorded_calls}"
exit 1
""")
            mock_uninstall.chmod(0o755)

            mock_install = tmp_dir / MOCK_INSTALL_SCRIPT
            mock_install.write_text(f"""#!/usr/bin/env bash
echo "{MOCK_INSTALL_SUCCESS_SIGNAL}" >> "{recorded_calls}"
exit 0
""")
            mock_install.chmod(0o755)

            env = get_isolated_test_env(
                overrides={
                    "GCP_PROJECT_ID": MOCK_GCP_PROJECT_ID,
                    "GCP_REGION": MOCK_GCP_REGION,
                    "GKE_CLUSTER_NAME": MOCK_GKE_CLUSTER_NAME,
                    "IMAGE_TAG": MOCK_IMAGE_TAG_SEMVER,
                }
            )

            proc = subprocess.run(
                ["bash", str(_PROVISION_RC_SCRIPT)],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(tmp_dir),
            )

            self.assertEqual(proc.returncode, 0, f"Script failed: {proc.stderr}")
            calls = recorded_calls.read_text().splitlines()
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], MOCK_UNINSTALL_FAIL_SIGNAL)
            self.assertEqual(calls[1], MOCK_INSTALL_SUCCESS_SIGNAL)


if __name__ == "__main__":
    unittest.main()
