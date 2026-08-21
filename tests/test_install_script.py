"""Unit tests for install.sh validation and execution routines.

Tests pure numeric SemVer (X.Y.Z) references, 40-character commit SHAs,
piped stdin (curl | bash) execution, and local script path resolution in install.sh.
"""

import os
import pathlib
import subprocess
import unittest

from tests.testing.common import (
    INSTALLER_HELP_BANNER,
    INVALID_IMMUTABLE_REFS,
    MOCK_GOOGLE_CHAT_MODE,
    VALID_IMMUTABLE_REFS,
    get_isolated_test_env,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALL_SH = _REPO_ROOT / "install.sh"


class InstallScriptValidationTest(unittest.TestCase):
    def _run_install_func(self, func_call, env=None, cwd=None):
        """Source install.sh in test mode and run the given function call."""
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
{func_call}
"""
        full_env = get_isolated_test_env(overrides=env)
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(cwd or _REPO_ROOT),
        )

    def test_validate_immutable_ref_accepts_valid_refs(self):
        for ref in VALID_IMMUTABLE_REFS:
            with self.subTest(ref=ref):
                cmd = f'validate_immutable_ref "{ref}"'
                proc = self._run_install_func(cmd)
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"install.sh: expected ref '{ref}' to be valid, stderr: {proc.stderr}",
                )

    def test_validate_immutable_ref_rejects_invalid_refs(self):
        for ref in INVALID_IMMUTABLE_REFS:
            with self.subTest(ref=ref):
                cmd = f'validate_immutable_ref "{ref}"'
                proc = self._run_install_func(cmd)
                self.assertNotEqual(
                    proc.returncode,
                    0,
                    f"install.sh: expected ref '{ref}' to be rejected",
                )

    def test_piped_stdin_executes_main(self):
        """Ensures piped curl | bash invocations execute main and do not exit early."""
        install_script_content = _INSTALL_SH.read_text()
        proc = subprocess.run(
            ["bash", "-s", "--", "--help"],
            input=install_script_content,
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, f"Piped execution failed: {proc.stderr}")
        self.assertIn(INSTALLER_HELP_BANNER, proc.stdout)

    def test_acquire_source_repo_resolves_script_directory(self):
        """Verifies acquire_source_repo finds local repo scripts via BASH_SOURCE."""
        cmd = 'out_dir=""; PARAM_ALLOW_UNVERIFIED_SOURCE=true acquire_source_repo out_dir ""; echo "DIR=$out_dir"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"DIR={_REPO_ROOT}", proc.stdout)

    def test_parse_args_google_chat_mode(self):
        """Verifies parse_args captures --google-chat-mode."""
        cmd = f'parse_args --google-chat-mode={MOCK_GOOGLE_CHAT_MODE}; echo "MODE=$PARAM_GOOGLE_CHAT_MODE"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"MODE={MOCK_GOOGLE_CHAT_MODE}", proc.stdout)

    def test_parse_args_enable_google_chat(self):
        """Verifies parse_args captures --enable-google-chat."""
        cmd = 'parse_args --enable-google-chat; echo "CHAT=$PARAM_ENABLE_GOOGLE_CHAT"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CHAT=true", proc.stdout)

    def test_gvisor_defaults_to_on(self):
        """The agent runs model-authored commands; the sandbox is the default."""
        proc = self._run_install_func('echo "GVISOR=$PARAM_ENABLE_GVISOR"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("GVISOR=true", proc.stdout)

    def test_parse_args_keeps_an_empty_gvisor_value_empty(self):
        """`--gvisor=` must reach main's validator rather than read as a default.

        main uses ${PARAM_ENABLE_GVISOR-true} for exactly this: parse_args
        leaves the empty string in place, the `:-` form would silently
        substitute it back to the default, and the validator rejects it.
        """
        cmd = 'parse_args --gvisor=; echo "GVISOR=[$PARAM_ENABLE_GVISOR]"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("GVISOR=[]", proc.stdout)

    def test_prompt_menu_defaults_to_the_first_option(self):
        """The premise the gVisor prompt's ordering rests on.

        main lists the incoming value as option 1 and treats option 2 as "the
        other one", so that answering the prompt with nothing confirms what
        `--gvisor` asked for and the `(Default)` label matches what that
        produces. It holds only while prompt_menu resolves an unanswered
        prompt to option 1; if that moves, the prompt starts inverting the
        caller's choice in silence.

        With no controlling TTY this takes prompt_read's auto-select branch
        rather than a literal empty line, but both resolve through the same
        default_val="1" that prompt_menu passes.
        """
        cmd = (
            'gvisor_choice=""; prompt_menu "Pick" "first" "second" gvisor_choice; '
            'echo "CHOICE=$gvisor_choice"'
        )
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CHOICE=1", proc.stdout)


if __name__ == "__main__":
    unittest.main()
