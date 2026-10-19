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


class SlackStepsFollowTheModeTest(unittest.TestCase):
    """The Slack checklist follows the path the install's Slack is on (#3386).

    Under next without Google Chat the A2A gateway holds Slack: it does not
    acknowledge slash commands, ignores /sethome, and takes its home channel
    only from SLACK_HOME_CHANNEL. Everywhere else the Hermes listener does.
    """

    _NEXT_HOME = "C0123ABCD"

    def _run(self, install_env, applied=None):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text(install_env)
            # Only what a clean shell has: an inherited PLATFORM_AGENT_MODE or
            # SLACK_HOME_CHANNEL would decide a case the file leaves unset.
            env = {
                "PATH": os.environ["PATH"],
                "HOME": tmp,
                "TERM": "dumb",
                "VARS_FILE": str(pathlib.Path(tmp) / "vars.sh"),
                "KUBE_AGENTS_INSTALL_ENV": str(env_file),
                "REGISTRY_PREFIX": "ghcr.io/gke-labs/kube-agents",
                **(applied or {}),
            }
            proc = subprocess.run(
                ["bash", str(_REPO_ROOT / "scripts" / "installer" / "print_instructions_slack.sh"), "--dry-run"],
                capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=60,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # install.sh runs the script under `|| true`, so a failure inside it
        # shows up only as a missing checklist.
        self.assertIn("[Slack Integration Instructions]", proc.stdout, proc.stderr)
        return proc.stdout

    @staticmethod
    def _env(mode=None, gchat=False, home=""):
        lines = ["SLACK_ENABLED=true", f"GOOGLE_CHAT_ENABLED={'true' if gchat else 'false'}",
                 f"SLACK_HOME_CHANNEL={home}"]
        if mode is not None:
            lines.append(f"PLATFORM_AGENT_MODE={mode}")
        return "\n".join(lines) + "\n"

    def _assert_today(self, out):
        self.assertIn("hermes slack manifest", out)
        self.assertIn("hermes pairing approve slack", out)
        self.assertNotIn("platform-agent-a2a-gateway", out)

    def _assert_next(self, out):
        self.assertIn("#for-specmode-next-the-a2a-gateway", out)
        self.assertIn("#moving-an-existing-app-to-next", out)
        self.assertIn("kubectl logs deploy/platform-agent-a2a-gateway -c gateway -n kubeagents-system", out)
        self.assertNotIn("hermes slack manifest", out)
        self.assertNotIn("pairing", out)
        self.assertNotIn("/hermes sethome", out)

    def test_next_without_google_chat_prints_the_gateway_steps(self):
        for home in ("", self._NEXT_HOME):
            with self.subTest(home=home):
                self._assert_next(self._run(self._env(mode="next", home=home)))

    def test_next_with_no_home_channel_names_install_env_not_the_flag(self):
        # install.sh refuses --slack-home-channel when install.env assigns the
        # key, and every installer-written install.env assigns it, empty or not.
        out = self._run(self._env(mode="next"))
        self.assertIn("Set SLACK_HOME_CHANNEL in install.env", out)
        self.assertNotIn("--slack-home-channel", out)
        self.assertNotIn("Invite the app to the home channel", out)

    def test_next_with_a_home_channel_says_to_invite_the_app_there(self):
        out = self._run(self._env(mode="next", home=self._NEXT_HOME))
        self.assertIn(f"Invite the app to the home channel {self._NEXT_HOME}", out)
        self.assertNotIn("No home channel is configured", out)

    def test_next_with_google_chat_keeps_slack_on_the_today_steps(self):
        self._assert_today(self._run(self._env(mode="next", gchat=True)))

    def test_today_prints_the_listener_steps(self):
        for home in ("", self._NEXT_HOME):
            with self.subTest(home=home):
                self._assert_today(self._run(self._env(mode="today", home=home)))

    def test_install_sh_hands_the_printer_its_install_env_and_applied_values(self):
        # Without them the printer reads the checkout's install.env, which is
        # not the run's when install.env came from the working directory, and
        # misses an interview answer the run applied but did not record.
        source = (_REPO_ROOT / "install.sh").read_text()
        call = source.index('bash "${repo_dir}/scripts/installer/print_instructions_slack.sh"')
        head = source[source.rindex("\n\n", 0, call):call]
        for handoff in ('KUBE_AGENTS_INSTALL_ENV="$INSTALL_ENV_FILE"',
                        'SLACK_STEPS_PLATFORM_AGENT_MODE="$platform_agent_mode"',
                        'SLACK_STEPS_SLACK_ENABLED="$slack_enabled"',
                        'SLACK_STEPS_GOOGLE_CHAT_ENABLED="${google_chat_enabled:-false}"',
                        'SLACK_STEPS_HOME_CHANNEL="$slack_home_channel"'):
            with self.subTest(handoff=handoff):
                self.assertIn(handoff, head)

    def test_the_values_install_sh_applied_win_over_install_env(self):
        # An interactive run applies its chat-menu and home-channel answers
        # without writing them to install.env.
        file_says_both = self._env(mode="next", gchat=True)
        applied_slack_only = {"SLACK_STEPS_PLATFORM_AGENT_MODE": "next",
                              "SLACK_STEPS_SLACK_ENABLED": "true",
                              "SLACK_STEPS_GOOGLE_CHAT_ENABLED": "false",
                              "SLACK_STEPS_HOME_CHANNEL": self._NEXT_HOME}
        out = self._run(file_says_both, applied_slack_only)
        self._assert_next(out)
        self.assertIn(f"Invite the app to the home channel {self._NEXT_HOME}", out)
        applied_both = {**applied_slack_only, "SLACK_STEPS_GOOGLE_CHAT_ENABLED": "true"}
        self._assert_today(self._run(self._env(mode="next"), applied_both))
        # Slack added on a re-run over a file that records it off.
        file_says_no_slack = self._env(mode="next").replace("SLACK_ENABLED=true", "SLACK_ENABLED=false")
        self._assert_next(self._run(file_says_no_slack, applied_slack_only))

    def test_a_standalone_run_ignores_what_the_shell_exports(self):
        # install.sh's bootstrap drops an inherited PLATFORM_AGENT_MODE too.
        exported = {"PLATFORM_AGENT_MODE": "next", "GOOGLE_CHAT_ENABLED": "false"}
        self._assert_today(self._run(self._env(mode=None), exported))
        self._assert_today(self._run(self._env(mode="next", gchat=True), exported))

    def test_the_home_channel_is_trimmed_not_rewritten(self):
        out = self._run(self._env(mode="next", home=f'"  {self._NEXT_HOME} "'))
        self.assertIn(f"Invite the app to the home channel {self._NEXT_HOME},", out)

    def test_a_home_channel_that_is_not_an_id_is_named_not_invited(self):
        # The gateway turns home posts off for such a value (slack-app.md).
        for home in ('"C012 C013"', "'#gke-alerts'"):
            with self.subTest(home=home):
                out = self._run(self._env(mode="next", home=home))
                self.assertIn("which is not a channel ID", out)
                self.assertIn("Set SLACK_HOME_CHANNEL in install.env", out)
                self.assertNotIn("Invite the app to the home channel", out)

    def test_an_install_env_without_a_mode_is_today(self):
        # Such a file predates the key. Reading it as the install default would
        # turn every old install into next once that default changes.
        self._assert_today(self._run(self._env(mode=None)))


if __name__ == "__main__":
    unittest.main()
