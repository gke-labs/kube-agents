"""Unit tests for chat_platforms — which platforms a message is posted to.

Run: python3 -m unittest agents.platform.scripts.test_chat_platforms

The invariant under test: a platform the install has enabled is never silently
dropped, and the function never resolves to nothing.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat_platforms as cp  # noqa: E402


def _config(text: str):
    """A CONFIG_PATH patch pointing at a temp config.yaml holding `text`."""
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(text)
    tmp.close()
    return mock.patch.object(cp, "CONFIG_PATH", tmp.name)


def _env(**kwargs):
    """Replace the environment with exactly `kwargs` — no host leakage."""
    return mock.patch.dict(os.environ, kwargs, clear=True)


class EnvSignalTest(unittest.TestCase):
    """A deployed pod: config.yaml settles nothing, the environment settles it all."""

    def setUp(self):
        # A missing file is the "config says nothing" case and needs no temp file.
        self.no_config = mock.patch.object(cp, "CONFIG_PATH", "/nonexistent/config.yaml")
        self.no_config.start()
        self.addCleanup(self.no_config.stop)

    def test_relay_url_enables_slack(self):
        # The regression #989 is about: SLACK_RELAY_URL is what the operator
        # actually sets on this container when spec.integration.slack.enabled.
        with _env(SLACK_RELAY_URL="http://127.0.0.1:8780"):
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_both_relay_urls_enable_both_google_chat_first(self):
        with _env(SLACK_RELAY_URL="http://x", GOOGLE_CHAT_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat", "slack"])

    def test_bot_token_absent_is_not_the_question(self):
        # The defect in the sibling resolvers: SLACK_BOT_TOKEN is a credential and
        # lives in the credential-proxy container, so a Slack install that has
        # everything except the token must still resolve to slack.
        with _env(SLACK_HOME_CHANNEL="C0123ABCD"):
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_empty_string_is_not_a_signal(self):
        # The operator renders SLACK_HOME_CHANNEL only when it is set, but an
        # empty value reaching us must not read as "Slack is on".
        with _env(SLACK_RELAY_URL="", SLACK_HOME_CHANNEL="   "):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat"])

    def test_nothing_configured_falls_back_rather_than_returning_empty(self):
        # Preserves the pre-#989 behaviour: an install this cannot read is no
        # worse off than it was, and never sends to nowhere.
        with _env():
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat"])


class ConfigFileTest(unittest.TestCase):
    def test_config_enables_without_any_env(self):
        with _config("platforms:\n  slack:\n    enabled: true\n"), _env():
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_explicit_false_overrides_an_env_signal(self):
        # Somebody turning Slack off on a pod the operator still renders
        # SLACK_RELAY_URL for. The file is the more specific statement.
        with _config("platforms:\n  slack:\n    enabled: false\n"), \
             _env(SLACK_RELAY_URL="http://x", GOOGLE_CHAT_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat"])

    def test_config_naming_one_platform_does_not_silence_the_other(self):
        # Resolution is per platform, not per source. A config that mentions only
        # Slack must not hide a Google Chat the environment knows about — the
        # short-circuit bug this module exists to avoid.
        with _config("platforms:\n  slack:\n    enabled: true\n"), \
             _env(GOOGLE_CHAT_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat", "slack"])

    def test_platforms_block_without_enabled_is_not_an_answer(self):
        # The operator-managed shape: renderConfigYAML puts `enabled` in the
        # managed scope at /etc/hermes, so this file carries the subtree with no
        # `enabled` key. Must fall through to the env, not read as False.
        with _config("platforms:\n  slack:\n    home_channel: C0123ABCD\n"), \
             _env(SLACK_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_malformed_config_degrades_to_the_env(self):
        with _config("platforms: [this is not a mapping\n"), \
             _env(SLACK_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_valid_yaml_of_the_wrong_shape_does_not_raise(self):
        # The case the test above does NOT cover: `platforms: [a, b]` is valid YAML,
        # so safe_load returns cleanly and the wrong type reaches the traversal. A
        # `.get` on a list raises outside the try, escapes _notify — which
        # cluster_agent_reconcile.main does not guard — and fails an hourly run that
        # has already created and pruned profiles, in a script contracted to exit 0.
        for text in ("platforms: [google_chat, slack]\n", "platforms: slack\n",
                     "platforms: 3\n", "platforms:\n  slack: enabled\n"):
            with self.subTest(config=text):
                with _config(text), _env(SLACK_RELAY_URL="http://x"):
                    self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_a_whole_document_of_the_wrong_shape_does_not_raise(self):
        with _config("- just\n- a list\n"), _env(GOOGLE_CHAT_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat"])

    def test_valueless_enabled_is_not_an_explicit_no(self):
        # `enabled:` with nothing after it parses to None. That is "this file does not
        # say", not "this file says no", so it must not outrank the relay URL the
        # operator rendered — the distinction _config_enabled's docstring promises.
        with _config("platforms:\n  slack:\n    enabled:\n"), \
             _env(SLACK_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["slack"])

    def test_empty_config_degrades_to_the_env(self):
        with _config(""), _env(GOOGLE_CHAT_RELAY_URL="http://x"):
            self.assertEqual(cp.enabled_chat_platforms(), ["google_chat"])


class ConfigPathContractTest(unittest.TestCase):
    def test_config_path_matches_agent_common_server(self):
        # CONFIG_PATH is deliberately repeated rather than imported — importing
        # agent_common_server for it would pull FastMCP in, which ImportCostTest below
        # exists to prevent. The copy is the cheap half of that trade; this is the
        # other half, so the two cannot silently desynchronise. Read as text rather
        # than imported, for the same reason the constant is not imported.
        here = Path(__file__).resolve().parent

        def config_path_line(module: str) -> str:
            src = (here / module).read_text().splitlines()
            return next(l for l in src if l.startswith("CONFIG_PATH ="))

        self.assertEqual(config_path_line("chat_platforms.py"),
                         config_path_line("agent_common_server.py"))


class ImportCostTest(unittest.TestCase):
    def test_importing_does_not_pull_in_the_mcp_stack(self):
        # The reason this module exists rather than a helper on
        # agent_common_server: a `no_agent` cron script must not pay for a
        # FastMCP import to find out where to send one message.
        #
        # In a subprocess, not against this interpreter's sys.modules — under
        # `unittest discover` a sibling test module has already imported FastMCP
        # long before this runs, so the in-process form asserts nothing.
        probe = (
            "import sys; import chat_platforms; "
            "print(any(m == 'mcp' or m.startswith('mcp.') for m in sys.modules))"
        )
        res = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(res.stdout.strip(), "False", res.stderr)


if __name__ == "__main__":
    unittest.main()
