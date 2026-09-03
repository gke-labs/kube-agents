"""Unit tests for scripts/release/provision_environment.sh.

Tests parameter forwarding to uninstall.sh and install.sh, error handling,
and strict environment variable validation.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

import yaml

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
_PROVISION_SCRIPT = _REPO_ROOT / "scripts" / "release" / "provision_environment.sh"


class ProvisionEnvironmentTest(unittest.TestCase):
    def test_fails_when_required_env_vars_missing(self):
        """Ensures set -u aborts execution if required environment variables are absent."""
        proc = subprocess.run(
            ["bash", str(_PROVISION_SCRIPT)],
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
                    "ENABLE_PUBSUB_PLATFORM": "true",
                    "ENABLE_STOCKOUT_INVESTIGATOR": "true",
                }
            )

            proc = subprocess.run(
                ["bash", str(_PROVISION_SCRIPT)],
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
                f"--enable-pubsub-platform "
                f"--enable-stockout-investigator "
                f"--memory=hindsight"
            )
            self.assertEqual(calls[1], expected_install_call)

    def test_plugins_disabled_by_default_in_provision_environment(self):
        """Verifies plugin flags are omitted when env vars are unset."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = pathlib.Path(tmp)

            recorded_calls = tmp_dir / MOCK_CALLS_LOG
            mock_uninstall = tmp_dir / MOCK_UNINSTALL_SCRIPT
            mock_uninstall.write_text("#!/usr/bin/env bash\nexit 0\n")
            mock_uninstall.chmod(0o755)

            mock_install = tmp_dir / MOCK_INSTALL_SCRIPT
            mock_install.write_text(f'#!/usr/bin/env bash\necho "install: $*" >> "{recorded_calls}"\nexit 0\n')
            mock_install.chmod(0o755)

            env = get_isolated_test_env(
                overrides={
                    "GCP_PROJECT_ID": MOCK_GCP_PROJECT_ID,
                    "GCP_REGION": MOCK_GCP_REGION,
                    "GKE_CLUSTER_NAME": MOCK_GKE_CLUSTER_NAME,
                    "IMAGE_TAG": MOCK_IMAGE_TAG_SHA,
                }
            )

            proc = subprocess.run(
                ["bash", str(_PROVISION_SCRIPT)],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(tmp_dir),
            )

            self.assertEqual(proc.returncode, 0, f"Script failed: {proc.stderr}")
            calls = recorded_calls.read_text().splitlines()
            self.assertNotIn("--enable-pubsub-platform", calls[0])
            self.assertNotIn("--enable-stockout-investigator", calls[0])

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
                        ["bash", str(_PROVISION_SCRIPT)],
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
                ["bash", str(_PROVISION_SCRIPT)],
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
            ["bash", str(_PROVISION_SCRIPT)],
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
        self.assertIn("::error title=Environment teardown failed::", proc.stderr)
        self.assertIn("exited 1", proc.stderr)
        self.assertIn("Environment teardown failed (exit 1)", summary)
        self.assertIn("teardown blew up here", summary)
        self.assertEqual(calls[-1], MOCK_INSTALL_SUCCESS_SIGNAL)

    def test_strict_mode_stops_before_provisioning(self):
        proc, calls, summary = self._run(
            uninstall_exit=1, extra_env={"RC_TEARDOWN_STRICT": "true"}
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("Environment teardown failed (exit 1)", summary)
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

    def test_the_unprefixed_strict_name_works_on_its_own(self):
        """TEARDOWN_STRICT is the name; the RC_-prefixed one is the legacy spelling.

        The rename is only safe because both are read. Dropping the fallback
        before the GitHub environment settings are updated turns strict teardown
        off with no error, so each half is pinned separately.
        """
        proc, calls, _ = self._run(
            uninstall_exit=1, extra_env={"TEARDOWN_STRICT": "true"}
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertNotIn(MOCK_INSTALL_SUCCESS_SIGNAL, calls)

    def test_the_legacy_strict_name_still_works_on_its_own(self):
        """An environment nobody has migrated yet keeps strict teardown."""
        proc, calls, _ = self._run(
            uninstall_exit=1, extra_env={"RC_TEARDOWN_STRICT": "true"}
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertNotIn(MOCK_INSTALL_SUCCESS_SIGNAL, calls)

    def test_the_new_strict_name_wins_over_a_stale_legacy_one(self):
        """A migrated environment must not be overridden by a copy nobody deleted."""
        proc, calls, _ = self._run(
            uninstall_exit=1,
            extra_env={"TEARDOWN_STRICT": "false", "RC_TEARDOWN_STRICT": "true"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
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


class GithubMinterInputsTest(unittest.TestCase):
    """The minter half of provisioning: exit-code propagation, the PEM, and the warning.

    The exit-code case is the one that would ship silently. The script stopped ending on
    `./install.sh "${INSTALL_ARGS[@]}"` — it captures the status so the staged PEM can be
    removed first — and a `|| INSTALL_STATUS=$?` that is not re-raised turns every failed
    install into a green provisioning step.
    """

    def _run(self, overrides, install_exit=0, install_body=""):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)

        recorded_calls = tmp_dir / MOCK_CALLS_LOG
        mock_uninstall = tmp_dir / MOCK_UNINSTALL_SCRIPT
        mock_uninstall.write_text("#!/usr/bin/env bash\nexit 3\n")
        mock_uninstall.chmod(0o755)

        mock_install = tmp_dir / MOCK_INSTALL_SCRIPT
        mock_install.write_text(
            f"""#!/usr/bin/env bash
echo "{MOCK_INSTALL_SUCCESS_SIGNAL}" >> "{recorded_calls}"
{install_body}
exit {install_exit}
"""
        )
        mock_install.chmod(0o755)

        base = {
            "GCP_PROJECT_ID": MOCK_GCP_PROJECT_ID,
            "GCP_REGION": MOCK_GCP_REGION,
            "GKE_CLUSTER_NAME": MOCK_GKE_CLUSTER_NAME,
            "IMAGE_TAG": MOCK_IMAGE_TAG_SEMVER,
        }
        base.update(overrides)

        proc = subprocess.run(
            ["bash", str(_PROVISION_SCRIPT)],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=base),
            cwd=str(tmp_dir),
        )
        return proc, tmp_dir

    def test_a_failed_install_still_fails_the_step(self):
        proc, _ = self._run({}, install_exit=7)
        self.assertEqual(
            proc.returncode,
            7,
            "install.sh's exit code must survive the PEM-cleanup restructure; a green "
            f"step for a failed install is invisible. stdout:\n{proc.stdout}",
        )

    def test_a_successful_install_still_succeeds(self):
        proc, _ = self._run({}, install_exit=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_private_key_is_staged_readable_only_by_its_owner_and_then_removed(self):
        # The mock records the path and mode install.sh was handed, because the file is
        # gone by the time the script returns — which is the other half of the contract.
        proc, tmp_dir = self._run(
            {"GH_APP_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nabc\n"},
            install_body=(
                'printf "%s\\n" "${GITHUB_PEM_PATH}" > pem_path.txt\n'
                'stat -f "%Lp" "${GITHUB_PEM_PATH}" > pem_mode.txt '
                '2>/dev/null || stat -c "%a" "${GITHUB_PEM_PATH}" > pem_mode.txt\n'
                'cp "${GITHUB_PEM_PATH}" pem_contents.txt\n'
            ),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        pem_path = (tmp_dir / "pem_path.txt").read_text().strip()
        self.assertTrue(pem_path, "install.sh was not handed a GITHUB_PEM_PATH")
        self.assertEqual((tmp_dir / "pem_mode.txt").read_text().strip(), "600")
        self.assertIn("BEGIN RSA PRIVATE KEY", (tmp_dir / "pem_contents.txt").read_text())
        self.assertFalse(
            pathlib.Path(pem_path).exists(),
            "the staged private key must not outlive the install",
        )

    def test_the_staged_key_is_removed_even_when_the_install_fails(self):
        proc, tmp_dir = self._run(
            {"GH_APP_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nabc\n"},
            install_exit=1,
            install_body='printf "%s\\n" "${GITHUB_PEM_PATH}" > pem_path.txt\n',
        )
        self.assertEqual(proc.returncode, 1)
        pem_path = (tmp_dir / "pem_path.txt").read_text().strip()
        self.assertFalse(
            pathlib.Path(pem_path).exists(),
            "a failed install must not leave raw key material behind",
        )

    def test_no_pem_path_is_set_when_no_key_is_supplied(self):
        proc, tmp_dir = self._run(
            {},
            install_body='printf "[%s]\\n" "${GITHUB_PEM_PATH:-}" > pem_path.txt\n',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((tmp_dir / "pem_path.txt").read_text().strip(), "[]")

    def test_partial_minter_config_stops_the_deploy(self):
        # The environment-secret trap: org and repo resolve, the App ID arrives empty,
        # and installer_common.sh's own warning never fires because it requires all
        # three. This used to warn and provision anyway, which moved the failure to
        # test_github_token_minting_and_connectivity and turned it into an HTTP 502
        # with no named cause. It is fatal here instead.
        proc, _ = self._run({"GITHUB_ORG": "acme", "GITHUB_REPO": "infra"})
        self.assertNotEqual(
            proc.returncode, 0, "a half-configured minter must not provision an RC"
        )
        combined = proc.stdout + proc.stderr
        self.assertIn("GITHUB_APP_ID", combined)
        self.assertIn("::error", combined)

    def test_a_partial_minter_config_refuses_before_the_teardown(self):
        """The guard has to sit above `teardown_run`, not merely above install.sh.

        This script is uninstall.sh followed by install.sh. A guard placed after the
        teardown refuses an environment it has already destroyed and leaves the RC
        down until someone re-runs the pipeline — the same trap the `gke-admin`
        release note in scripts/release/README.md describes.
        """
        proc, tmp_dir = self._run(
            {"GITHUB_ORG": "acme", "GITHUB_REPO": "infra"},
            install_body='printf "ran\\n" > installer_ran.txt\n',
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(
            (tmp_dir / "installer_ran.txt").exists(),
            "the guard must fire before install.sh is invoked",
        )
        combined = proc.stdout + proc.stderr
        self.assertNotIn(
            "Tearing down existing RC environment",
            combined,
            "the guard must fire before uninstall.sh destroys the environment",
        )

    def test_a_complete_minter_config_provisions_without_complaint(self):
        proc, _ = self._run(
            {"GITHUB_ORG": "acme", "GITHUB_REPO": "infra", "GITHUB_APP_ID": "4143620"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("half-configured", proc.stdout + proc.stderr)

    def test_no_minter_config_at_all_provisions_without_complaint(self):
        # Every environment that deliberately runs without a minter — which is all of
        # them outside the RC — would otherwise be refused on every provision.
        proc, _ = self._run({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("half-configured", proc.stdout + proc.stderr)

    def test_the_gitops_names_are_accepted(self):
        """GITOPS_ORG / GITOPS_REPO are the installer's names. (see #1026)

        The old pair collided with GH_ORG / GH_REPO on these very environments,
        which name the *release* repository — so the workflow had to write
        `GITHUB_ORG: ${{ vars.GITOPS_ORG }}`, a line that reads like a bug and
        invites a "fix" that scopes a live App token at the release repo.
        """
        proc, _ = self._run(
            {"GITOPS_ORG": "acme", "GITOPS_REPO": "infra", "GITHUB_APP_ID": "4143620"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("half-configured", proc.stdout + proc.stderr)

    def test_a_half_configured_minter_is_caught_under_the_old_names_too(self):
        """The deprecation must not quietly disable this guard.

        The check moved to GITOPS_*, so an environment still exporting the old
        pair would satisfy nothing and read as "no minter configured" — which is
        an allowed state, and would provision an RC with a silently absent
        minter. Folding the old names in before the guard is what stops that.
        """
        proc, _ = self._run({"GITHUB_ORG": "acme", "GITHUB_REPO": "infra"})
        self.assertNotEqual(
            proc.returncode, 0, "a half-configured minter must not provision an RC"
        )
        self.assertIn("half-configured", proc.stdout + proc.stderr)


class LongLivedAllowlistGuardTest(GithubMinterInputsTest):
    """The rebuild path's copy of the guard render_install_env.sh runs under --strict.

    `deploy-environment.yml` offers `autopush` and `staging` in its dropdown, so
    this script is a route into a long-lived environment and not only into an
    ephemeral one. An empty allowlist is the single omission on that route that
    WIDENS access: install.sh renders `google_chat_allowed_users = []`, the
    chart's `with` omits the key, and the operator reads an absent list as
    allow-all. A rebuild would otherwise hand the whole domain an install that
    had been restricted, with nothing in the run saying so.

    Inherits the harness, not the assertions: `_run` executes the real script
    with a mock install.sh, so these drive the guard rather than reading the
    source for it.
    """

    _LONG_LIVED = {"LONG_LIVED_ENVIRONMENT": "true", "GOOGLE_CHAT_ENABLED": "true"}

    @staticmethod
    def _install_ran(tmp_dir):
        """The mock records itself in the calls log, not on stdout."""
        log = tmp_dir / MOCK_CALLS_LOG
        return log.exists() and MOCK_INSTALL_SUCCESS_SIGNAL in log.read_text()

    def test_a_long_lived_rebuild_with_no_allowlist_is_refused(self):
        proc, tmp_dir = self._run(dict(self._LONG_LIVED))
        self.assertNotEqual(
            proc.returncode,
            0,
            "a rebuild that opens the allowlist must not proceed to the teardown",
        )
        self.assertIn("Google Chat is enabled with no allowlist", proc.stdout)
        self.assertFalse(
            self._install_ran(tmp_dir),
            "the guard has to fire before install.sh, and before the teardown",
        )
        self.assertNotIn(
            "Tearing down",
            proc.stdout,
            "the guard sits above the teardown; refusing after it destroys the "
            "environment it was protecting",
        )

    def test_a_separator_only_allowlist_names_nobody_and_is_refused(self):
        """Non-empty to `-z`, empty to the installer — the same gap the renderer has.

        hcl_csv_list splits on `, \\t\\n` and drops empty items, so a list
        cleared down to a stray comma renders `[]` exactly as an unset one
        does. Both guards therefore have to measure emptiness that way.
        """
        for value in (" ", ",", ", ,", ",,"):
            with self.subTest(value=value):
                proc, _ = self._run({**self._LONG_LIVED, "ALLOWED_USERS": value})
                self.assertNotEqual(
                    proc.returncode, 0, f"{value!r} names no users but was accepted"
                )
                self.assertIn("Google Chat is enabled with no allowlist", proc.stdout)

    def test_a_real_allowlist_provisions(self):
        proc, tmp_dir = self._run(
            {**self._LONG_LIVED, "ALLOWED_USERS": "a@example.com"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._install_ran(tmp_dir))

    def test_allow_all_has_to_be_stated_and_then_provisions(self):
        proc, tmp_dir = self._run(
            {**self._LONG_LIVED, "GOOGLE_CHAT_ALLOW_ALL_USERS": "true"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._install_ran(tmp_dir))

    def test_slack_is_guarded_on_its_own_switch(self):
        proc, _ = self._run(
            {
                "LONG_LIVED_ENVIRONMENT": "true",
                "SLACK_ENABLED": "true",
                "SLACK_ALLOWED_USERS": "",
            }
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Slack is enabled with no allowlist", proc.stdout)

    def test_both_platforms_are_reported_in_one_run(self):
        proc, _ = self._run({**self._LONG_LIVED, "SLACK_ENABLED": "true"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Google Chat is enabled with no allowlist", proc.stdout)
        self.assertIn("Slack is enabled with no allowlist", proc.stdout)

    def test_enabled_means_what_it_means_to_the_installer(self):
        """A guard with its own vocabulary is a guard with its own blind spot."""
        for spelling in TRUTHY_BOOLEAN_INPUTS:
            with self.subTest(spelling=spelling):
                proc, _ = self._run(
                    {
                        "LONG_LIVED_ENVIRONMENT": "true",
                        "GOOGLE_CHAT_ENABLED": spelling,
                    }
                )
                self.assertNotEqual(
                    proc.returncode, 0, f"{spelling!r} was not read as enabled"
                )

    def test_long_lived_is_recognised_in_every_truthy_spelling(self):
        for spelling in TRUTHY_BOOLEAN_INPUTS:
            with self.subTest(spelling=spelling):
                proc, _ = self._run(
                    {
                        "LONG_LIVED_ENVIRONMENT": spelling,
                        "GOOGLE_CHAT_ENABLED": "true",
                    }
                )
                self.assertNotEqual(
                    proc.returncode, 0, f"LONG_LIVED_ENVIRONMENT={spelling!r} was ignored"
                )

    def test_an_ephemeral_environment_is_deliberately_exempt(self):
        """`rc` and `nightly` carry GOOGLE_CHAT_ENABLED=true and no allowlist today.

        They are destroyed and rebuilt every run and no real user reaches them,
        so the guard must not fire — an unconditional one would fail the RC
        pipeline on its next run rather than protect anything.
        """
        proc, tmp_dir = self._run({"GOOGLE_CHAT_ENABLED": "true"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("no allowlist", proc.stdout)
        self.assertTrue(self._install_ran(tmp_dir))

    def test_an_integration_that_is_off_needs_no_allowlist(self):
        proc, _ = self._run(
            {"LONG_LIVED_ENVIRONMENT": "true", "GOOGLE_CHAT_ENABLED": "false"}
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class DeployEnvironmentCarriesTheInstallSettingsTest(unittest.TestCase):
    """A setting the workflow never exports is unset everywhere, permanently.

    `render_install_env.sh`'s MAPPING is the contract for what reconciling a
    long-lived environment carries, and its left-hand column is the name the
    installer reads. `deploy-environment.yml` rebuilds the same two
    environments from scratch, from the same GitHub environment, and
    `provision_environment.sh` runs `./install.sh` directly — so the step's
    `env:` block is how those settings reach it. A name in the mapping and not
    in that block is not "defaulted on the rebuild"; it is a rebuild that
    installs something other than what the reconcile would have applied.

    This is the check that would have caught the gap, and the one the guard in
    provision_environment.sh depends on: a guard keyed on ALLOWED_USERS can
    only refuse an empty one if the workflow hands it the variable at all.
    """

    _WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "deploy-environment.yml"
    _RENDERER = _REPO_ROOT / "scripts" / "release" / "render_install_env.sh"
    _STEP = "Provision Environment in GCP"

    # Carried some other way, and each for a stated reason.
    _NOT_IN_ENV = {
        # The three coordinates go as explicit install.sh flags, built from
        # GCP_PROJECT_ID / GCP_REGION / GKE_CLUSTER_NAME.
        "PROJECT_ID",
        "REGION",
        "CLUSTER_NAME",
        # Derived, not mapped: the workflow passes the CI-side MEMORY_PROVIDER
        # and both paths translate it into the installer's MEMORY vocabulary.
        "MEMORY",
    }

    def _mapping_keys(self):
        """The install.env keys the renderer writes — the left column."""
        block = self._RENDERER.read_text().split('MAPPING="', 1)[1].split('"', 1)[0]
        return {
            line.split(":", 1)[0].strip()
            for line in block.splitlines()
            if ":" in line
        }

    def _provision_step_env(self):
        workflow = yaml.safe_load(self._WORKFLOW.read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if step.get("name") == self._STEP:
                    return set(step.get("env", {}))
        self.fail(f"no step named {self._STEP!r} in {self._WORKFLOW.name}")

    def test_every_mapped_setting_reaches_the_rebuild_path(self):
        missing = sorted(
            self._mapping_keys() - self._NOT_IN_ENV - self._provision_step_env()
        )
        self.assertEqual(
            missing,
            [],
            "render_install_env.sh writes these into install.env for the "
            "reconcile path, but deploy-environment.yml's provision step never "
            "puts them in the environment install.sh reads — so a "
            "destroy-and-rebuild of autopush or staging installs without them: "
            f"{missing}",
        )

    def test_the_allowlist_guard_gets_what_it_needs(self):
        """A guard keyed on a variable the workflow never sets can never fire."""
        step_env = self._provision_step_env()
        for name in (
            "ALLOWED_USERS",
            "SLACK_ENABLED",
            "SLACK_ALLOWED_USERS",
            "GOOGLE_CHAT_ALLOW_ALL_USERS",
            "SLACK_ALLOW_ALL_USERS",
            "LONG_LIVED_ENVIRONMENT",
        ):
            with self.subTest(name=name):
                self.assertIn(name, step_env)

    def test_the_guard_is_armed_for_the_long_lived_environments_only(self):
        """The dropdown's two long-lived options, and neither ephemeral one."""
        workflow = yaml.safe_load(self._WORKFLOW.read_text())
        dispatch = workflow[True]["workflow_dispatch"]
        options = dispatch["inputs"]["github_environment"]["options"]
        self.assertEqual(sorted(options), ["autopush", "nightly", "rc", "staging"])

        expression = None
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if step.get("name") == self._STEP:
                    expression = step["env"]["LONG_LIVED_ENVIRONMENT"]
        self.assertIsNotNone(expression)
        for env_name in ("autopush", "staging"):
            self.assertIn(
                f"'{env_name}'",
                expression,
                f"{env_name} is in the dropdown but does not arm the allowlist guard",
            )
        for env_name in ("rc", "nightly"):
            self.assertNotIn(f"'{env_name}'", expression)


if __name__ == "__main__":
    unittest.main()
