import abc
import dataclasses
import enum
import json
import os
import sys
import types
import unittest
import urllib.error
from typing import Any
from unittest import mock

RELAY_URL = "http://127.0.0.1:8642"


def _register_fake_modules():
    """Stand in for the Hermes gateway with the symbol names and shapes the
    patch imports (``gateway.config``, ``gateway.platforms.base`` and
    ``gateway.platform_registry`` as of hermes-agent v2026.9.14), so the
    adapter under test is built on a base, not on a stub of its own."""
    if "gateway.platform_registry" in sys.modules:
        return
    gateway = types.ModuleType("gateway")
    config_mod = types.ModuleType("gateway.config")
    platforms_mod = types.ModuleType("gateway.platforms")
    base_mod = types.ModuleType("gateway.platforms.base")
    registry_mod = types.ModuleType("gateway.platform_registry")

    @dataclasses.dataclass
    class PlatformEntry:
        name: str
        label: str = ""
        adapter_factory: object = None
        check_fn: object = None
        validate_config: object = None
        is_connected: object = None
        standalone_sender_fn: object = None
        source: str = "plugin"
        max_message_length: int = 0
        cron_deliver_env_var: str = ""
        allowed_users_env: str = ""
        allow_all_env: str = ""

    class PlatformRegistry:
        """The maps and lookups of the upstream registry: a global map, a
        per-profile scoped map that ``get()`` consults first, and deferred
        loaders a plugin's lazy registration leaves behind."""

        def __init__(self):
            self._entries = {}
            self._scoped_entries = {}
            self._deferred = {}
            self._scoped_deferred = {}

        def create_adapter(self, name, *args, **kwargs):
            return None

        def register(self, entry, *, scope=None):
            if scope is None:
                self._entries[entry.name] = entry
            else:
                self._scoped_entries.setdefault(scope, {})[entry.name] = entry

        def get(self, name):
            for scoped in self._scoped_entries.values():
                if name in scoped:
                    return scoped[name]
            return self._entries.get(name)

        def is_registered(self, name):
            return self.get(name) is not None

    registry_mod.PlatformEntry = PlatformEntry
    registry_mod.PlatformRegistry = PlatformRegistry
    registry_mod.platform_registry = PlatformRegistry()

    class Platform(enum.Enum):
        SLACK = "slack"

        @classmethod
        def _missing_(cls, value):
            # Upstream admits a name only once the registry knows it.
            if isinstance(value, str) and registry_mod.platform_registry.is_registered(value):
                pseudo = object.__new__(cls)
                pseudo._value_ = value
                pseudo._name_ = value.upper()
                cls._value2member_map_[value] = pseudo
                return pseudo
            return None

    @dataclasses.dataclass
    class PlatformConfig:
        enabled: bool = False
        typing_status_text: str | None = None
        extra: dict = dataclasses.field(default_factory=dict)

    class MessageType(enum.Enum):
        TEXT = "text"

    @dataclasses.dataclass
    class MessageEvent:
        text: str
        message_type: MessageType = MessageType.TEXT
        user_id: str | None = None
        user_name: str | None = None
        source: Any = None
        raw_message: Any = None
        message_id: str | None = None

    @dataclasses.dataclass
    class SendResult:
        success: bool
        message_id: str | None = None
        error: str | None = None
        raw_response: Any = None

    @dataclasses.dataclass
    class SessionSource:
        chat_id: str
        chat_name: str | None = None
        chat_type: str = "dm"
        user_id: str | None = None
        user_name: str | None = None
        thread_id: str | None = None

    class BasePlatformAdapter(abc.ABC):
        """The abstract set and call signatures of the upstream base, so an
        adapter that cannot be instantiated there cannot be instantiated here."""

        def __init__(self, config, platform):
            self.config = config
            self.platform = platform
            self._message_handler = None

        @abc.abstractmethod
        async def connect(self, *, is_reconnect=False):
            ...

        @abc.abstractmethod
        async def disconnect(self):
            ...

        @abc.abstractmethod
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            ...

        @abc.abstractmethod
        async def get_chat_info(self, chat_id):
            ...

        async def send_typing(self, chat_id, metadata=None):
            return None

        def set_message_handler(self, handler):
            self._message_handler = handler

        def build_source(self, chat_id, chat_name=None, chat_type="dm", user_id=None,
                         user_name=None, thread_id=None):
            return SessionSource(chat_id, chat_name, chat_type, user_id, user_name, thread_id)

        async def handle_message(self, event):
            if self._message_handler is not None:
                return await self._message_handler(event)
            return None

    config_mod.Platform = Platform
    config_mod.PlatformConfig = PlatformConfig
    base_mod.BasePlatformAdapter = BasePlatformAdapter
    base_mod.MessageEvent = MessageEvent
    base_mod.MessageType = MessageType
    base_mod.SendResult = SendResult
    platforms_mod.base = base_mod
    gateway.config = config_mod
    gateway.platforms = platforms_mod
    gateway.platform_registry = registry_mod
    for name, module in (
        ("gateway", gateway),
        ("gateway.config", config_mod),
        ("gateway.platforms", platforms_mod),
        ("gateway.platforms.base", base_mod),
        ("gateway.platform_registry", registry_mod),
    ):
        sys.modules[name] = module


