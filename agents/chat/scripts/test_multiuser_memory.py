#!/usr/bin/env python3
"""Tests for the multiuser_memory provider.

The provider lives at ``agents/chat/plugins/memory/multiuser_memory/`` and is
loaded by hermes-agent, so it imports modules that only exist inside the agent
image. They are stubbed below and the plugin is loaded straight from its path.

The tests deliberately live here rather than beside the plugin: Hermes'
``_load_provider_from_dir`` execs EVERY ``*.py`` in a provider directory as a
submodule, so a test file sitting there would have its module-level stub
installation run inside the real agent.

Not run in CI (only ``make validate`` runs there). Run locally:
    python3 agents/chat/scripts/test_multiuser_memory.py
"""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

PLUGIN_INIT = (
    Path(__file__).resolve().parents[1]
    / "plugins" / "memory" / "multiuser_memory" / "__init__.py"
)


def _install_hermes_stubs() -> None:
    """Minimal stand-ins for the hermes-agent modules the plugin imports."""
    memory_provider_mod = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # the real one is an ABC with this surface
        pass

    memory_provider_mod.MemoryProvider = MemoryProvider
    agent_mod = types.ModuleType("agent")
    agent_mod.memory_provider = memory_provider_mod

    registry_mod = types.ModuleType("tools.registry")
    registry_mod.tool_error = lambda msg: json.dumps({"success": False, "error": msg})
    tools_mod = types.ModuleType("tools")
    tools_mod.registry = registry_mod

    utils_mod = types.ModuleType("utils")
    utils_mod.atomic_replace = os.replace

    sys.modules.setdefault("agent", agent_mod)
    sys.modules["agent.memory_provider"] = memory_provider_mod
    sys.modules.setdefault("tools", tools_mod)
    sys.modules["tools.registry"] = registry_mod
    sys.modules["utils"] = utils_mod


