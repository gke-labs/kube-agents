"""The chat-instruction helpers' --help and quiet exit (#3082).

`print_instructions_slack.sh --help` used to ignore the flag, print nothing
on an install with Slack off, and leave the EXIT trap's cursor escape
(`tput cnorm`, "[?12l[?25h") on a pipe. These run both scripts for real,
with the state files pointed at a temporary directory so nothing in the
checkout is written.
"""

import os
import pathlib
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_HELPERS = ("print_instructions_slack.sh", "print_instructions_gchat.sh")
# How long a printer on a terminal gets to finish before it counts as stuck on a prompt.
_TTY_RUN_SECONDS = 20


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

    def test_a_disabled_platform_names_the_file_and_key_on_stderr(self):
        # The handoff routes print these commands for a platform the run turned
        # on; a file that does not turn it on must not read as nothing to do.
        for script, key in (("print_instructions_gchat.sh", "GOOGLE_CHAT_ENABLED"),
                            ("print_instructions_slack.sh", "SLACK_ENABLED")):
            with self.subTest(script=script):
                proc = self._run(script, "--dry-run", term="dumb")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn(f"{key} is not on in ", proc.stderr)
                self.assertIn("install.env, so there is no ", proc.stderr)

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

    def test_the_namespace_the_run_used_wins_over_install_env(self):
        applied = {"SLACK_STEPS_SLACK_ENABLED": "true", "SLACK_STEPS_GOOGLE_CHAT_ENABLED": "false",
                   "SLACK_STEPS_HOME_CHANNEL": "", "SLACK_STEPS_NAMESPACE": "custom-ns"}
        out = self._run(self._env(mode="today") + "NAMESPACE=file-ns\n",
                        {**applied, "SLACK_STEPS_PLATFORM_AGENT_MODE": "today"})
        self.assertIn("-n custom-ns -- hermes pairing approve slack", out)
        out = self._run(self._env(mode="next") + "NAMESPACE=file-ns\n",
                        {**applied, "SLACK_STEPS_PLATFORM_AGENT_MODE": "next"})
        self.assertIn("-c gateway -n custom-ns", out)
        out = self._run(self._env(mode="today") + "NAMESPACE=file-ns\n",
                        {**applied, "SLACK_STEPS_PLATFORM_AGENT_MODE": "today", "SLACK_STEPS_NAMESPACE": ""})
        self.assertIn("-n file-ns -- hermes pairing approve slack", out)

    def test_next_with_google_chat_keeps_slack_on_the_today_steps(self):
        self._assert_today(self._run(self._env(mode="next", gchat=True)))

    def test_today_prints_the_listener_steps(self):
        for home in ("", self._NEXT_HOME):
            with self.subTest(home=home):
                self._assert_today(self._run(self._env(mode="today", home=home)))

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

    def test_a_standalone_run_ignores_an_exported_mode(self):
        # install.sh's bootstrap drops an inherited PLATFORM_AGENT_MODE too.
        exported = {"PLATFORM_AGENT_MODE": "next"}
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

    def test_a_standalone_run_keeps_an_exported_google_chat_the_file_does_not_set(self):
        # install.sh applies an exported GOOGLE_CHAT_ENABLED when install.env
        # does not assign the key, and Chat then holds the gateway.
        out = self._run("SLACK_ENABLED=true\nPLATFORM_AGENT_MODE=next\n", {"GOOGLE_CHAT_ENABLED": "true"})
        self._assert_today(out)

    def test_a_home_channel_padded_with_unicode_space_is_the_channel(self):
        out = self._run(self._env(mode="next", home=f'"\u00a0{self._NEXT_HOME}\u3000"'))
        self.assertIn(f"Invite the app to the home channel {self._NEXT_HOME},", out)

    def test_a_standalone_run_keeps_an_exported_home_channel_the_file_does_not_set(self):
        # install.sh applies an exported SLACK_HOME_CHANNEL when install.env
        # does not assign the key, so the checklist must not call it unset.
        out = self._run("SLACK_ENABLED=true\nGOOGLE_CHAT_ENABLED=false\nPLATFORM_AGENT_MODE=next\n",
                        {"SLACK_HOME_CHANNEL": self._NEXT_HOME})
        self.assertIn(f"Invite the app to the home channel {self._NEXT_HOME}", out)


