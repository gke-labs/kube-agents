"""`confirm_action` in scripts/installer/common.sh asks unless told not to.

The destruction prompt has two legitimate bypasses, both explicit: the
`--no-confirm`/`-y` flag (or an exported `NO_CONFIRM=1`, the same intent
spelled as a variable) and `--dry-run`, under which nothing is destroyed.
`CI` is not one of them. GitHub Actions and GitLab CI set `CI=true` on their
own, so a helper that read it as "yes" let an inherited variable authorise a
delete nobody asked for (#557).

The value-prompt path is a different question and is pinned here too:
`is_non_interactive` still answers yes under `CI`, because taking a default
when nobody can type is right, and authorising a destruction is not.

Each case runs the real bash: it sources common.sh the way the scripts under
scripts/dev/ do, calls the function with stdin closed, and reads whether the
sentinel after the call was reached.
"""

import os
import pathlib
import pty
import subprocess
import tempfile
import unittest

from tests.testing.common import FALSY_BOOLEAN_INPUTS, TRUTHY_BOOLEAN_INPUTS, get_isolated_test_env

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER_DIR = REPO_ROOT / "scripts" / "installer"

#: Printed after confirm_action returns; absent when the prompt was refused.
SENTINEL = "REACHED"
#: The line confirm_action prints when it is actually asking.
PROMPT = "Are you sure you want to proceed?"
#: What confirm_action prints when stdin is not a terminal and gave no "y".
NO_TTY_HINT = "No interactive terminal is available. Re-run with --no-confirm"
#: The callers' shell options; the fixture runs under the same ones so an EOF
#: at the prompt behaves as it does in scripts/dev/.
CALLER_SHELL_OPTIONS = "set -euo pipefail"


def _script_env(state_dir, overrides):
    """A hermetic environment for a bash child that sources common.sh.

    get_isolated_test_env drops CI and the runner's variables; the two flag
    variables common.sh reads come off too, so the override, when there is
    one, is their only source. VARS_FILE points at a temp file so nothing
    touches the developer's real scripts/installer/vars.sh, and TERM=dumb
    keeps the EXIT trap's `tput cnorm` out of the captured stdout.
    """
    base = get_isolated_test_env()
    base.pop("NO_CONFIRM", None)
    base.pop("DRY_RUN", None)
    return {
        **base,
        "TERM": "dumb",
        "NO_COLOR": "1",
        "VARS_FILE": str(pathlib.Path(state_dir) / "vars.sh"),
        **overrides,
    }


def _run_confirm(script_args=(), env_overrides=None):
    """Source common.sh with `script_args`, call confirm_action, then echo SENTINEL.

    stdin is /dev/null, so a prompt that is shown reads EOF, is refused, and
    exits before the sentinel.
    """
    with tempfile.TemporaryDirectory() as state_dir:
        args = " ".join(script_args)
        body = (
            f"{CALLER_SHELL_OPTIONS}\n"
            f'source "{INSTALLER_DIR}/common.sh" {args}\n'
            f'confirm_action "DESTROY" "a:b"\n'
            f"echo {SENTINEL}\n"
        )
        return subprocess.run(
            ["bash", "-c", body],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=_script_env(state_dir, env_overrides or {}),
            cwd=str(REPO_ROOT),
        )


class ConfirmActionBypassTest(unittest.TestCase):
    def assert_prompted(self, proc, label):
        out = proc.stdout + proc.stderr
        self.assertIn(PROMPT, proc.stdout, f"{label}: no prompt was shown\n{out}")
        self.assertNotIn(SENTINEL, proc.stdout, f"{label}: the destruction line was reached\n{out}")
        self.assertNotEqual(proc.returncode, 0, f"{label}: an unanswered prompt exited 0\n{out}")
        self.assertIn(NO_TTY_HINT, out, f"{label}: no hint about the missing terminal\n{out}")

    def assert_skipped(self, proc, label):
        self.assertEqual(proc.returncode, 0, f"{label}: {proc.stdout}{proc.stderr}")
        self.assertNotIn(PROMPT, proc.stdout, f"{label}: a prompt was shown\n{proc.stdout}")
        self.assertIn(SENTINEL, proc.stdout, f"{label}: the call did not return\n{proc.stdout}{proc.stderr}")

    def test_no_flags_and_no_ci_prompts(self):
        self.assert_prompted(_run_confirm(), "no flags")

    def test_ci_true_alone_still_prompts(self):
        self.assert_prompted(_run_confirm(env_overrides={"CI": "true"}), "CI=true")

    def test_every_truthy_ci_spelling_still_prompts(self):
        for value in TRUTHY_BOOLEAN_INPUTS:
            with self.subTest(CI=value):
                self.assert_prompted(_run_confirm(env_overrides={"CI": value}), f"CI={value!r}")

    def test_a_falsy_ci_prompts_too(self):
        for value in FALSY_BOOLEAN_INPUTS:
            with self.subTest(CI=value):
                self.assert_prompted(_run_confirm(env_overrides={"CI": value}), f"CI={value!r}")

    def test_no_confirm_flag_skips_the_prompt(self):
        self.assert_skipped(_run_confirm(script_args=("--no-confirm",)), "--no-confirm")

    def test_short_yes_flag_skips_the_prompt(self):
        self.assert_skipped(_run_confirm(script_args=("-y",)), "-y")

    def test_dry_run_flag_skips_the_prompt(self):
        self.assert_skipped(_run_confirm(script_args=("--dry-run",)), "--dry-run")

    def test_no_confirm_flag_skips_with_ci_set_as_well(self):
        self.assert_skipped(
            _run_confirm(script_args=("--no-confirm",), env_overrides={"CI": "true"}),
            "--no-confirm with CI=true",
        )

    def test_exported_no_confirm_skips_the_prompt(self):
        # A purpose-named variable cannot be inherited from a CI runner by
        # accident, so it is explicit intent and stays honoured.
        self.assert_skipped(_run_confirm(env_overrides={"NO_CONFIRM": "1"}), "NO_CONFIRM=1")


class ValuePromptPathIsUnchangedTest(unittest.TestCase):
    """The fix removes CI from the destruction bypass only.

    `is_non_interactive` is also true whenever stdin is not a terminal, which
    it never is under a test runner, so a pipe would make this pass for the
    wrong reason. The child gets a pseudo-terminal on stdin instead, and the
    only thing that differs between the two calls is CI.
    """

    def _is_non_interactive(self, env_overrides):
        with tempfile.TemporaryDirectory() as state_dir:
            env = _script_env(state_dir, env_overrides)
            body = (
                f'source "{INSTALLER_DIR}/common.sh"\n'
                f"is_non_interactive && echo {SENTINEL}\n"
            )
            master, slave = pty.openpty()
            try:
                return subprocess.run(
                    ["bash", "-c", body],
                    stdin=slave,
                    capture_output=True,
                    text=True,
                    env=env,
                    cwd=str(REPO_ROOT),
                )
            finally:
                os.close(slave)
                os.close(master)

    def test_a_terminal_without_ci_is_interactive(self):
        proc = self._is_non_interactive({})
        self.assertNotIn(SENTINEL, proc.stdout, proc.stdout + proc.stderr)

    def test_ci_still_makes_value_prompts_take_defaults(self):
        proc = self._is_non_interactive({"CI": "true"})
        self.assertIn(SENTINEL, proc.stdout, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
