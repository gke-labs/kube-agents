"""Tests for kanban_chat_notify and its applier, with the Hermes modules stubbed.

The build runs verify_kanban_chat_notify.py against the real patched tree; this
file runs on every pull request, where Hermes is not installed.
"""

import asyncio
import enum
import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _install_hermes_stubs() -> None:
    gateway = types.ModuleType("gateway")
    config = types.ModuleType("gateway.config")
    platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")

    class Platform(enum.Enum):
        API_SERVER = "api_server"
        GOOGLE_CHAT = "google_chat"
        SLACK = "slack"

    @dataclass
    class PlatformConfig:
        enabled: bool = False

    @dataclass
    class SendResult:
        success: bool
        message_id: Optional[str] = None
        error: Optional[str] = None
        raw_response: Any = None

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config, self.platform, self._message_handler = config, platform, None

        def set_message_handler(self, handler):
            self._message_handler = handler

    config.Platform, config.PlatformConfig = Platform, PlatformConfig
    base.BasePlatformAdapter, base.SendResult = BasePlatformAdapter, SendResult
    for name, module in (("gateway", gateway), ("gateway.config", config),
                         ("gateway.platforms", platforms), ("gateway.platforms.base", base)):
        sys.modules.setdefault(name, module)


_install_hermes_stubs()

import apply_kanban_chat_notify  # noqa: E402
import kanban_chat_notify  # noqa: E402
from gateway.config import Platform  # noqa: E402

ROUTED = {kanban_chat_notify.NOTIFY_PLATFORM_ENV: "google_chat"}
UNROUTED = {kanban_chat_notify.NOTIFY_PLATFORM_ENV: ""}


class _Runner:
    def _primary_message_handler(self):
        async def handler(event):
            return None
        return handler


class ResolveTest(unittest.TestCase):
    def test_a_live_adapter_always_wins(self):
        live = object()
        with mock.patch.dict(os.environ, ROUTED):
            self.assertIs(kanban_chat_notify.resolve(_Runner(), Platform.GOOGLE_CHAT, live), live)

    def test_the_routed_platform_gets_the_stand_in(self):
        runner = _Runner()
        with mock.patch.dict(os.environ, ROUTED):
            first = kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None)
            again = kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None)
        self.assertIsInstance(first, kanban_chat_notify.ChatNotifyAdapter)
        self.assertIs(first, again)
        self.assertIsNotNone(first._message_handler)

    def test_nothing_else_gets_one(self):
        with mock.patch.dict(os.environ, ROUTED):
            self.assertIsNone(kanban_chat_notify.resolve(_Runner(), Platform.SLACK, None))
        with mock.patch.dict(os.environ, UNROUTED):
            self.assertIsNone(kanban_chat_notify.resolve(_Runner(), Platform.GOOGLE_CHAT, None))

    def test_active_platforms(self):
        with mock.patch.dict(os.environ, ROUTED):
            self.assertEqual(kanban_chat_notify.active_platforms({"api_server"}), {"api_server", "google_chat"})
        with mock.patch.dict(os.environ, UNROUTED):
            self.assertEqual(kanban_chat_notify.active_platforms({"api_server"}), {"api_server"})


class _Proc:
    def __init__(self, returncode, out=b"", err=b""):
        self.returncode, self._out, self._err = returncode, out, err

    async def communicate(self):
        return self._out, self._err


class SendTest(unittest.TestCase):
    def _send(self, returncode, out=b"", err=b"", metadata=None, text="- done"):
        adapter = kanban_chat_notify.ChatNotifyAdapter(Platform.GOOGLE_CHAT, _Runner())
        calls = []

        async def fake_exec(*argv, **kwargs):
            calls.append((argv, kwargs))
            return _Proc(returncode, out, err)

        with mock.patch.object(kanban_chat_notify.asyncio, "create_subprocess_exec", fake_exec):
            result = asyncio.run(adapter.send("spaces/H", text, metadata=metadata))
        return result, calls

    def test_a_threaded_send_ends_the_flags_before_the_text(self):
        result, calls = self._send(0, b'{"message_id":"m1","thread_id":"t1"}', metadata={"thread_id": "spaces/H/threads/T"})
        argv, kwargs = calls[0]
        self.assertEqual(list(argv), ["a2a", "notify", "--platform", "google_chat", "--thread", "spaces/H/threads/T", "--", "- done"])
        self.assertIs(kwargs["stdin"], asyncio.subprocess.DEVNULL)
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "m1")

    def test_no_thread_is_a_new_thread(self):
        _, calls = self._send(0, b"{}")
        self.assertNotIn("--thread", calls[0][0])

    def test_a_refusal_is_a_failed_send(self):
        result, _ = self._send(1, err=b"a2a: notify: the gateway posted nothing")
        self.assertFalse(result.success)
        self.assertIn("posted nothing", result.error)

    def test_outcome_unknown_is_not_a_failure(self):
        result, _ = self._send(kanban_chat_notify.NOTIFY_OUTCOME_UNKNOWN)
        self.assertTrue(result.success)


# The upstream shapes the applier anchors on and wraps, as in v2026.9.14.
_NOTIFIER_SOURCE = '''
def _adapter_for_subscription(runner, platform, sub, owner_profile):
    return runner.adapters.get(platform)


class _Collector:
    def __init__(self, runner):
        self.profile_adapters = {}
        self.active_platforms = _platform_names(runner.adapters).union(
            *(_platform_names(m) for m in self.profile_adapters.values()))
'''


class ApplierTest(unittest.TestCase):
    def test_it_wraps_the_resolver_and_the_filter_once(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / apply_kanban_chat_notify.NOTIFIER_RELATIVE
            target.parent.mkdir(parents=True)
            target.write_text(_NOTIFIER_SOURCE)
            apply_kanban_chat_notify.apply(Path(root))
            patched = target.read_text()
            self.assertIn("self.active_platforms = _kage_chat_notify_active(", patched)
            self.assertIn("_kage_upstream_adapter_for_subscription = _adapter_for_subscription", patched)
            compile(patched, str(target), "exec")
            with self.assertRaises(SystemExit):
                apply_kanban_chat_notify.apply(Path(root))

    def test_a_drifted_anchor_fails_the_build(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / apply_kanban_chat_notify.NOTIFIER_RELATIVE
            target.parent.mkdir(parents=True)
            target.write_text(_NOTIFIER_SOURCE.replace("self.active_platforms =", "self.served ="))
            with self.assertRaises(SystemExit):
                apply_kanban_chat_notify.apply(Path(root))


if __name__ == "__main__":
    unittest.main()
