"""Unit tests for the legacy_slash_commands pre_gateway_dispatch rewrite.

Run: python3 -m unittest agents/chat/defaults/plugins/legacy_slash_commands/test_plugin.py

``hermes_cli.commands`` is not importable here, so the tests patch the plugin's
``_subcommand_map`` with the same shape the real ``slack_subcommand_map()``
returns (bare subcommand name -> real gateway command).
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import plugin  # noqa: E402

FAKE_MAP = {
    "sethome": "/sethome",
    "help": "/help",
    "model": "/model",
    "compact": "/compress",
}


class RewriteLegacyHermesCommandTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(plugin, "_subcommand_map", return_value=dict(FAKE_MAP))
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_known_subcommand_becomes_the_real_command(self):
        self.assertEqual(plugin.rewrite_legacy_hermes_command("/hermes sethome"), "/sethome")

    def test_subcommand_arguments_are_preserved(self):
        self.assertEqual(
            plugin.rewrite_legacy_hermes_command("/hermes model gpt-5"), "/model gpt-5"
        )

    def test_bare_hermes_shows_help(self):
        self.assertEqual(plugin.rewrite_legacy_hermes_command("/hermes"), "/help")
        self.assertEqual(plugin.rewrite_legacy_hermes_command("/hermes   "), "/help")

    def test_leading_bot_mention_is_stripped(self):
        self.assertEqual(
            plugin.rewrite_legacy_hermes_command("<@U0BKNNDJERG> /hermes sethome"), "/sethome"
        )

    def test_subcommand_is_case_insensitive(self):
        self.assertEqual(plugin.rewrite_legacy_hermes_command("/HERMES SetHome"), "/sethome")

    def test_unknown_subcommand_becomes_a_plain_question(self):
        # Upstream treats "/hermes <anything else>" as a free-form question; the
        # prefix must go, or the gateway answers "Unknown command /hermes".
        self.assertEqual(
            plugin.rewrite_legacy_hermes_command("/hermes what clusters do I have?"),
            "what clusters do I have?",
        )

    def test_non_hermes_text_is_left_alone(self):
        for text in ("/sethome", "hello", "", None, "please run /hermes sethome for me"):
            self.assertIsNone(plugin.rewrite_legacy_hermes_command(text))

    def test_other_slash_commands_are_left_alone(self):
        self.assertIsNone(plugin.rewrite_legacy_hermes_command("/help"))


class PreGatewayDispatchHookTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(plugin, "_subcommand_map", return_value=dict(FAKE_MAP))
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_hook_returns_a_rewrite_action(self):
        event = SimpleNamespace(text="/hermes sethome")
        self.assertEqual(
            plugin.handle_pre_gateway_dispatch(event=event, gateway=None, session_store=None),
            {"action": "rewrite", "text": "/sethome"},
        )

    def test_hook_is_a_no_op_for_ordinary_messages(self):
        event = SimpleNamespace(text="what clusters do I have?")
        self.assertIsNone(
            plugin.handle_pre_gateway_dispatch(event=event, gateway=None, session_store=None)
        )

    def test_hook_never_raises(self):
        self.assertIsNone(
            plugin.handle_pre_gateway_dispatch(event=None, gateway=None, session_store=None)
        )


if __name__ == "__main__":
    unittest.main()