def _load_plugin():
    _install_hermes_stubs()
    spec = importlib.util.spec_from_file_location("multiuser_memory_under_test", PLUGIN_INIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mum = _load_plugin()


def _set_gateway_config(config) -> None:
    """Install a ``hermes_cli.config.load_config`` stub returning *config*.

    Pass ``None`` to make the import fail, which is what happens outside the
    agent image and must be treated as "threads are shared".
    """
    if config is None:
        sys.modules.pop("hermes_cli.config", None)
        sys.modules.pop("hermes_cli", None)
        return
    config_mod = types.ModuleType("hermes_cli.config")
    config_mod.load_config = lambda: config
    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.config = config_mod
    sys.modules["hermes_cli"] = hermes_cli_mod
    sys.modules["hermes_cli.config"] = config_mod


def _expected_filename(raw_user: str) -> str:
    sanitized = "".join(c if c.isalnum() or c in "-_." else "_" for c in raw_user).strip("_")
    digest = hashlib.sha256(raw_user.encode("utf-8")).hexdigest()[:12]
    return f"{sanitized}_{digest}.md"


class MultiUserMemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        _set_gateway_config(None)
        self.addCleanup(_set_gateway_config, None)

    def provider(self, **kwargs):
        p = mum.MultiUserFileMemoryProvider()
        kwargs.setdefault("hermes_home", str(self.home))
        kwargs.setdefault("user_id", "alice@example.com")
        p.initialize("session-1", **kwargs)
        return p

    def user_files(self):
        d = self.home / "memories" / "users"
        return sorted(f.name for f in d.iterdir()) if d.is_dir() else []

    @staticmethod
    def failed(result: str) -> bool:
        return json.loads(result).get("success") is False


class TestPrivateStore(MultiUserMemoryTestCase):
    """A session that belongs to exactly one human."""

    def test_dm_session_writes_and_hydrates_private_entries(self):
        p = self.provider(chat_type="dm", thread_id="threads/abc")
        result = p.handle_tool_call(
            "multiuser_memory", {"action": "add", "target": "user", "content": "Default cluster: A"}
        )
        self.assertTrue(json.loads(result)["success"], result)
        self.assertIn("Default cluster: A", p.system_prompt_block())
        self.assertIn("User Profile Memory", p.system_prompt_block())

    def test_store_filename_is_sanitized_and_hashed(self):
        self.provider(chat_type="dm").handle_tool_call(
            "multiuser_memory", {"action": "add", "target": "user", "content": "x"}
        )
        self.assertEqual(self.user_files(), [_expected_filename("alice@example.com")])

    def test_hash_separates_identities_that_sanitize_alike(self):
        # "a@b.com" and "a_b.com" both sanitize to "a_b.com"; only the digest
        # keeps them from sharing one file. This is why AGENTS.md documents the
        # path as <sanitized-user>_<hash>.md rather than <user>.md.
        for raw in ("a@b.com", "a_b.com"):
            self.provider(chat_type="dm", user_id=raw).handle_tool_call(
                "multiuser_memory", {"action": "add", "target": "user", "content": f"from {raw}"}
            )
        self.assertEqual(len(self.user_files()), 2, self.user_files())

    def test_flat_group_message_keeps_private_store(self):
        # No thread_id: build_session_key appends the participant id, so the
        # session is still one human's.
        p = self.provider(chat_type="group")
        self.assertFalse(p._session_is_shared)
        self.assertTrue(
            json.loads(
                p.handle_tool_call(
                    "multiuser_memory", {"action": "add", "target": "user", "content": "ok"}
                )
            )["success"]
        )

    def test_cli_session_keeps_private_store(self):
        # No chat_type at all (CLI / api_server): single operator, not shared.
        self.assertFalse(self.provider(thread_id="whatever")._session_is_shared)

    def test_replace_and_remove(self):
        p = self.provider(chat_type="dm")
        call = lambda args: p.handle_tool_call("multiuser_memory", args)
        call({"action": "add", "target": "user", "content": "Default cluster: A"})
        call({
            "action": "replace", "target": "user",
            "old_content": "Default cluster: A", "new_content": "Default cluster: B",
        })
        self.assertIn("Default cluster: B", p.system_prompt_block())
        self.assertNotIn("Default cluster: A", p.system_prompt_block())
        call({"action": "remove", "target": "user", "content": "Default cluster: B"})
        self.assertNotIn("Default cluster: B", p.system_prompt_block())


class TestSharedThread(MultiUserMemoryTestCase):
    """A space thread is one session shared by every participant.

    ``agent._user_id`` is frozen when the Agent is constructed and the gateway
    caches one Agent per session key, so the provider would otherwise serve the
    first speaker's private entries to everyone else in the thread.
    """

    def shared(self, **kwargs):
        return self.provider(chat_type="group", thread_id="spaces/S/threads/T", **kwargs)

    def test_detected_as_shared(self):
        self.assertTrue(self.shared()._session_is_shared)

    def test_private_writes_are_refused(self):
        p = self.shared()
        result = p.handle_tool_call(
            "multiuser_memory", {"action": "add", "target": "user", "content": "Default cluster: A"}
        )
        self.assertTrue(self.failed(result), result)
        self.assertIn("shared thread", json.loads(result)["error"])
        self.assertEqual(self.user_files(), [])

    def test_private_reads_are_refused(self):
        # Seed a file as the same user in a DM, then re-enter via a shared
        # thread: the entries must not come back.
        self.provider(chat_type="dm").handle_tool_call(
            "multiuser_memory", {"action": "add", "target": "user", "content": "Default cluster: A"}
        )
        p = self.shared()
        result = p.handle_tool_call("multiuser_memory", {"action": "read", "target": "user"})
        self.assertTrue(self.failed(result), result)
        self.assertNotIn("Default cluster: A", result)

    def test_prompt_omits_private_block_and_explains_why(self):
        self.provider(chat_type="dm").handle_tool_call(
            "multiuser_memory", {"action": "add", "target": "user", "content": "Default cluster: A"}
        )
        block = self.shared().system_prompt_block()
        self.assertNotIn("User Profile Memory", block)
        self.assertNotIn("Default cluster: A", block)
        self.assertIn("shared thread", block)

    def test_shared_store_still_works(self):
        p = self.shared()
        result = p.handle_tool_call(
            "multiuser_memory",
            {"action": "add", "target": "memory", "content": "Standard region: us-central1"},
        )
        self.assertTrue(json.loads(result)["success"], result)
        self.assertIn("Standard region: us-central1", p.system_prompt_block())
        self.assertIn("Shared SOPs", p.system_prompt_block())

    def test_thread_sessions_per_user_restores_private_store(self):
        for config in ({"thread_sessions_per_user": True},
                       {"gateway": {"thread_sessions_per_user": True}}):
            with self.subTest(config=config):
                _set_gateway_config(config)
                self.assertFalse(self.shared()._session_is_shared)

    def test_unreadable_gateway_config_fails_closed(self):
        def explode():
            raise RuntimeError("no config here")

        config_mod = types.ModuleType("hermes_cli.config")
        config_mod.load_config = explode
        hermes_cli_mod = types.ModuleType("hermes_cli")
        hermes_cli_mod.config = config_mod
        sys.modules["hermes_cli"] = hermes_cli_mod
        sys.modules["hermes_cli.config"] = config_mod
        self.assertTrue(self.shared()._session_is_shared)


if __name__ == "__main__":
    unittest.main(verbosity=2)
