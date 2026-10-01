"""Credential-free Microsoft Teams / Bot Framework transport for Hermes' adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from credential_proxy_client import authorization_headers

LOGGER = logging.getLogger("teams-relay-patch")
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_RELAY_READY_TIMEOUT = 120.0


def parse_allowed_users(raw: str) -> set[str]:
    """Parse comma-separated allowed user IDs or emails into a normalized set."""
    if not raw:
        return set()
    return {user.strip().lower() for user in raw.split(",") if user.strip()}


def is_user_allowed(
    sender_id: str,
    sender_name: str | None,
    sender_email: str | None,
    allowed_users: set[str],
    allow_all: bool = False,
) -> bool:
    """Check if the sender is authorized to interact with the Teams bot."""
    if allow_all:
        return True
    if not allowed_users:
        return False
    candidates = {sender_id.lower()}
    if sender_name:
        candidates.add(sender_name.lower())
    if sender_email:
        candidates.add(sender_email.lower())
    return bool(candidates & allowed_users)


def is_tenant_allowed(activity_tenant_id: str | None, configured_tenant_id: str | None) -> bool:
    """Check if the activity originates from the configured Microsoft Entra ID tenant."""
    if not configured_tenant_id:
        return True
    if not activity_tenant_id:
        return False
    return activity_tenant_id.strip().lower() == configured_tenant_id.strip().lower()


def markdown_to_adaptive_card(text: str, title: str | None = None) -> dict[str, Any]:
    """Convert Markdown text into a Microsoft Teams Adaptive Card v1.5 schema."""
    body_elements: list[dict[str, Any]] = []

    if title:
        body_elements.append(
            {
                "type": "TextBlock",
                "text": title,
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            }
        )

    # Simple parsing into code blocks, bullet points, headers, and text
    lines = text.split("\n")
    in_code_block = False
    code_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                in_code_block = False
                body_elements.append(
                    {
                        "type": "TextBlock",
                        "text": "\n".join(code_lines),
                        "fontType": "Monospace",
                        "wrap": True,
                    }
                )
                code_lines = []
            else:
                in_code_block = True
                code_lines = []
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not stripped:
            continue

        if stripped.startswith("#"):
            # Header
            header_level = len(stripped) - len(stripped.lstrip("#"))
            header_text = stripped.lstrip("#").strip()
            size = "Large" if header_level == 1 else "Medium"
            body_elements.append(
                {
                    "type": "TextBlock",
                    "text": header_text,
                    "weight": "Bolder",
                    "size": size,
                    "wrap": True,
                }
            )
        elif stripped.startswith(("- ", "* ")):
            # Bullet item
            body_elements.append(
                {
                    "type": "TextBlock",
                    "text": f"• {stripped[2:].strip()}",
                    "wrap": True,
                }
            )
        else:
            # Standard prose
            body_elements.append(
                {
                    "type": "TextBlock",
                    "text": stripped,
                    "wrap": True,
                }
            )

    if in_code_block and code_lines:
        body_elements.append(
            {
                "type": "TextBlock",
                "text": "\n".join(code_lines),
                "fontType": "Monospace",
                "wrap": True,
            }
        )

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body_elements if body_elements else [{"type": "TextBlock", "text": text, "wrap": True}],
    }


def format_teams_activity_payload(
    message: str,
    *,
    use_adaptive_cards: bool = True,
    title: str | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Format an outbound Bot Framework activity payload."""
    if not use_adaptive_cards:
        return {
            "type": "message",
            "text": message,
        }

    card = markdown_to_adaptive_card(message, title=title)
    if actions:
        card["actions"] = actions

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }


def install() -> None:
    """Install the Teams relay patch onto Hermes PlatformRegistry."""
    relay_url = os.getenv("TEAMS_RELAY_URL", "").rstrip("/")
    if not relay_url:
        return

    def request(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", **authorization_headers()}
        req = urllib.request.Request(
            relay_url + path,
            data=body,
            headers=headers,
            method="GET" if body is None else "POST",
        )
        with urllib.request.urlopen(req, timeout=35) as response:
            return json.load(response)

    class TeamsRelayAdapter:
        """Hermes platform adapter connecting to Microsoft Teams via credential proxy."""

        def __init__(self, config: dict[str, Any] | None = None) -> None:
            self.config = config or {}
            self.extra = self.config.get("extra") or {}
            self.adaptive_cards = self.extra.get("adaptive_cards", True)
            self.typing_status_text = self.config.get("typing_status_text", "Kage is thinking…")
            self.configured_tenant_id = os.getenv("TEAMS_TENANT_ID", "").strip() or None
            self.allowed_users = parse_allowed_users(os.getenv("TEAMS_ALLOWED_USERS", ""))
            self.allow_all_users = os.getenv("TEAMS_ALLOW_ALL_USERS", "false").lower() in ("true", "1", "yes")
            self._running = False
            self._relay_task: asyncio.Task[None] | None = None
            self._handler: Callable[[str, str, dict[str, Any]], Any] | None = None

        def set_handler(self, handler: Callable[[str, str, dict[str, Any]], Any]) -> None:
            self._handler = handler

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            self._running = True
            self._relay_task = asyncio.create_task(self._relay_loop())
            LOGGER.info("Teams adapter connected through credential proxy relay")
            return True

        async def disconnect(self) -> None:
            self._running = False
            if self._relay_task:
                self._relay_task.cancel()
                try:
                    await self._relay_task
                except asyncio.CancelledError:
                    pass
            LOGGER.info("Teams adapter disconnected")

        async def send(
            self,
            chat_id: str,
            message: str,
            *,
            thread_id: str | None = None,
            metadata: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            meta = metadata or {}
            service_url = meta.get("serviceUrl") or os.getenv("TEAMS_DEFAULT_SERVICE_URL", "https://smba.trafficmanager.net/teams/")
            conversation_id = chat_id

            payload = format_teams_activity_payload(
                message,
                use_adaptive_cards=self.adaptive_cards,
                title=meta.get("title"),
                actions=meta.get("actions"),
            )
            if thread_id:
                payload["replyToId"] = thread_id

            endpoint = f"v3/conversations/{conversation_id}/activities"
            return await asyncio.to_thread(
                request,
                "/v1/chat/teams/api",
                {
                    "serviceUrl": service_url,
                    "endpoint": endpoint,
                    "method": "POST",
                    "payload": payload,
                },
            )

        async def send_typing(self, chat_id: str, service_url: str) -> None:
            """Send a typing indicator activity to Teams."""
            endpoint = f"v3/conversations/{chat_id}/activities"
            payload = {"type": "typing"}
            try:
                await asyncio.to_thread(
                    request,
                    "/v1/chat/teams/api",
                    {
                        "serviceUrl": service_url,
                        "endpoint": endpoint,
                        "method": "POST",
                        "payload": payload,
                    },
                )
            except Exception:
                LOGGER.debug("Failed to send typing indicator to Teams", exc_info=True)

        async def _process_activity(self, activity: dict[str, Any]) -> None:
            activity_type = activity.get("type")
            if activity_type != "message":
                LOGGER.debug("Ignoring non-message Teams activity type: %s", activity_type)
                return

            channel_data = activity.get("channelData") or {}
            tenant = channel_data.get("tenant") or {}
            tenant_id = tenant.get("id") or activity.get("tenantId")

            # 1. Single-tenant check
            if not is_tenant_allowed(tenant_id, self.configured_tenant_id):
                LOGGER.warning(
                    "Rejected Teams activity from unauthorized tenant: %s (expected %s)",
                    tenant_id,
                    self.configured_tenant_id,
                )
                return

            # 2. User allowlist check
            sender = activity.get("from") or {}
            sender_id = str(sender.get("id", ""))
            sender_name = sender.get("name")
            sender_email = sender.get("email") or sender.get("userPrincipalName")

            if not is_user_allowed(
                sender_id=sender_id,
                sender_name=sender_name,
                sender_email=sender_email,
                allowed_users=self.allowed_users,
                allow_all=self.allow_all_users,
            ):
                LOGGER.warning("Rejected Teams activity from unauthorized user: %s (%s)", sender_id, sender_name)
                service_url = activity.get("serviceUrl", "https://smba.trafficmanager.net/teams/")
                conv = activity.get("conversation") or {}
                conv_id = conv.get("id", "")
                if conv_id:
                    await self.send(
                        conv_id,
                        "Sorry, you are not authorized to interact with this platform agent.",
                        metadata={"serviceUrl": service_url},
                    )
                return

            # 3. Handle message text & dispatch
            text = activity.get("text", "").strip()
            service_url = activity.get("serviceUrl", "https://smba.trafficmanager.net/teams/")
            conv = activity.get("conversation") or {}
            conv_id = str(conv.get("id", ""))
            activity_id = str(activity.get("id", ""))

            # Fire typing indicator
            await self.send_typing(conv_id, service_url)

            if self._handler:
                metadata = {
                    "serviceUrl": service_url,
                    "activityId": activity_id,
                    "tenantId": tenant_id,
                    "sender": sender,
                }
                await self._handler(conv_id, text, metadata)

        async def _relay_loop(self) -> None:
            while self._running:
                receipt = ""
                try:
                    response = await asyncio.to_thread(request, "/v1/chat/teams/events")
                    event_data = response.get("event")
                    if not event_data:
                        continue
                    receipt = str(event_data.get("receipt", ""))
                    activity = event_data.get("event") or {}
                    await self._process_activity(activity)
                    if receipt:
                        await asyncio.to_thread(
                            request, "/v1/chat/teams/events/ack", {"receipt": receipt}
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.warning("Teams relay event loop error", exc_info=True)
                    if receipt:
                        try:
                            await asyncio.to_thread(
                                request, "/v1/chat/teams/events/nack", {"receipt": receipt}
                            )
                        except Exception:
                            LOGGER.debug("Failed to nack receipt %s", receipt, exc_info=True)
                    await asyncio.sleep(2)

    from gateway.platform_registry import PlatformRegistry

    original_registry_create = PlatformRegistry.create_adapter
    if not getattr(PlatformRegistry, "_teams_relay_patched", False):

        def create_adapter(self: Any, name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "teams":
                config = kwargs.get("config") if "config" in kwargs else (args[0] if args else {})
                return TeamsRelayAdapter(config)
            return original_registry_create(self, name, *args, **kwargs)

        PlatformRegistry.create_adapter = create_adapter
        PlatformRegistry._teams_relay_patched = True
