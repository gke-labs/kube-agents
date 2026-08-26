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
    TRUTHY_BOOLEAN_INPUTS,
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


class TeardownOutcomeTest(unittest.TestCase):
    """A teardown that tore nothing down must not read like one that did.

    The pipeline ran for weeks reinstalling on top of a surviving RC
    environment, because a single warning covered both "nothing installed yet"
    and "the teardown failed". These pin the three outcomes apart.
    """

    def _run(self, uninstall_exit, extra_env=None, uninstall_stdout="",
             trailing_newline=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)

        recorded_calls = tmp_dir / MOCK_CALLS_LOG
        summary = tmp_dir / "step_summary.md"
        summary.touch()

        mock_uninstall = tmp_dir / MOCK_UNINSTALL_SCRIPT
        # Quoted heredoc: uninstall_stdout carries backticks and HTML in the
        # fence-escape test, and an `echo "…"` would have the mock's own shell
        # interpret them before the script under test ever sees them. A heredoc
        # always terminates its last line, so trailing_newline=False switches to
        # a printf that does not — the case where the closing fence would
        # otherwise be swallowed by the log's final line.
        if trailing_newline:
            emit = f"cat <<'UNINSTALL_STDOUT_EOF'\n{uninstall_stdout}\nUNINSTALL_STDOUT_EOF"
        else:
            emit = f"printf '%s' \"$(cat <<'UNINSTALL_STDOUT_EOF'\n{uninstall_stdout}\nUNINSTALL_STDOUT_EOF\n)\""
        mock_uninstall.write_text(f"""#!/usr/bin/env bash
echo "{MOCK_UNINSTALL_FAIL_SIGNAL}" >> "{recorded_calls}"
{emit}
exit {uninstall_exit}
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
                # get_isolated_test_env strips GITHUB_*, so the job-summary
                # path only exists when a test asks for it.
                "GITHUB_STEP_SUMMARY": str(summary),
                **(extra_env or {}),
            }
        )
        proc = subprocess.run(
            ["bash", str(_PROVISION_RC_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_dir),
        )
        calls = recorded_calls.read_text().splitlines() if recorded_calls.exists() else []
        return proc, calls, summary.read_text()

    def test_nothing_to_tear_down_is_not_reported_as_a_failure(self):
        proc, calls, summary = self._run(uninstall_exit=3)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Nothing to tear down", proc.stdout)
        self.assertNotIn("::error", proc.stdout + proc.stderr)
        self.assertEqual(summary, "")
        self.assertEqual(calls[-1], MOCK_INSTALL_SUCCESS_SIGNAL)

    def test_a_failed_teardown_is_annotated_and_summarised(self):
        proc, calls, summary = self._run(
            uninstall_exit=1, uninstall_stdout="teardown blew up here"
        )
        # Still not fatal by default — see the comment on the case arm — but
        # the run carries an annotation and the job summary carries the output.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("::error title=RC teardown failed::", proc.stderr)
        self.assertIn("exited 1", proc.stderr)
        self.assertIn("RC teardown failed (exit 1)", summary)
        self.assertIn("teardown blew up here", summary)
        self.assertEqual(calls[-1], MOCK_INSTALL_SUCCESS_SIGNAL)

    def test_strict_mode_stops_before_provisioning(self):
        proc, calls, summary = self._run(
            uninstall_exit=1, extra_env={"RC_TEARDOWN_STRICT": "true"}
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("RC teardown failed (exit 1)", summary)
        self.assertNotIn(MOCK_INSTALL_SUCCESS_SIGNAL, calls)

    def test_strict_mode_accepts_what_the_installer_calls_truthy(self):
        # A human types this into a GitHub web form. Accepting only the literal
        # "true" means `1` silently keeps installing over a live environment.
        for value in TRUTHY_BOOLEAN_INPUTS:
            with self.subTest(value=value):
                proc, calls, _ = self._run(
                    uninstall_exit=1, extra_env={"RC_TEARDOWN_STRICT": value}
                )
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertNotIn(MOCK_INSTALL_SUCCESS_SIGNAL, calls)

    def test_an_unparseable_strict_value_warns_and_does_not_stop(self):
        proc, calls, _ = self._run(
            uninstall_exit=1, extra_env={"RC_TEARDOWN_STRICT": "yeah-ok"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC_TEARDOWN_STRICT not understood", proc.stderr)
        self.assertEqual(calls[-1], MOCK_INSTALL_SUCCESS_SIGNAL)

    def test_teardown_output_cannot_break_out_of_the_summary_fence(self):
        proc, _, summary = self._run(
            uninstall_exit=1,
            uninstall_stdout="oops ``` <img src=x onerror=alert(1)>",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Exactly the two fences this script writes, so nothing in the captured
        # output closed the block early.
        self.assertEqual(summary.count("```"), 2)
        self.assertIn("oops", summary)

    def test_a_log_without_a_trailing_newline_still_closes_the_fence(self):
        proc, _, summary = self._run(
            uninstall_exit=1,
            uninstall_stdout="last line, no newline",
            trailing_newline=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The closing fence must be on its own line, not concatenated onto the
        # log's final line, or the block never closes and </details> and
        # everything after it render as code.
        self.assertIn("\nlast line, no newline\n```\n", summary)


if __name__ == "__main__":
    unittest.main()
