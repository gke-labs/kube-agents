import dataclasses
import json
import os
import sys
import types
import unittest
import urllib.error
from unittest import mock


def _register_fake_modules():
    if "gateway.platform_registry" in sys.modules:
        return
    gateway = types.ModuleType("gateway")
    registry_mod = types.ModuleType("gateway.platform_registry")

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

    registry_mod.PlatformEntry = PlatformEntry
    registry_mod.PlatformRegistry = PlatformRegistry
    registry_mod.platform_registry = PlatformRegistry()
    gateway.platform_registry = registry_mod
    sys.modules["gateway"] = gateway
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


class _FakePlatformConfig:
    """Attribute-style PlatformConfig mirroring gateway.config.PlatformConfig."""

    def __init__(self, *, extra=None, typing_status_text="Custom thinking…"):
        self.extra = extra
        self.typing_status_text = typing_status_text


class TestTeamsRelayPatch(unittest.IsolatedAsyncioTestCase):
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
        with mock.patch.dict(os.environ, {"TEAMS_RELAY_URL": ""}):
            teams_relay_patch.install()

    @mock.patch.dict(os.environ, {"TEAMS_RELAY_URL": "http://127.0.0.1:8642"})
    async def test_teams_relay_adapter_lifecycle_and_message_processing(self):
        from gateway.platform_registry import PlatformRegistry, platform_registry

        teams_relay_patch.install()
        self.assertIsNotNone(platform_registry.get("teams"))

        registry = PlatformRegistry()
        adapter = registry.create_adapter(
            "teams", config={"extra": {"adaptive_cards": True}}
        )
        self.assertIsNotNone(adapter)

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

    @mock.patch.dict(os.environ, {"TEAMS_RELAY_URL": "http://127.0.0.1:8642"})
    async def test_base_platform_adapter_handle_message_and_platform_config_object(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        cfg = _FakePlatformConfig(
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

    @mock.patch.dict(os.environ, {"TEAMS_RELAY_URL": "http://127.0.0.1:8642"})
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
                message="Fleet audit passing",
                thread_id="reply-thread-1",
                metadata={
                    "title": "Audit Pass",
                    "serviceUrl": "https://smba.trafficmanager.net/teams/",
                },
            )
            self.assertEqual(res, {"id": "msg-1"})
            self.assertTrue(getattr(res, "success", False))
            self.assertEqual(getattr(res, "message_id", None), "msg-1")

            await adapter.send_typing(
                "conv-123", "https://smba.trafficmanager.net/teams/"
            )

    @mock.patch.dict(os.environ, {"TEAMS_RELAY_URL": "http://127.0.0.1:8642"})
    async def test_unauthorized_tenant_rejected(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")
        adapter.configured_tenant_id = "allowed-tenant-only"

        activity = {
            "type": "message",
            "channelData": {"tenant": {"id": "unauthorized-tenant"}},
            "from": {"id": "user-1"},
            "conversation": {"id": "conv-1"},
        }
        handled = []
        adapter.set_handler(lambda c, t, m: handled.append(t))
        await adapter._process_activity(activity)
        self.assertEqual(len(handled), 0)

    @mock.patch.dict(os.environ, {"TEAMS_RELAY_URL": "http://127.0.0.1:8642"})
    async def test_unauthorized_user_rejected_and_warned(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")
        adapter.allow_all_users = False
        adapter.allowed_users = {"allowed-admin@domain.com"}

        # Display name equals allowed-admin@domain.com, but id/aad/email do not -> MUST reject!
        activity = {
            "type": "message",
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
            self.assertTrue(mock_urlopen.called)

    @mock.patch.dict(os.environ, {"TEAMS_RELAY_URL": "http://127.0.0.1:8642"})
    async def test_malformed_activity_and_untrusted_service_url_dropped(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")
        adapter.allow_all_users = True

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            for bad_activity in (
                {"type": "message", "channelData": "bad"},
                {"type": "message", "from": "bad"},
                {"type": "message", "conversation": "bad"},
                {"type": "message", "text": 12345},
                {
                    "type": "message",
                    "text": "hi",
                    "serviceUrl": "https://attacker.trafficmanager.net/",
                    "conversation": {"id": "conv-1"},
                    "from": {"id": "u1"},
                },
            ):
                await adapter._process_activity(bad_activity)
            mock_urlopen.assert_not_called()

    @mock.patch.dict(os.environ, {"TEAMS_RELAY_URL": "http://127.0.0.1:8642"})
    async def test_non_message_ignored(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")

        handled = []
        adapter.set_handler(lambda c, t, m: handled.append(t))
        await adapter._process_activity({"type": "conversationUpdate"})
        self.assertEqual(len(handled), 0)

    @mock.patch.dict(os.environ, {"TEAMS_RELAY_URL": "http://127.0.0.1:8642"})
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
