import abc
import dataclasses
import enum
import json
import os
import sys
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


def _register_fake_modules():
    if "gateway.platform_registry" in sys.modules:
        return
    gateway = types.ModuleType("gateway")
    registry_mod = types.ModuleType("gateway.platform_registry")
    platforms_mod = types.ModuleType("gateway.platforms")
    base_mod = types.ModuleType("gateway.platforms.base")
    config_mod = types.ModuleType("gateway.config")

    class Platform(enum.Enum):
        LOCAL = "local"
        TELEGRAM = "telegram"
        DISCORD = "discord"
        SLACK = "slack"
        TEAMS = "teams"

        @classmethod
        def _missing_(cls, value):
            if isinstance(value, str):
                for member in cls:
                    if member.value == value:
                        return member
            return None

    @dataclasses.dataclass
    class PlatformConfig:
        enabled: bool = False
        token: str | None = None
        api_key: str | None = None
        home_channel: object = None
        typing_status_text: str | None = None
        extra: dict = dataclasses.field(default_factory=dict)

    class MessageType(enum.Enum):
        TEXT = "text"
        COMMAND = "command"

    @dataclasses.dataclass
    class SendResult:
        success: bool
        message_id: str | None = None
        raw_response: object = None
        error: str | None = None

    @dataclasses.dataclass
    class MessageEvent:
        text: str
        message_type: MessageType = MessageType.TEXT
        source: object = None
        raw_message: object = None
        message_id: str | None = None
        media_urls: list = dataclasses.field(default_factory=list)
        media_types: list = dataclasses.field(default_factory=list)
        reply_to_id: str | None = None
        reply_to_text: str | None = None
        timestamp: object = None

    class BasePlatformAdapter(abc.ABC):
        """The abstract set and call signatures of the upstream base, so an
        adapter that cannot be instantiated there cannot be instantiated here."""

        def __init__(self, config: PlatformConfig, platform: Platform):
            if not isinstance(platform, Platform) or not hasattr(platform, "value"):
                raise TypeError(f"Expected Platform enum with .value, got {platform!r}")
            self.config = config
            self.platform = platform
            self._message_handler = None
            self._running = False
            self._background_tasks = set()

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

        def set_message_handler(self, handler):
            self._message_handler = handler

        def build_source(
            self,
            chat_id: str,
            chat_name: str | None = None,
            chat_type: str = "dm",
            user_id: str | None = None,
            user_name: str | None = None,
            thread_id: str | None = None,
            chat_topic: str | None = None,
            user_id_alt: str | None = None,
            chat_id_alt: str | None = None,
        ):
            return {
                "platform": self.platform.value,
                "chat_id": chat_id,
                "chat_name": chat_name,
                "chat_type": chat_type,
                "user_id": user_id,
                "user_name": user_name,
                "thread_id": thread_id,
                "chat_topic": chat_topic,
                "user_id_alt": user_id_alt,
                "chat_id_alt": chat_id_alt,
            }

        async def handle_message(self, event: MessageEvent) -> None:
            if self._message_handler is not None:
                await self._message_handler(event)

    @dataclasses.dataclass
    class PlatformEntry:
        name: str
        label: str = ""
        adapter_factory: object = None
        check_fn: object = None
        is_connected: object = None
        standalone_sender_fn: object = None

    class PlatformRegistry:
        def __init__(self):
            self._entries = {}

        def create_adapter(self, name, *args, **kwargs):
            return None

        def register(self, entry, *, scope=None):
            del scope
            self._entries[entry.name] = entry

        def get(self, name):
            return self._entries.get(name)

    config_mod.Platform = Platform
    config_mod.PlatformConfig = PlatformConfig

    base_mod.BasePlatformAdapter = BasePlatformAdapter
    base_mod.MessageEvent = MessageEvent
    base_mod.MessageType = MessageType
    base_mod.SendResult = SendResult
    platforms_mod.base = base_mod

    registry_mod.PlatformEntry = PlatformEntry
    registry_mod.PlatformRegistry = PlatformRegistry
    registry_mod.platform_registry = PlatformRegistry()

    gateway.config = config_mod
    gateway.platforms = platforms_mod
    gateway.platform_registry = registry_mod

    sys.modules["gateway"] = gateway
    sys.modules["gateway.config"] = config_mod
    sys.modules["gateway.platforms"] = platforms_mod
    sys.modules["gateway.platforms.base"] = base_mod
    sys.modules["gateway.platform_registry"] = registry_mod


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

