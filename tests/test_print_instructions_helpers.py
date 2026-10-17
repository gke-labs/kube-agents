"""The chat-instruction helpers' --help and quiet exit (#3082).

`print_instructions_slack.sh --help` used to ignore the flag, print nothing
on an install with Slack off, and leave the EXIT trap's cursor escape
(`tput cnorm`, "[?12l[?25h") on a pipe. These run both scripts for real,
with the state files pointed at a temporary directory so nothing in the
checkout is written.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_HELPERS = ("print_instructions_slack.sh", "print_instructions_gchat.sh")


class ChatInstructionHelpersTest(unittest.TestCase):
    def _run(self, script, *args, term="xterm-256color"):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "PATH": os.environ["PATH"],
                "HOME": tmp,
                # A terminal type tput writes escapes for, so a guard that
                # stops checking stdout shows up here as bytes on the pipe.
                "TERM": term,
                "VARS_FILE": str(pathlib.Path(tmp) / "vars.sh"),
                "KUBE_AGENTS_INSTALL_ENV": str(pathlib.Path(tmp) / "install.env"),
                "REGISTRY_PREFIX": "ghcr.io/gke-labs/kube-agents",
                "SLACK_ENABLED": "false",
                "GOOGLE_CHAT_ENABLED": "false",
            }
            pathlib.Path(env["KUBE_AGENTS_INSTALL_ENV"]).write_text("SLACK_ENABLED=false\nGOOGLE_CHAT_ENABLED=false\n")
            return subprocess.run(
                ["bash", str(_REPO_ROOT / "scripts" / "installer" / script), *args],
                capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=60,
            )

    def test_help_prints_usage_and_exits_zero_in_any_position(self):
        for script in _HELPERS:
            for args in (("--help",), ("-h",), ("-y", "--help"), ("--dry-run", "--help"), ("--help", "-y")):
                with self.subTest(script=script, args=args):
                    proc = self._run(script, *args)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertTrue(proc.stdout.startswith(f"Usage: {script} "), proc.stdout)
                    self.assertIn("prints the checklist", proc.stdout)
                    self.assertNotIn("\x1b", proc.stdout, "usage carried a terminal escape")

    def test_a_disabled_platform_prints_nothing_on_a_pipe(self):
        # The EXIT trap's tput writes only to a terminal: on a pipe, with the
        # platform off, the run prints nothing at all. That proves the guard
        # only where an unguarded `tput cnorm` would have written something,
        # so the premise is checked first rather than assumed.
        probe = subprocess.run(["tput", "cnorm"], capture_output=True, env={**os.environ, "TERM": "xterm-256color"},
                               check=False) if shutil.which("tput") else None
        if probe is None or not probe.stdout:
            self.skipTest("tput writes nothing for TERM=xterm-256color here, so the guard is unobservable")
        for script in _HELPERS:
            with self.subTest(script=script):
                proc = self._run(script, "--dry-run")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "", repr(proc.stdout))


if __name__ == "__main__":
    unittest.main()
