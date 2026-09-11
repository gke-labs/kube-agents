import json
import os
import sys
import types
import unittest
from unittest import mock


def _register_fake_modules():
    if "gateway.platform_registry" in sys.modules:
        return
    gateway = types.ModuleType("gateway")
    registry_mod = types.ModuleType("gateway.platform_registry")

    class PlatformRegistry:
        def create_adapter(self, name, *args, **kwargs):
            return None

    registry_mod.PlatformRegistry = PlatformRegistry
    gateway.platform_registry = registry_mod
    sys.modules["gateway"] = gateway
    sys.modules["gateway.platform_registry"] = registry_mod


_register_fake_modules()

import teams_relay_patch
from teams_relay_patch import (
    format_teams_activity_payload,
    is_tenant_allowed,
    is_user_allowed,
    markdown_to_adaptive_card,
    parse_allowed_users,
)


class TestTeamsRelayPatch(unittest.IsolatedAsyncioTestCase):
    def test_parse_allowed_users(self):
        raw = "user1@example.com, USER2@example.com , 1234-abcd-5678 "
        parsed = parse_allowed_users(raw)
        self.assertEqual(
            parsed,
            {"user1@example.com", "user2@example.com", "1234-abcd-5678"},
        )
        self.assertEqual(parse_allowed_users(""), set())

    def test_is_user_allowed(self):
        allowed = {"user1@example.com", "aad-obj-id-123"}
        self.assertTrue(
            is_user_allowed("aad-obj-id-123", "User One", "user1@example.com", allowed)
        )
        self.assertTrue(
            is_user_allowed("unknown-id", "User One", "user1@example.com", allowed)
        )
        self.assertFalse(
            is_user_allowed("unauthorized-id", "User Two", "user2@example.com", allowed)
        )
        # allow_all mode
        self.assertTrue(
            is_user_allowed("unauthorized-id", "User Two", "user2@example.com", allowed, allow_all=True)
        )

    def test_is_tenant_allowed(self):
        # Single-tenant enforcement
        self.assertTrue(is_tenant_allowed("tenant-abc-123", "tenant-abc-123"))
        self.assertTrue(is_tenant_allowed("TENANT-ABC-123", "tenant-abc-123"))
        self.assertFalse(is_tenant_allowed("tenant-xyz-999", "tenant-abc-123"))
        self.assertFalse(is_tenant_allowed(None, "tenant-abc-123"))
        # Unrestricted if no tenant configured
        self.assertTrue(is_tenant_allowed("any-tenant", None))
        self.assertTrue(is_tenant_allowed(None, None))

    def test_markdown_to_adaptive_card(self):
        text = "# Fleet Audit Report\n\n- Finding 1: cgroup pressure\n- Finding 2: missing tag\n\n```json\n{\"ok\": true}\n```\n\nSummary complete."
        card = markdown_to_adaptive_card(text, title="Audit Results")
        self.assertEqual(card["type"], "AdaptiveCard")
        self.assertEqual(card["version"], "1.5")
        body = card["body"]
        self.assertTrue(len(body) >= 4)
        self.assertEqual(body[0]["type"], "TextBlock")
        self.assertEqual(body[0]["text"], "Audit Results")
        self.assertEqual(body[1]["text"], "Fleet Audit Report")

    def test_format_teams_activity_payload(self):
        # Adaptive Cards enabled
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

        # Flat text fallback
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
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams", config={"extra": {"adaptive_cards": True}})
        self.assertIsNotNone(adapter)

        handled_messages = []

        async def fake_handler(chat_id, text, metadata):
            handled_messages.append((chat_id, text, metadata))

        adapter.set_handler(fake_handler)
        adapter.allow_all_users = True

        activity = {
            "type": "message",
            "id": "act-12345",
            "text": "run fleet audit",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "conversation": {"id": "conv-9876"},
            "from": {"id": "user-456", "name": "Admin"},
            "channelData": {"tenant": {"id": "tenant-1"}},
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
                metadata={"title": "Audit Pass", "serviceUrl": "https://smba.trafficmanager.net/teams/"},
            )
            self.assertEqual(res, {"id": "msg-1"})

            # Send typing
            await adapter.send_typing("conv-123", "https://smba.trafficmanager.net/teams/")

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

        activity = {
            "type": "message",
            "from": {"id": "user-unknown", "name": "Eve", "email": "eve@domain.com"},
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
    async def test_connect_and_disconnect(self):
        from gateway.platform_registry import PlatformRegistry

        teams_relay_patch.install()
        registry = PlatformRegistry()
        adapter = registry.create_adapter("teams")

        self.assertTrue(await adapter.connect())
        self.assertTrue(adapter._running)
        await adapter.disconnect()
        self.assertFalse(adapter._running)


if __name__ == "__main__":
    unittest.main()