_TEAMS_ENV_KEYS = (
    "TEAMS_RELAY_URL",
    "TEAMS_TENANT_ID",
    "TEAMS_ALLOWED_USERS",
    "TEAMS_ALLOW_ALL_USERS",
    "TEAMS_RELAY_BOOTSTRAP_WAIT_SECONDS",
    "TEAMS_DEFAULT_SERVICE_URL",
)


class TestTeamsRelayPatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self._saved_env = {key: os.environ.get(key) for key in _TEAMS_ENV_KEYS}
        for key in _TEAMS_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["TEAMS_RELAY_URL"] = "http://127.0.0.1:8642"

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

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
        # Query strings and fragments (including bare ? and #) must be rejected
        self.assertFalse(is_trusted_service_url("https://smba.trafficmanager.net/teams?"))
        self.assertFalse(is_trusted_service_url("https://smba.trafficmanager.net/teams#"))
        self.assertFalse(is_trusted_service_url("https://smba.trafficmanager.net/teams?x=1"))
        self.assertFalse(is_trusted_service_url("https://smba.trafficmanager.net/teams#frag"))

    def test_markdown_to_adaptive_card(self):
        text = (
            "# Fleet Audit Report\n\n"
            "- Finding 1: cgroup pressure\n"
            "- Finding 2: missing tag\n\n"
            "```json\n{\"ok\": true}\n```\n\n"
            "#1040 is not a heading\n"
            "#!/bin/bash\n\n"
            "Summary complete."
        )
        card = markdown_to_adaptive_card(text, title="Audit Results")
        self.assertEqual(card["type"], "AdaptiveCard")
        self.assertEqual(card["version"], "1.5")
        body = card["body"]
        self.assertTrue(len(body) >= 5)
        self.assertEqual(body[0]["type"], "TextBlock")
        self.assertEqual(body[0]["text"], "Audit Results")
        self.assertEqual(body[1]["text"], "Fleet Audit Report")
        self.assertEqual(body[1].get("weight"), "Bolder")
        # Non-heading # tokens (#1040, #!/bin/bash) retain their # and are not rendered as headings
        issue_blocks = [
            block for block in body if block.get("text") == "#1040 is not a heading"
        ]
        shebang_blocks = [
            block for block in body if block.get("text") == "#!/bin/bash"
        ]
        self.assertEqual(len(issue_blocks), 1)
        self.assertIsNone(issue_blocks[0].get("weight"))
        self.assertEqual(len(shebang_blocks), 1)
        self.assertIsNone(shebang_blocks[0].get("weight"))

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

    def test_plugin_adapter_registers_teams_platform(self):
        plugin_dir = (
            Path(__file__).resolve().parents[2]
            / "chat"
            / "plugins"
            / "platforms"
            / "teams"
        )
        sys.path.insert(0, str(plugin_dir.parent))
        self.addCleanup(sys.path.remove, str(plugin_dir.parent))
        from teams import adapter as teams_plugin_adapter

        registered = []

        class FakePluginContext:
            def register_platform(self, **kwargs):
                registered.append(kwargs)

        teams_plugin_adapter.register(FakePluginContext())
        self.assertEqual(len(registered), 1)
        self.assertEqual(registered[0]["name"], "teams")
        self.assertEqual(registered[0]["label"], "Microsoft Teams")

    async def test_teams_relay_adapter_lifecycle_and_message_processing(self):
        from gateway.platform_registry import PlatformRegistry, platform_registry

        teams_relay_patch.install()
        self.assertIsNotNone(platform_registry.get("teams"))

        registry = PlatformRegistry()
        adapter = registry.create_adapter(
            "teams", config={"extra": {"adaptive_cards": True}}
        )
        self.assertIsNotNone(adapter)
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
            enabled=True,
            extra={"adaptive_cards": False},
            typing_status_text="Custom thinking…",
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
        self.assertEqual(received_events[0].source["platform"], "teams")
        # The activity id is per message: as thread_id it would key a new
        # session per message and mark every DM as a shared thread.
        self.assertIsNone(received_events[0].source["thread_id"])
        self.assertEqual(received_events[0].source["chat_type"], "channel")

        for conversation_type, chat_type in (
            ("personal", "dm"),
            ("groupChat", "group"),
            ("channel", "channel"),
            ("unknownType", "channel"),
        ):
            received_events.clear()
            typed = dict(activity, conversation={"id": "conv-777", "conversationType": conversation_type})
            with mock.patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = mock.MagicMock()
                mock_resp.read.return_value = json.dumps({"status": "ok"}).encode("utf-8")
                mock_resp.__enter__.return_value = mock_resp
                mock_urlopen.return_value = mock_resp
                await adapter._process_activity(typed)
            self.assertEqual(len(received_events), 1, conversation_type)
            self.assertEqual(received_events[0].source["chat_type"], chat_type, conversation_type)
            self.assertIsNone(received_events[0].source["thread_id"], conversation_type)

    async def test_send_and_send_typing(self):
        from gateway.platforms.base import SendResult
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps(
                {"response": {"id": "msg-1"}}
            ).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            res = await adapter.send(
                chat_id="conv-123",
                content="Fleet audit passing",
                reply_to="reply-thread-1",
                metadata={
                    "title": "Audit Pass",
                    "serviceUrl": "https://smba.trafficmanager.net/teams/",
                },
            )
            self.assertIsInstance(res, SendResult)
            self.assertTrue(res.success)
            self.assertEqual(res.message_id, "msg-1")
            self.assertEqual(res.raw_response, {"response": {"id": "msg-1"}})

            await adapter.send_typing(
                "conv-123", "https://smba.trafficmanager.net/teams/"
            )
            self.assertEqual(
                await adapter.get_chat_info("19:abc@thread.tacv2"),
                {
                    "name": "19:abc@thread.tacv2",
                    "type": "channel",
                    "chat_id": "19:abc@thread.tacv2",
                },
            )
            self.assertEqual(
                await adapter.get_chat_info("a:1-personal-chat"),
                {
                    "name": "a:1-personal-chat",
                    "type": "dm",
                    "chat_id": "a:1-personal-chat",
                },
            )

    async def test_send_failure_returns_failed_send_result(self):
        from gateway.platforms.base import SendResult
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        adapter = PlatformRegistry().create_adapter("teams")

        rejected = urllib.error.HTTPError(
            "http://relay/v1/chat/teams/api", 400, "Bad Request", {}, None
        )
        for error in (rejected, urllib.error.URLError("connection refused")):
            with mock.patch("urllib.request.urlopen", side_effect=error):
                res = await adapter.send(chat_id="conv-123", content="hello")
            self.assertIsInstance(res, SendResult)
            self.assertFalse(res.success)
            self.assertTrue(res.error)

    async def test_relay_loop_acks_processed_event_and_nacks_failed_event(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")
        adapter.allow_all_users = True

        handled = []

        async def handler(chat_id, text, metadata):
            handled.append((chat_id, text, metadata))

        adapter.set_handler(handler)

        valid_activity = {
            "type": "message",
            "id": "act-loop-1",
            "text": "hello from loop",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "conversation": {"id": "conv-loop-1"},
            "from": {"id": "29:u-loop", "name": "Operator"},
        }

        requests_seen = []

        def fake_urlopen_ack(req, timeout=35):
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            requests_seen.append((req.get_method(), req.full_url, body))
            resp = mock.MagicMock()
            if req.full_url.endswith("/v1/chat/teams/events") and req.get_method() == "GET":
                adapter._running = False
                resp.read.return_value = json.dumps(
                    {"event": {"receipt": "r-ack-1", "event": valid_activity}}
                ).encode("utf-8")
            else:
                resp.read.return_value = json.dumps({"settled": True}).encode("utf-8")
            resp.__enter__.return_value = resp
            return resp

        adapter._running = True
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen_ack):
            await adapter._relay_loop()

        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0][1], "hello from loop")
        ack_calls = [
            (method, url, body)
            for method, url, body in requests_seen
            if url.endswith("/v1/chat/teams/events/ack")
        ]
        self.assertEqual(
            ack_calls,
            [("POST", "http://127.0.0.1:8642/v1/chat/teams/events/ack", {"receipt": "r-ack-1"})],
        )

        # Now verify that when _process_activity raises, _relay_loop nacks the receipt
        requests_seen.clear()

        def fake_urlopen_nack(req, timeout=35):
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            requests_seen.append((req.get_method(), req.full_url, body))
            resp = mock.MagicMock()
            if req.full_url.endswith("/v1/chat/teams/events") and req.get_method() == "GET":
                adapter._running = False
                resp.read.return_value = json.dumps(
                    {"event": {"receipt": "r-nack-1", "event": valid_activity}}
                ).encode("utf-8")
            else:
                resp.read.return_value = json.dumps({"settled": True}).encode("utf-8")
            resp.__enter__.return_value = resp
            return resp

        adapter._running = True
        with (
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen_nack),
            mock.patch.object(
                adapter, "_process_activity", side_effect=RuntimeError("boom")
            ),
        ):
            await adapter._relay_loop()

        nack_calls = [
            (method, url, body)
            for method, url, body in requests_seen
            if url.endswith("/v1/chat/teams/events/nack")
        ]
        self.assertEqual(
            nack_calls,
            [("POST", "http://127.0.0.1:8642/v1/chat/teams/events/nack", {"receipt": "r-nack-1"})],
        )

    async def test_unauthorized_tenant_rejected(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")
        adapter.configured_tenant_id = "allowed-tenant-only"
        adapter.allow_all_users = True

        handled = []
        adapter.set_handler(lambda c, t, m: handled.append(t))

        unauthorized_activity = {
            "type": "message",
            "text": "should be rejected by tenant gate",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "channelData": {"tenant": {"id": "unauthorized-tenant"}},
            "from": {"id": "user-1"},
            "conversation": {"id": "conv-1"},
        }
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            await adapter._process_activity(unauthorized_activity)
            self.assertEqual(handled, [])
            mock_urlopen.assert_not_called()

            # Control: identical activity with the allowed tenant passes the gate
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"status": "ok"}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            allowed_activity = {
                **unauthorized_activity,
                "channelData": {"tenant": {"id": "allowed-tenant-only"}},
            }
            await adapter._process_activity(allowed_activity)
            self.assertEqual(handled, ["should be rejected by tenant gate"])

    async def test_unauthorized_user_rejected_and_warned(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")
        adapter.allow_all_users = False
        adapter.allowed_users = {"allowed-admin@domain.com"}

        handled = []
        adapter.set_handler(lambda c, t, m: handled.append(t))

        # Display name equals allowed-admin@domain.com, but id/aad/email do not -> MUST reject!
        activity = {
            "type": "message",
            "text": "secret command",
            "from": {
                "id": "29:user-unknown",
                "name": "allowed-admin@domain.com",
                "email": "eve@domain.com",
            },
            "conversation": {"id": "conv-2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"status": "ok"}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            await adapter._process_activity(activity)
            self.assertEqual(handled, [])
            self.assertEqual(mock_urlopen.call_count, 1)
            sent_req = mock_urlopen.call_args[0][0]
            sent_body = sent_req.data.decode("utf-8")
            self.assertIn("not authorized", sent_body)
            self.assertNotIn('"typing"', sent_body)

            # Control: when email matches the allowlist, the message is dispatched
            allowed_activity = {
                **activity,
                "from": {
                    "id": "29:user-unknown",
                    "name": "Admin",
                    "email": "allowed-admin@domain.com",
                },
            }
            await adapter._process_activity(allowed_activity)
            self.assertEqual(handled, ["secret command"])

    async def test_malformed_activity_and_untrusted_service_url_dropped(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")
        adapter.allow_all_users = True

        handled = []
        adapter.set_handler(lambda c, t, m: handled.append(t))

        base_valid = {
            "type": "message",
            "text": "valid text",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "conversation": {"id": "conv-1"},
            "from": {"id": "u1"},
        }

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            for bad_override in (
                {"channelData": "bad"},
                {"from": "bad"},
                {"conversation": "bad"},
                {"text": 12345},
                {"serviceUrl": "https://attacker.trafficmanager.net/"},
                {"serviceUrl": "https://smba.trafficmanager.net/teams?query=1"},
                {"serviceUrl": "https://smba.trafficmanager.net/teams#frag"},
            ):
                bad_activity = {**base_valid, **bad_override}
                await adapter._process_activity(bad_activity)
            self.assertEqual(handled, [])
            mock_urlopen.assert_not_called()

            # Control: base_valid itself passes and dispatches
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"status": "ok"}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            await adapter._process_activity(base_valid)
            self.assertEqual(handled, ["valid text"])

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


if __name__ == "__main__":
    unittest.main()
