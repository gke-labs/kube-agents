"""Unit tests for the legacy_slash_commands pre_gateway_dispatch rewrite.

Run: python3 -m unittest agents/chat/defaults/plugins/legacy_slash_commands/test_plugin.py

``hermes_cli.commands`` is not importable here, so the tests patch the plugin's
``_subcommand_map`` with the same shape the real ``slack_subcommand_map()``
returns (bare subcommand name -> real gateway command).
"""

import os
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
    "undo": "/undo",
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


class DisableUndoCommandTest(unittest.TestCase):
    def test_undo_loses_its_slash(self):
        self.assertEqual(plugin.disable_undo_command("/undo"), "undo")
        self.assertEqual(plugin.disable_undo_command("/undo 2"), "undo 2")
        self.assertEqual(plugin.disable_undo_command("/UNDO"), "undo")

    def test_bot_mention_forms_are_covered(self):
        self.assertEqual(plugin.disable_undo_command("/undo@kage"), "undo")
        self.assertEqual(plugin.disable_undo_command("<@U0BKNNDJERG> /undo"), "undo")

    def test_other_text_is_left_alone(self):
        for text in ("/undone", "undo", "please /undo that", "/help", "", None):
            self.assertIsNone(plugin.disable_undo_command(text))

    def test_only_the_planning_agent_profile_disables_undo(self):
        # The operator names the gateway's profile in HERMES_GATEWAY_PROFILE: empty on
        # the Planning Agent, "platform" under experimental.platformFrontDoor. The
        # entrypoint's platform_is_front_door matches "platform" exactly, so every
        # other value is the chat profile (tests/test_docker_entrypoint.py pins these).
        with mock.patch.dict(os.environ, {"HERMES_GATEWAY_PROFILE": "platform"}):
            self.assertFalse(plugin.on_planning_agent_profile())
            self.assertIsNone(plugin.disable_undo_command("/undo"))
        for value in ("", "default", "Platform", "platform2", " platform"):
            with mock.patch.dict(os.environ, {"HERMES_GATEWAY_PROFILE": value}):
                self.assertTrue(plugin.on_planning_agent_profile())
                self.assertEqual(plugin.disable_undo_command("/undo"), "undo")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(plugin.on_planning_agent_profile())


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

    def test_hook_disables_undo_typed_directly_or_through_hermes(self):
        for text in ("/undo", "/hermes undo"):
            event = SimpleNamespace(text=text)
            self.assertEqual(
                plugin.handle_pre_gateway_dispatch(event=event, gateway=None, session_store=None),
                {"action": "rewrite", "text": "undo"},
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