_register_fake_modules()

import teams_relay_patch
from teams_relay_patch import (
    extract_activity_tenant_id,
    format_teams_activity_payload,
    is_tenant_allowed,
    is_trusted_service_url,
    is_user_allowed,
    markdown_to_adaptive_card,
    parse_allowed_users,
)


class TestTeamsRelayPatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The adapter reads TEAMS_* from the process environment; a developer's
        # own tenant or allowlist must not reach the code under test.
        saved = {k: v for k, v in os.environ.items() if k.startswith("TEAMS_")}
        for key in saved:
            os.environ.pop(key, None)
        os.environ["TEAMS_RELAY_URL"] = RELAY_URL

        def restore():
            for key in [k for k in os.environ if k.startswith("TEAMS_")]:
                os.environ.pop(key, None)
            os.environ.update(saved)

        self.addCleanup(restore)

    def test_parse_allowed_users(self):
        raw = "user1@example.com, USER2@example.com , 1234-abcd-5678 "
        parsed = parse_allowed_users(raw)
        self.assertEqual(
            parsed,
            {"user1@example.com", "user2@example.com", "1234-abcd-5678"},
        )
        self.assertEqual(parse_allowed_users(""), set())

    def test_is_user_allowed_matches_id_aad_and_upn_but_never_display_name(self):
        allowed = {"user1@example.com", "aad-obj-id-123", "29:teams-id-99"}
        # Matches Teams ID
        self.assertTrue(
            is_user_allowed("29:teams-id-99", "Random Name", None, allowed)
        )
        # Matches Entra Object ID (aadObjectId)
        self.assertTrue(
            is_user_allowed(
                "29:other",
                "Random Name",
                None,
                allowed,
                sender_aad_id="AAD-OBJ-ID-123",
            )
        )
        # Matches UPN / email
        self.assertTrue(
            is_user_allowed("29:other", "Random Name", "User1@Example.com", allowed)
        )
        # Display name matching an allowlisted UPN or AAD ID MUST be rejected!
        self.assertFalse(
            is_user_allowed(
                "29:attacker",
                "user1@example.com",
                "attacker@evil.example",
                allowed,
                sender_aad_id="aad-attacker-999",
            )
        )
        self.assertFalse(
            is_user_allowed(
                "29:attacker",
                "aad-obj-id-123",
                None,
                allowed,
            )
        )
        # allow_all mode
        self.assertTrue(
            is_user_allowed(
                "unauthorized-id", "User Two", "user2@example.com", allowed, allow_all=True
            )
        )

    def test_is_tenant_allowed_and_extraction(self):
        self.assertTrue(is_tenant_allowed("tenant-abc-123", "tenant-abc-123"))
        self.assertTrue(is_tenant_allowed("TENANT-ABC-123", "tenant-abc-123"))
        self.assertFalse(is_tenant_allowed("tenant-xyz-999", "tenant-abc-123"))
        self.assertFalse(is_tenant_allowed(None, "tenant-abc-123"))
        self.assertTrue(is_tenant_allowed("any-tenant", None))
        self.assertTrue(is_tenant_allowed(None, None))

        # Prefers channelData.tenant.id, falls back to conversation.tenantId, ignores top-level tenantId
        self.assertEqual(
            extract_activity_tenant_id(
                {"channelData": {"tenant": {"id": "t-channel"}}, "conversation": {"tenantId": "t-conv"}}
            ),
            "t-channel",
        )
        self.assertEqual(
            extract_activity_tenant_id(
                {"conversation": {"id": "c1", "tenantId": "t-conv"}}
            ),
            "t-conv",
        )
        self.assertIsNone(
            extract_activity_tenant_id({"tenantId": "top-level-ignored"})
        )

    def test_is_trusted_service_url(self):
        self.assertTrue(is_trusted_service_url("https://smba.trafficmanager.net/teams/"))
        self.assertTrue(is_trusted_service_url("https://api.botframework.com/"))
        self.assertTrue(is_trusted_service_url("https://us.api.botframework.com/"))
        self.assertFalse(is_trusted_service_url("https://attacker.trafficmanager.net/"))
        self.assertFalse(is_trusted_service_url("http://smba.trafficmanager.net/teams/"))
        self.assertFalse(is_trusted_service_url("https://evil.example.com/"))

    def test_markdown_to_adaptive_card(self):
        text = '# Fleet Audit Report\n\n- Finding 1: cgroup pressure\n- Finding 2: missing tag\n\n```json\n{"ok": true}\n```\n\nSummary complete.'
        card = markdown_to_adaptive_card(text, title="Audit Results")
        self.assertEqual(card["type"], "AdaptiveCard")
        self.assertEqual(card["version"], "1.5")
        body = card["body"]
        self.assertTrue(len(body) >= 4)
        self.assertEqual(body[0]["type"], "TextBlock")
        self.assertEqual(body[0]["text"], "Audit Results")
        self.assertEqual(body[1]["text"], "Fleet Audit Report")
        # A hash with no space after it is text, not a header.
        card = markdown_to_adaptive_card("#1040 at line start\n#!/bin/sh\n## Real")
        self.assertEqual(
            [(b["text"], b.get("size")) for b in card["body"]],
            [("#1040 at line start", None), ("#!/bin/sh", None), ("Real", "Medium")],
        )

    def test_format_teams_activity_payload(self):
        payload = format_teams_activity_payload(
            "# Title\nDetails",
            use_adaptive_cards=True,
            title="Card Title",
            actions=[{"type": "Action.Submit", "title": "Remediate"}],
        )
        self.assertEqual(payload["type"], "message")
        self.assertIn("attachments", payload)
        self.assertEqual(
            payload["attachments"][0]["contentType"],
            "application/vnd.microsoft.card.adaptive",
        )
        self.assertEqual(len(payload["attachments"][0]["content"]["actions"]), 1)

        plain_payload = format_teams_activity_payload(
            "Plain notification", use_adaptive_cards=False
        )
        self.assertEqual(plain_payload["type"], "message")
        self.assertEqual(plain_payload["text"], "Plain notification")
        self.assertNotIn("attachments", plain_payload)

    def test_install_noop_without_relay_url(self):
        os.environ["TEAMS_RELAY_URL"] = ""
        teams_relay_patch.install()

    async def test_teams_relay_adapter_lifecycle_and_message_processing(self):
        from gateway.platform_registry import PlatformRegistry, platform_registry

        teams_relay_patch.install()
        self.assertIsNotNone(platform_registry.get("teams"))

        registry = PlatformRegistry()
        adapter = registry.create_adapter(
            "teams", config={"extra": {"adaptive_cards": True}}
        )
        self.assertIsNotNone(adapter)
        # Built on the gateway's base with the platform the registry admitted.
        from gateway.config import Platform

        self.assertIs(adapter.platform, Platform("teams"))
        self.assertEqual(adapter.platform.value, "teams")

        handled_messages = []

        async def fake_handler(chat_id, text, metadata):
            handled_messages.append((chat_id, text, metadata))

        adapter.set_handler(fake_handler)
        adapter.allow_all_users = False
        adapter.allowed_users = {"aad-user-456"}

        activity = {
            "type": "message",
            "id": "act-12345",
            "text": "run fleet audit",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "conversation": {"id": "conv-9876", "tenantId": "tenant-1"},
            "from": {"id": "29:user-456", "aadObjectId": "aad-user-456", "name": "Admin"},
        }

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"status": "ok"}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            await adapter._process_activity(activity)

        self.assertEqual(len(handled_messages), 1)
        conv_id, text, meta = handled_messages[0]
        self.assertEqual(conv_id, "conv-9876")
        self.assertEqual(text, "run fleet audit")
        self.assertEqual(meta["activityId"], "act-12345")
        self.assertEqual(meta["tenantId"], "tenant-1")

    async def test_base_platform_adapter_handle_message_and_platform_config_object(self):
        from gateway.config import PlatformConfig
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        cfg = PlatformConfig(
            extra={"adaptive_cards": False}, typing_status_text="Custom thinking…"
        )
        adapter = registry.create_adapter("teams", cfg)
        self.assertFalse(adapter.adaptive_cards)
        self.assertEqual(adapter.typing_status_text, "Custom thinking…")
        adapter.allow_all_users = True

        received_events = []

        async def on_message_event(event):
            received_events.append(event)

        adapter.set_message_handler(on_message_event)

        activity = {
            "type": "message",
            "id": "act-777",
            "text": "check pods",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "conversation": {"id": "conv-777"},
            "from": {"id": "29:u1", "aadObjectId": "aad-u1", "name": "Alice"},
            "channelData": {"tenant": {"id": "tenant-1"}},
        }

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"status": "ok"}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            await adapter._process_activity(activity)

        self.assertEqual(len(received_events), 1)
        self.assertEqual(received_events[0].text, "check pods")
        self.assertEqual(received_events[0].message_id, "act-777")
        self.assertEqual(received_events[0].source.chat_id, "conv-777")
        self.assertEqual(received_events[0].source.user_id, "aad-u1")

    async def test_send_and_send_typing(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"id": "msg-1"}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            res = await adapter.send(
                chat_id="conv-123",
                content="Fleet audit passing",
                thread_id="reply-thread-1",
                metadata={
                    "title": "Audit Pass",
                    "serviceUrl": "https://smba.trafficmanager.net/teams/",
                },
            )
            self.assertTrue(res.success)
            self.assertEqual(res.message_id, "msg-1")
            self.assertEqual(res.raw_response, {"id": "msg-1"})

            await adapter.send_typing(
                "conv-123", metadata={"serviceUrl": "https://smba.trafficmanager.net/teams/"}
            )
            self.assertEqual(
                await adapter.get_chat_info("19:abc@thread.tacv2"),
                {"name": "19:abc@thread.tacv2", "type": "channel", "chat_id": "19:abc@thread.tacv2"},
            )

    async def test_bundled_teams_entry_is_taken_over(self):
        """A ``teams`` entry another module registered first, in the scoped
        map ``get()`` consults first, ends up pointing at the relay adapter."""
        from gateway.platform_registry import PlatformEntry, platform_registry

        foreign = PlatformEntry(
            name="teams",
            label="bundled",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: False,
            is_connected=lambda _cfg=None: False,
            standalone_sender_fn=None,
        )
        platform_registry.register(foreign, scope="profile-a")
        self.addCleanup(platform_registry._scoped_entries.pop, "profile-a", None)

        teams_relay_patch.install()

        served = platform_registry.get("teams")
        self.assertIs(served, foreign)
        self.assertTrue(served.check_fn())
        self.assertTrue(served.is_connected())
        self.assertEqual(served.max_message_length, 16000)
        self.assertIsNotNone(served.standalone_sender_fn)
        self.assertEqual(served.standalone_sender_fn.__module__, "teams_relay_patch")

        # And an entry registered after install() is patched on the way in.
        late = PlatformEntry(name="teams", label="late", check_fn=lambda: False)
        platform_registry.register(late, scope="profile-b")
        self.addCleanup(platform_registry._scoped_entries.pop, "profile-b", None)
        self.assertTrue(late.check_fn())
        self.assertEqual(late.max_message_length, 16000)

    async def test_standalone_send_returns_the_cause_and_never_raises(self):
        """The registry's cron path expects ``success`` or ``error`` back; the
        broker's ``teams`` object is what names the cause."""
        import io
        from gateway.platform_registry import platform_registry

        teams_relay_patch.install()
        sender = platform_registry.get("teams").standalone_sender_fn
        body = json.dumps({"error": "Teams operation failed", "detail": "HTTP 403: BotNotInConversationRoster",
                           "teams": {"status": 403, "code": "BotNotInConversationRoster", "message": "not in roster"}}).encode()
        err = urllib.error.HTTPError("http://127.0.0.1:8642/v1/chat/teams/api", 502, "Bad Gateway", {}, io.BytesIO(body))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            res = await sender(None, "conv-1", "hello")
        self.assertIn("BotNotInConversationRoster", res["error"])
        self.assertNotIn("success", res)

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            self.assertEqual(await sender(None, "conv-1", "   "), {"success": True, "platform": "teams", "skipped": "empty_text"})
            self.assertIn("error", await sender(None, "conv-1", "report", media_files=["/tmp/a.png"]))
            mock_urlopen.assert_not_called()

    async def test_send_drops_to_plain_text_when_the_card_is_over_the_byte_cap(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        adapter = PlatformRegistry().create_adapter("teams")
        self.assertEqual(adapter.MAX_MESSAGE_LENGTH, 16000)
        sent = []

        def fake_urlopen(req, timeout=35):
            sent.append(json.loads(req.data))
            resp = mock.MagicMock()
            resp.read.return_value = json.dumps({"id": "m"}).encode("utf-8")
            resp.__enter__.return_value = resp
            return resp

        # 16000 characters as 20-character lines: a card several times the text.
        long_text = "\n".join(["- item %05d x" % i for i in range(1000)])
        self.assertLessEqual(len(long_text), 16000)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            await adapter.send("conv-1", long_text)
            await adapter.send("conv-1", "# short\n- one line")
        big, small = sent[0]["payload"], sent[1]["payload"]
        self.assertNotIn("attachments", big)
        self.assertEqual(big["text"], long_text)
        self.assertLessEqual(len(json.dumps(big, ensure_ascii=False).encode("utf-8")), 28 * 1024)
        self.assertIn("attachments", small)

    async def test_send_truncates_non_ascii_text_that_no_form_can_fit(self):
        """16,000 CJK characters are 48 KB of UTF-8: over the cap as a card and
        as plain text, so the text is cut to fit and says so."""
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        adapter = PlatformRegistry().create_adapter("teams")
        sent = []

        def fake_urlopen(req, timeout=35):
            sent.append(json.loads(req.data))
            resp = mock.MagicMock()
            resp.read.return_value = json.dumps({"id": "m"}).encode("utf-8")
            resp.__enter__.return_value = resp
            return resp

        # Newlines and quotes cost two bytes each once serialized, and the
        # reply id rides along: the cap is measured on exactly what is posted.
        cjk = "\n".join(['集群"状态"报告。' * 5 for _ in range(460)])  # 16,100+ characters
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            await adapter.send("conv-1", cjk, reply_to="1697000000000")
        payload = sent[0]["payload"]
        self.assertNotIn("attachments", payload)
        self.assertEqual(payload["replyToId"], "1697000000000")
        self.assertTrue(payload["text"].endswith(teams_relay_patch.TEAMS_TRUNCATION_MARKER))
        self.assertTrue(cjk.startswith(payload["text"][: -len(teams_relay_patch.TEAMS_TRUNCATION_MARKER)]))
        self.assertLessEqual(
            len(json.dumps(payload, ensure_ascii=False).encode("utf-8")), 28 * 1024
        )
        self.assertGreater(len(payload["text"]), 9000)  # cut to fit, not emptied

    def _recording_adapter(self):
        """An adapter with every gate open except the one a test closes, so
        the gate under test is the only reason an activity is dropped, and a
        recorder on the dispatch path so a pass is a positive signal."""
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        adapter = PlatformRegistry().create_adapter("teams")
        adapter.allow_all_users = True
        handled = []
        adapter.set_handler(lambda c, t, m: handled.append((c, t, m)))
        return adapter, handled

    @staticmethod
    def _ok_urlopen():
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps({"status": "ok"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        return mock.patch("urllib.request.urlopen", return_value=mock_resp)

    async def test_unauthorized_tenant_rejected(self):
        adapter, handled = self._recording_adapter()
        adapter.configured_tenant_id = "allowed-tenant-only"
        activity = {
            "type": "message",
            "text": "hi",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "channelData": {"tenant": {"id": "unauthorized-tenant"}},
            "from": {"id": "user-1"},
            "conversation": {"id": "conv-1"},
        }
        with self._ok_urlopen(), self.assertLogs("teams-relay-patch", level="WARNING") as logs:
            await adapter._process_activity(activity)
        self.assertEqual(handled, [])
        self.assertTrue(any("unauthorized tenant" in line for line in logs.output), logs.output)
        # Control: the same activity from the configured tenant is dispatched.
        activity["channelData"]["tenant"]["id"] = "allowed-tenant-only"
        with self._ok_urlopen():
            await adapter._process_activity(activity)
        self.assertEqual([h[0] for h in handled], ["conv-1"])

    async def test_unauthorized_user_rejected_and_warned(self):
        adapter, handled = self._recording_adapter()
        adapter.allow_all_users = False
        adapter.allowed_users = {"allowed-admin@domain.com"}
        # Display name equals an allowlisted UPN, but id/aad/email do not -> rejected.
        activity = {
            "type": "message",
            "text": "hi",
            "from": {
                "id": "29:user-unknown",
                "name": "allowed-admin@domain.com",
                "email": "eve@domain.com",
            },
            "conversation": {"id": "conv-2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        with self._ok_urlopen() as mock_urlopen, self.assertLogs(
            "teams-relay-patch", level="WARNING"
        ) as logs:
            await adapter._process_activity(activity)
        self.assertEqual(handled, [])
        self.assertTrue(any("unauthorized user" in line for line in logs.output), logs.output)
        # Exactly one outbound call: the "not authorized" notice, and no typing indicator.
        posted = [json.loads(call.args[0].data) for call in mock_urlopen.call_args_list]
        self.assertEqual(len(posted), 1)
        self.assertIn("not authorized", json.dumps(posted[0]["payload"]))
        self.assertNotIn('"typing"', json.dumps(posted[0]["payload"]))
        # Control: the allowlisted email is admitted and dispatched.
        activity["from"]["email"] = "allowed-admin@domain.com"
        with self._ok_urlopen():
            await adapter._process_activity(activity)
        self.assertEqual([h[0] for h in handled], ["conv-2"])

    async def test_malformed_activity_and_untrusted_service_url_dropped(self):
        adapter, handled = self._recording_adapter()
        base = {
            "type": "message",
            "text": "hi",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "conversation": {"id": "conv-1"},
            "from": {"id": "u1"},
        }
        # Each fixture differs from a dispatchable activity in exactly one field.
        cases = (
            ({"channelData": "bad"}, "malformed"),
            ({"from": "bad"}, "malformed"),
            ({"conversation": "bad"}, "malformed"),
            ({"text": 12345}, "malformed"),
            ({"serviceUrl": "https://attacker.trafficmanager.net/"}, "untrusted serviceUrl"),
        )
        for change, phrase in cases:
            with self._ok_urlopen() as mock_urlopen, self.assertLogs(
                "teams-relay-patch", level="WARNING"
            ) as logs:
                await adapter._process_activity({**base, **change})
            self.assertEqual(handled, [], change)
            mock_urlopen.assert_not_called()
            self.assertTrue(any(phrase in line for line in logs.output), (change, logs.output))
        # Control: the unchanged activity is dispatched.
        with self._ok_urlopen():
            await adapter._process_activity(dict(base))
        self.assertEqual([h[0] for h in handled], ["conv-1"])

    async def test_non_message_ignored(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")

        handled = []
        adapter.set_handler(lambda c, t, m: handled.append(t))
        await adapter._process_activity({"type": "conversationUpdate"})
        self.assertEqual(len(handled), 0)

    async def test_connect_waits_for_sidecar_and_disconnects(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")

        call_count = 0

        def fake_urlopen(req, timeout=35):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 503, "Service Unavailable", hdrs=None, fp=None
                )
            resp = mock.MagicMock()
            resp.read.return_value = json.dumps({"settled": False}).encode("utf-8")
            resp.__enter__.return_value = resp
            return resp

        with (
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
            mock.patch("asyncio.sleep", new=mock.AsyncMock()),
        ):
            self.assertTrue(await adapter.connect())
            self.assertGreaterEqual(call_count, 2)
            self.assertTrue(adapter._running)
            await adapter.disconnect()
            self.assertFalse(adapter._running)

    @mock.patch.dict(os.environ, {"TEAMS_RELAY_URL": RELAY_URL})
    async def test_relay_loop_acks_a_handled_event_and_nacks_one_the_handler_fails(self):
        """One pulled event reaches the handler and its receipt is acked; when
        the handler raises, the receipt is nacked instead. The fake flips
        ``_running`` on the pull, so each loop runs one iteration."""
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        adapter = PlatformRegistry().create_adapter("teams")
        adapter.allow_all_users = True

        activity = {
            "type": "message",
            "id": "act-loop-1",
            "text": "hello from loop",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "conversation": {"id": "conv-loop-1"},
            "from": {"id": "29:u-loop", "name": "Operator"},
        }
        posts = []

        def fake_urlopen(req, timeout=None):
            del timeout
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            if req.get_method() == "GET" and req.full_url.endswith("/v1/chat/teams/events"):
                adapter._running = False
                payload = {"event": {"receipt": "r-1", "event": activity}}
            else:
                posts.append((req.full_url, body))
                payload = {"settled": True}
            resp = mock.MagicMock()
            resp.read.return_value = json.dumps(payload).encode("utf-8")
            resp.__enter__.return_value = resp
            return resp

        def settled(path):
            return [body for url, body in posts if url.endswith(path)]

        handled = []

        async def handler(chat_id, text, metadata):
            handled.append((chat_id, text))

        async def failing_handler(chat_id, text, metadata):
            raise RuntimeError("handler failed")

        with (
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
            mock.patch("asyncio.sleep", new=mock.AsyncMock()),
        ):
            adapter.set_handler(handler)
            adapter._running = True
            await adapter._relay_loop()
            self.assertEqual(handled, [("conv-loop-1", "hello from loop")])
            self.assertEqual(settled("/v1/chat/teams/events/ack"), [{"receipt": "r-1"}])
            self.assertEqual(settled("/v1/chat/teams/events/nack"), [])

            posts.clear()
            adapter.set_handler(failing_handler)
            adapter._running = True
            with self.assertLogs("teams-relay-patch", level="WARNING"):
                await adapter._relay_loop()
            self.assertEqual(settled("/v1/chat/teams/events/ack"), [])
            self.assertEqual(settled("/v1/chat/teams/events/nack"), [{"receipt": "r-1"}])


if __name__ == "__main__":
    unittest.main()