class GchatStepsFollowTheModeTest(unittest.TestCase):
    """The Google Chat checklist follows the path the install's Chat is on.

    Under next the A2A gateway holds Chat: there is no Hermes google_chat
    platform to pair with, and its home channel comes only from
    GOOGLE_CHAT_HOME_CHANNEL. Under today the Hermes listener holds it.
    """

    _SPACE = "spaces/AAAAbcd123"
    _PROJECT_NUMBER = "123456789012"

    def _run(self, install_env, applied=None, defaults=None):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text(install_env)
            env = {
                "PATH": os.environ["PATH"],
                "HOME": tmp,
                "TERM": "dumb",
                "VARS_FILE": str(pathlib.Path(tmp) / "vars.sh"),
                "KUBE_AGENTS_INSTALL_ENV": str(env_file),
                "REGISTRY_PREFIX": "ghcr.io/gke-labs/kube-agents",
                **(applied or {}),
            }
            if defaults is not None:
                defaults_file = pathlib.Path(tmp) / "install.defaults.env"
                defaults_file.write_text(defaults)
                env["KUBE_AGENTS_INSTALL_DEFAULTS"] = str(defaults_file)
            proc = subprocess.run(
                ["bash", str(_REPO_ROOT / "scripts" / "installer" / "print_instructions_gchat.sh"), "--dry-run"],
                capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=60,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # install.sh runs the script under `|| true`, so a failure inside it
        # shows up only as a missing checklist.
        self.assertIn("[Google Chat Integration Instructions]", proc.stdout, proc.stderr)
        return proc.stdout

    @staticmethod
    def _env(mode=None, home="", allowed="a@example.com"):
        lines = ["GOOGLE_CHAT_ENABLED=true", "CHAT_TOPIC_NAME=file-topic", "CHAT_SUB_NAME=file-sub",
                 f"GOOGLE_CHAT_HOME_CHANNEL={home}", f"ALLOWED_USERS={allowed}"]
        if mode is not None:
            lines.append(f"PLATFORM_AGENT_MODE={mode}")
        return "\n".join(lines) + "\n"

    def _both(self, install_env, applied=None):
        """The run with and without PROJECT_NUMBER, which install.env never
        records, so a standalone run has none and install.sh's has one."""
        return [self._run(install_env, {**(applied or {}), **pn})
                for pn in ({}, {"PROJECT_NUMBER": self._PROJECT_NUMBER})]

    def _assert_today(self, out):
        self.assertIn("hermes pairing approve google_chat", out)
        self.assertNotIn("A2A gateway", out)

    def _assert_next(self, out):
        self.assertNotIn("pairing", out)
        self.assertNotIn("makes its space the home channel", out)
        self.assertNotIn("run /sethome there", out)
        self.assertIn("A2A gateway answers", out)

    def test_next_with_no_home_channel_prints_the_fix_in_install_env(self):
        for out in self._both(self._env(mode="next")):
            self._assert_next(out)
            self.assertIn("No home channel is configured", out)
            self.assertIn("Set GOOGLE_CHAT_HOME_CHANNEL in install.env to a space name (spaces/<id>)", out)
            self.assertNotIn("--google-chat-home-channel", out)

    def test_next_with_a_space_names_it(self):
        for out in self._both(self._env(mode="next", home=f'"  {self._SPACE} "')):
            self._assert_next(out)
            self.assertIn(f"GOOGLE_CHAT_HOME_CHANNEL is {self._SPACE}: add the app to that space", out)
            self.assertNotIn("No home channel is configured", out)

    def test_next_with_a_value_that_is_not_a_space_says_how_to_fix_it(self):
        # The gateway refuses anything but spaces/<id> and turns its notify
        # route off (a2a/gateway/notify.go).
        for home in ("AAAAbcd123", "https://mail.google.com/chat/u/0/#chat/space/AAAA", "spaces/AAAA/threads/B", "spaces/"):
            with self.subTest(home=home):
                out = self._run(self._env(mode="next", home=f"'{home}'"))
                self._assert_next(out)
                self.assertIn(f"GOOGLE_CHAT_HOME_CHANNEL is '{home}', which is not a space name", out)
                self.assertIn("no card reports back to the conversation", out)
                self.assertIn("Set GOOGLE_CHAT_HOME_CHANNEL in install.env to spaces/<id>", out)
                self.assertIn("after /room/ or /space/", out)
                self.assertNotIn("add the app to that space", out)

    def test_next_says_who_the_gateway_answers(self):
        # An empty list renders allow-all on the operator; blank items are
        # dropped first, as hcl_csv_list drops them.
        for allowed in ("", '" , "'):
            with self.subTest(allowed=allowed):
                out = self._run(self._env(mode="next", allowed=allowed))
                self.assertIn("ALLOWED_USERS is empty, so the A2A gateway answers everyone the app is visible to", out)
        out = self._run(self._env(mode="next", allowed='"a@example.com, b@example.com"'))
        # The gateway posts one refusal notice per unlisted sender
        # (a2a/gateway/gateway.go, verifySender), then stays silent.
        self.assertIn("The A2A gateway answers only a@example.com, b@example.com. Anyone else gets one notice", out)
        self.assertIn("asks an admin to add them to the allowed users, and no answers after it", out)

    def test_the_steps_are_numbered_without_a_gap(self):
        without, with_number = self._both(self._env(mode="next"))
        self.assertIn("[ ] 2. No home channel", without)
        self.assertIn("[ ] 3. The A2A gateway answers only", without)
        self.assertIn("[ ] 3. No home channel", with_number)
        self.assertIn("[ ] 4. The A2A gateway answers only", with_number)

    def test_google_chat_on_is_read_the_way_the_tfvars_generator_reads_it(self):
        # hcl_bool renders enable_google_chat with is_truthy.
        for spelling in ("yes", "1", "TRUE"):
            with self.subTest(spelling=spelling):
                env = self._env(mode="next").replace("GOOGLE_CHAT_ENABLED=true", f"GOOGLE_CHAT_ENABLED={spelling}")
                self._assert_next(self._run(env))

    def test_today_keeps_the_listener_steps(self):
        for home in ("", self._SPACE):
            with self.subTest(home=home):
                for out in self._both(self._env(mode="today", home=home)):
                    self._assert_today(out)
        out = self._run(self._env(mode="today"), {"PROJECT_NUMBER": self._PROJECT_NUMBER})
        self.assertIn("makes its space the home channel", out)

    def test_install_sh_reads_one_mode_for_the_checklists_and_the_closing_summary(self):
        # A mode set by hand on the CR can keep it from the key; the closing
        # summary must not warn about next while the checklist prints today.
        source = (_REPO_ROOT / "install.sh").read_text()
        call = source.index('\n  print_chat_setup_checklists "${PLATFORM_AGENT_MODE_AFTER_APPLY:-$platform_agent_mode}"\n')
        self.assertLess(call, source.index("print_next_home_channel_unset_summary flags\n}", call))
        self.assertNotIn('bash "${repo_dir}/scripts/installer/print_instructions_', source)

    def test_the_namespace_the_run_used_wins_over_install_env(self):
        applied = {"GCHAT_STEPS_PLATFORM_AGENT_MODE": "today", "GCHAT_STEPS_GOOGLE_CHAT_ENABLED": "true",
                   "GCHAT_STEPS_TOPIC_NAME": "t", "GCHAT_STEPS_SUB_NAME": "t-sub", "GCHAT_STEPS_NAMESPACE": "custom-ns"}
        out = self._run(self._env(mode="today") + "NAMESPACE=file-ns\n", applied)
        self.assertIn("-n custom-ns -- hermes pairing approve google_chat", out)
        out = self._run(self._env(mode="today") + "NAMESPACE=file-ns\n", {**applied, "GCHAT_STEPS_NAMESPACE": ""})
        self.assertIn("-n file-ns -- hermes pairing approve google_chat", out)

    def test_an_empty_subscription_name_is_the_one_the_generator_derives(self):
        # What install.env holds after the Day-2 menu turned Google Chat on.
        out = self._run(self._env(mode="today").replace("CHAT_SUB_NAME=file-sub", "CHAT_SUB_NAME="))
        self.assertNotIn("is missing", out)

    def test_the_values_install_sh_applied_win_over_install_env(self):
        # The after-apply mode is next while the file says today (a CR set by
        # hand), and an interview answer the file does not record.
        applied = {"GCHAT_STEPS_PLATFORM_AGENT_MODE": "next", "GCHAT_STEPS_GOOGLE_CHAT_ENABLED": "true",
                   "GCHAT_STEPS_HOME_CHANNEL": "", "GCHAT_STEPS_ALLOWED_USERS": "c@example.com",
                   "GCHAT_STEPS_TOPIC_NAME": "applied-topic", "GCHAT_STEPS_SUB_NAME": "applied-sub",
                   "PROJECT_NUMBER": self._PROJECT_NUMBER}
        out = self._run(self._env(mode="today", home=self._SPACE), applied)
        self._assert_next(out)
        self.assertIn("No home channel is configured", out)
        self.assertIn("topics/applied-topic", out)
        self.assertNotIn("file-topic", out)
        self.assertIn("answers only c@example.com", out)
        # The reverse: the file says next, the CR stays on today.
        self._assert_today(self._run(self._env(mode="next"), {**applied, "GCHAT_STEPS_PLATFORM_AGENT_MODE": "today"}))
        # Chat turned on by the run over a file that records it off.
        off = self._env(mode="next").replace("GOOGLE_CHAT_ENABLED=true", "GOOGLE_CHAT_ENABLED=false")
        self._assert_next(self._run(off, applied))

    def test_an_install_env_without_a_mode_is_today_whatever_the_default(self):
        # Such a file predates the key: reading it as the install default
        # would turn every old install into next once that default changes.
        defaults = (_REPO_ROOT / "install.defaults.env").read_text() + '\nDEFAULT_PLATFORM_AGENT_MODE="next"\n'
        self._assert_today(self._run(self._env(mode=None), defaults=defaults))

    def test_a_standalone_run_ignores_an_exported_mode(self):
        # install.sh's bootstrap drops an inherited PLATFORM_AGENT_MODE too.
        self._assert_today(self._run(self._env(mode=None), {"PLATFORM_AGENT_MODE": "next"}))

    def test_a_standalone_run_keeps_an_exported_home_channel_the_file_does_not_set(self):
        env = self._env(mode="next").replace("GOOGLE_CHAT_HOME_CHANNEL=\n", "")
        out = self._run(env, {"GOOGLE_CHAT_HOME_CHANNEL": self._SPACE})
        self.assertIn(f"is {self._SPACE}: add the app to that space", out)


class ChatSetupChecklistsTest(unittest.TestCase):
    """print_chat_setup_checklists, which install.sh calls after its apply and
    the Day-2 menu after its Save & Apply: it hands each printer what the
    apply used, and returns 0 whatever it printed, because both call it bare
    under set -e."""

    _STUB = (
        "#!/usr/bin/env bash\n"
        '{ echo "ARGS=$*"; env | grep -E "^(GCHAT_STEPS_|SLACK_STEPS_|KUBE_AGENTS_INSTALL_ENV=|PROJECT_NUMBER=)" | sort; } '
        '>"$OUT_DIR/$(basename "$0" .sh).out"\n'
        'exit "${STUB_EXIT:-0}"\n'
    )

    def _run(self, keys, mode="next", stub_exit="0"):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            installer = root / "scripts" / "installer"
            installer.mkdir(parents=True)
            for name in ("installer_common.sh", "gke_dns_endpoint.sh"):
                shutil.copy(_REPO_ROOT / "scripts" / "installer" / name, installer / name)
            shutil.copy(_REPO_ROOT / "install.defaults.env", root / "install.defaults.env")
            for name in _HELPERS:
                (installer / name).write_text(self._STUB)
            out_dir = root / "out"
            out_dir.mkdir()
            exports = "".join(f"export {k}='{v}'\n" for k, v in keys.items())
            body = (
                "set -Eeuo pipefail\n"
                "trap 'echo ERR_TRAP_FIRED' ERR\n"
                "print_info() { echo \"INFO $*\"; }\nprint_warning() { echo \"WARN $*\"; }\nprint_error() { echo \"ERR $*\"; }\n"
                f'source "{installer / "installer_common.sh"}"\n'
                f"INSTALL_ENV_FILE=/the/run/install.env\n{exports}"
                f'print_chat_setup_checklists "{mode}"\n'
                'echo "rc=$? printed=$CHAT_SETUP_CHECKLIST_PRINTED key=[$NEXT_HOME_CHANNEL_UNSET_KEY]"\n'
            )
            proc = subprocess.run(
                ["bash", "-c", body], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
                env={"PATH": os.environ["PATH"], "HOME": tmp, "OUT_DIR": str(out_dir), "STUB_EXIT": stub_exit},
            )
            printed = {p.stem: p.read_text() for p in out_dir.iterdir()}
        return proc.stdout + proc.stderr, printed

    _CHAT = {"GOOGLE_CHAT_ENABLED": "true", "GOOGLE_CHAT_HOME_CHANNEL": "", "ALLOWED_USERS": "a@example.com",
             "CHAT_TOPIC_NAME": "t", "CHAT_SUB_NAME": "t-sub", "PROJECT_NUMBER": "123456789012",
             "NAMESPACE": "custom-ns"}

    def test_google_chat_gets_what_the_apply_used(self):
        out, printed = self._run(self._CHAT)
        self.assertIn("rc=0 printed=true key=[GOOGLE_CHAT_HOME_CHANNEL]", out)
        self.assertEqual(set(printed), {"print_instructions_gchat"})
        handoff = printed["print_instructions_gchat"]
        for line in ("ARGS=--dry-run", "KUBE_AGENTS_INSTALL_ENV=/the/run/install.env",
                     "GCHAT_STEPS_PLATFORM_AGENT_MODE=next", "GCHAT_STEPS_GOOGLE_CHAT_ENABLED=true",
                     "GCHAT_STEPS_HOME_CHANNEL=\n", "GCHAT_STEPS_ALLOWED_USERS=a@example.com",
                     "GCHAT_STEPS_TOPIC_NAME=t\n", "GCHAT_STEPS_SUB_NAME=t-sub", "PROJECT_NUMBER=123456789012",
                     "GCHAT_STEPS_NAMESPACE=custom-ns"):
            with self.subTest(line=line):
                self.assertIn(line, handoff)

    def test_slack_gets_what_the_apply_used(self):
        out, printed = self._run({"SLACK_ENABLED": "true", "SLACK_HOME_CHANNEL": "C123", "GOOGLE_CHAT_ENABLED": "false",
                                  "NAMESPACE": "custom-ns"},
                                 mode="today")
        self.assertIn("rc=0 printed=true key=[]", out)
        self.assertEqual(set(printed), {"print_instructions_slack"})
        handoff = printed["print_instructions_slack"]
        for line in ("ARGS=--dry-run", "KUBE_AGENTS_INSTALL_ENV=/the/run/install.env",
                     "SLACK_STEPS_PLATFORM_AGENT_MODE=today", "SLACK_STEPS_SLACK_ENABLED=true",
                     "SLACK_STEPS_GOOGLE_CHAT_ENABLED=false", "SLACK_STEPS_HOME_CHANNEL=C123",
                     "SLACK_STEPS_NAMESPACE=custom-ns"):
            with self.subTest(line=line):
                self.assertIn(line, handoff)

    def test_a_toggle_is_read_the_way_the_tfvars_generator_reads_it(self):
        out, printed = self._run({**self._CHAT, "GOOGLE_CHAT_ENABLED": "yes"})
        self.assertIn("printed=true", out)
        self.assertIn("print_instructions_gchat", printed)

    def test_the_commands_name_the_run_install_env_as_a_word_a_shell_reads_back(self):
        # A backslash and a space survive the paste.
        env_file = r"/srv/a\b c/install.env"
        out = self._commands(env_file)
        line = next(l for l in out.splitlines() if "KUBE_AGENTS_INSTALL_ENV=" in l)
        word = line.split("KUBE_AGENTS_INSTALL_ENV=", 1)[1].split(" bash ", 1)[0]
        back = subprocess.run(["bash", "-c", f"printf %s {word}"], capture_output=True, text=True).stdout
        self.assertEqual(back, env_file)

    def _commands(self, env_file):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            installer = root / "scripts" / "installer"
            installer.mkdir(parents=True)
            for name in ("installer_common.sh", "gke_dns_endpoint.sh"):
                shutil.copy(_REPO_ROOT / "scripts" / "installer" / name, installer / name)
            shutil.copy(_REPO_ROOT / "install.defaults.env", root / "install.defaults.env")
            body = (
                "set -Eeuo pipefail\n"
                "print_info() { :; }\nprint_warning() { :; }\nprint_error() { :; }\n"
                f'source "{installer / "installer_common.sh"}"\n'
                f"INSTALL_ENV_FILE='{env_file}' GOOGLE_CHAT_ENABLED=true print_chat_checklist_commands\n"
            )
            return subprocess.run(["bash", "-c", body], capture_output=True, text=True, cwd=tmp,
                                  env={"PATH": os.environ["PATH"], "HOME": tmp}, timeout=60).stdout

    def test_no_chat_platform_returns_zero_under_the_err_trap(self):
        # install.sh's default: neither platform on. A non-zero return here
        # would fire on_error and turn a finished install into a failed one.
        out, printed = self._run({"GOOGLE_CHAT_ENABLED": "false", "SLACK_ENABLED": "false"})
        self.assertIn("rc=0 printed=false key=[]", out)
        self.assertNotIn("ERR_TRAP_FIRED", out)
        self.assertEqual(printed, {})

    def test_a_printer_that_fails_is_named_and_does_not_end_the_run(self):
        out, _ = self._run(self._CHAT, stub_exit="1")
        self.assertIn("rc=0 printed=true", out)
        self.assertNotIn("ERR_TRAP_FIRED", out)
        warning = out.index("WARN A chat setup checklist did not print in full. Show it again with:")
        self.assertLess(warning, out.index("KUBE_AGENTS_INSTALL_ENV=/the/run/install.env bash "))
        self.assertIn("/scripts/installer/print_instructions_gchat.sh", out[warning:])


class PrintersAskNothingUnderDryRunTest(unittest.TestCase):
    """print_chat_setup_checklists passes --dry-run. On a terminal, with no
    REGISTRY_PREFIX in the environment, install.env or vars.sh (the Day-2
    menu's case: install.sh's own run exports one), load_state otherwise asks
    for it and writes vars.sh."""

    _PROMPT = "Enter Container Registry Prefix"

    def _run_on_a_tty(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            install_env = pathlib.Path(tmp) / "install.env"
            install_env.write_text("PROJECT_ID=p\nGOOGLE_CHAT_ENABLED=true\nCHAT_TOPIC_NAME=t\n")
            vars_file = pathlib.Path(tmp) / "vars.sh"
            env = {"PATH": os.environ["PATH"], "HOME": tmp, "TERM": "dumb",
                   "VARS_FILE": str(vars_file), "KUBE_AGENTS_INSTALL_ENV": str(install_env)}
            script = str(_REPO_ROOT / "scripts" / "installer" / "print_instructions_gchat.sh")
            pid, fd = pty.fork()
            if pid == 0:
                os.execve("/bin/bash", ["bash", script, *args], env)
            out = b""
            deadline = time.monotonic() + _TTY_RUN_SECONDS
            try:
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([fd], [], [], 0.2)
                    if not ready:
                        continue
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    out += chunk
                    if self._PROMPT.encode() in out:
                        break
            finally:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)
                os.close(fd)
            return out.decode(errors="replace"), vars_file.exists()

    def test_dry_run_asks_nothing_and_writes_no_vars_file(self):
        out, wrote = self._run_on_a_tty("--dry-run")
        self.assertNotIn(self._PROMPT, out)
        self.assertIn("[Google Chat Integration Instructions]", out)
        self.assertFalse(wrote)

    def test_without_it_the_printer_asks(self):
        out, _ = self._run_on_a_tty()
        self.assertIn(self._PROMPT, out)


if __name__ == "__main__":
    unittest.main()
