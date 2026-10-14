"""Credential-free Microsoft Teams / Bot Framework transport for Hermes' adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from credential_proxy_client import authorization_headers

LOGGER = logging.getLogger("teams-relay-patch")
DEFAULT_RELAY_READY_TIMEOUT = 120.0
# Bot Framework caps one activity at about 28 KB; the gateway chunks outbound
# text against the registry entry's limit, so the entry has to carry it.
TEAMS_MAX_MESSAGE_LENGTH = 28000
TEAMS_TRUSTED_SERVICE_HOSTS = ("smba.trafficmanager.net", "api.botframework.com")


def is_trusted_service_url(service_url: str) -> bool:
    """Return True if service_url uses HTTPS on an official Bot Framework host."""
    if not service_url or not isinstance(service_url, str):
        return False
    parsed = urllib.parse.urlparse(service_url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    # The broker appends the endpoint to this URL by string concatenation; a
    # `?` or `#` would turn the endpoint into a query string or a fragment.
    if "?" in service_url or "#" in service_url:
        return False
    host = (parsed.hostname or "").lower()
    return (
        host in TEAMS_TRUSTED_SERVICE_HOSTS
        or host == "botframework.com"
        or host.endswith(".botframework.com")
    )


def parse_allowed_users(raw: str) -> set[str]:
    """Parse comma-separated allowed user IDs or emails into a normalized set."""
    if not raw:
        return set()
    return {user.strip().lower() for user in raw.split(",") if user.strip()}


def is_user_allowed(
    sender_id: str,
    sender_name: str | None = None,
    sender_email: str | None = None,
    allowed_users: set[str] | None = None,
    allow_all: bool = False,
    *,
    sender_aad_id: str | None = None,
) -> bool:
    """Check if the sender is authorized to interact with the Teams bot.

    Display names (``sender_name`` / ``from.name``) are never used for
    authorization because they are user-mutable and non-unique across tenants.
    """
    del sender_name
    if allow_all:
        return True
    if not allowed_users:
        return False
    candidates: set[str] = set()
    for identifier in (sender_id, sender_aad_id, sender_email):
        if isinstance(identifier, str) and identifier.strip():
            candidates.add(identifier.strip().lower())
    return bool(candidates & allowed_users)


def extract_activity_tenant_id(activity: dict[str, Any]) -> str | None:
    """Extract the Entra tenant ID from channelData.tenant.id or conversation.tenantId."""
    if not isinstance(activity, dict):
        return None
    channel_data = activity.get("channelData")
    if isinstance(channel_data, dict):
        tenant = channel_data.get("tenant")
        if isinstance(tenant, dict):
            tid = tenant.get("id")
            if isinstance(tid, str) and tid.strip():
                return tid.strip()
    conv = activity.get("conversation")
    if isinstance(conv, dict):
        tid = conv.get("tenantId")
        if isinstance(tid, str) and tid.strip():
            return tid.strip()
    return None


def is_tenant_allowed(
    activity_tenant_id: str | None, configured_tenant_id: str | None
) -> bool:
    """Check if the activity originates from the configured Microsoft Entra ID tenant."""
    if not configured_tenant_id:
        return True
    if not activity_tenant_id or not isinstance(activity_tenant_id, str):
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

        # Markdown needs whitespace after the hashes; `#1040` or `#!/bin/sh` is text.
        if stripped.startswith("#") and stripped.lstrip("#")[:1] == " ":
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
            body_elements.append(
                {
                    "type": "TextBlock",
                    "text": f"• {stripped[2:].strip()}",
                    "wrap": True,
                }
            )
        else:
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
        "body": (
            body_elements
            if body_elements
            else [{"type": "TextBlock", "text": text, "wrap": True}]
        ),
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

    # Imported unconditionally, as the Slack relay patch does: install() only
    # runs inside a gateway process, and a gateway whose base classes cannot be
    # imported is a failure to see in the log, not one to paper over.
    from gateway.config import Platform
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )

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

    async def bootstrap_relay() -> None:
        """Wait for the credential-proxy sidecar Teams relay to become reachable."""
        try:
            wait_seconds = float(
                os.getenv(
                    "TEAMS_RELAY_BOOTSTRAP_WAIT_SECONDS",
                    str(DEFAULT_RELAY_READY_TIMEOUT),
                )
            )
        except ValueError:
            LOGGER.warning(
                "Invalid TEAMS_RELAY_BOOTSTRAP_WAIT_SECONDS; using the default"
            )
            wait_seconds = DEFAULT_RELAY_READY_TIMEOUT
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                await asyncio.to_thread(
                    request, "/v1/chat/teams/events/ack", {"receipt": ""}
                )
                return
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    # 404 from /events/ack means TeamsRelay is initialized and answered.
                    return
                if exc.code != 503:
                    raise
            except (urllib.error.URLError, OSError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("Teams relay bootstrap timed out")
            LOGGER.info("Teams relay is not ready yet; retrying bootstrap")
            await asyncio.sleep(2)

    class TeamsRelayAdapter(BasePlatformAdapter):
        """Hermes platform adapter connecting to Microsoft Teams via credential proxy."""

        def __init__(self, config: Any = None) -> None:
            # ``Platform._missing_`` admits a name the platform registry knows,
            # and install() registers "teams" before anything builds an adapter.
            super().__init__(config, Platform("teams"))
            self.config = config if config is not None else {}
            if isinstance(self.config, dict):
                raw_extra = self.config.get("extra")
                typing_text = self.config.get("typing_status_text")
            else:
                raw_extra = getattr(self.config, "extra", None)
                typing_text = getattr(self.config, "typing_status_text", None)
            self.extra: dict[str, Any] = (
                raw_extra if isinstance(raw_extra, dict) else {}
            )
            self.adaptive_cards = bool(self.extra.get("adaptive_cards", True))
            self.typing_status_text = (
                typing_text
                if isinstance(typing_text, str) and typing_text
                else "Kage is thinking…"
            )
            self.configured_tenant_id = (
                os.getenv("TEAMS_TENANT_ID", "").strip() or None
            )
            self.allowed_users = parse_allowed_users(
                os.getenv("TEAMS_ALLOWED_USERS", "")
            )
            self.allow_all_users = os.getenv(
                "TEAMS_ALLOW_ALL_USERS", "false"
            ).lower() in ("true", "1", "yes")
            self._running = False
            self._relay_task: asyncio.Task[None] | None = None
            self._handler: Callable[[str, str, dict[str, Any]], Any] | None = None

        def set_handler(
            self, handler: Callable[[str, str, dict[str, Any]], Any]
        ) -> None:
            self._handler = handler

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            del is_reconnect
            await bootstrap_relay()
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
            content: str,
            reply_to: str | None = None,
            metadata: dict[str, Any] | None = None,
            *,
            thread_id: str | None = None,
            **kwargs: Any,
        ) -> SendResult:
            # Same parameter order and names as ``BasePlatformAdapter.send``:
            # the send tool and the base helpers call ``content=`` by keyword.
            del kwargs
            message = content
            meta = metadata if isinstance(metadata, dict) else {}
            default_service_url = os.getenv(
                "TEAMS_DEFAULT_SERVICE_URL", "https://smba.trafficmanager.net/teams/"
            )
            candidate_url = meta.get("serviceUrl") or default_service_url
            service_url = (
                candidate_url
                if is_trusted_service_url(candidate_url)
                else default_service_url
            )
            conversation_id = str(chat_id or "").strip()

            payload = format_teams_activity_payload(
                message,
                use_adaptive_cards=self.adaptive_cards,
                title=meta.get("title"),
                actions=meta.get("actions"),
            )
            effective_thread = (
                thread_id
                or reply_to
                or (
                    str(meta.get("thread_id")).strip()
                    if meta.get("thread_id")
                    else None
                )
            )
            if effective_thread:
                payload["replyToId"] = effective_thread

            endpoint = f"v3/conversations/{conversation_id}/activities"
            raw = await asyncio.to_thread(
                request,
                "/v1/chat/teams/api",
                {
                    "serviceUrl": service_url,
                    "endpoint": endpoint,
                    "method": "POST",
                    "payload": payload,
                },
            )
            response = raw if isinstance(raw, dict) else {}
            inner = response.get("response")
            inner = inner if isinstance(inner, dict) else response
            message_id = inner.get("id")
            return SendResult(
                success=True,
                message_id=str(message_id) if message_id else None,
                raw_response=response,
            )

        async def send_typing(
            self, chat_id: str, metadata: dict[str, Any] | None = None
        ) -> None:
            """Send a typing indicator activity to Teams (base signature)."""
            meta = metadata if isinstance(metadata, dict) else {}
            service_url = meta.get("serviceUrl") or os.getenv(
                "TEAMS_DEFAULT_SERVICE_URL", "https://smba.trafficmanager.net/teams/"
            )
            if not is_trusted_service_url(service_url):
                return
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

        async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
            """Teams channel ids end in ``@thread.<suffix>``; the rest are chats."""
            chat_type = "channel" if "@thread" in str(chat_id) else "dm"
            return {"name": str(chat_id), "type": chat_type, "chat_id": str(chat_id)}

        async def _process_activity(self, activity: dict[str, Any]) -> None:
            if not isinstance(activity, dict):
                LOGGER.warning("Ignoring malformed non-object Teams activity")
                return

            activity_type = activity.get("type")
            if activity_type != "message":
                LOGGER.debug(
                    "Ignoring non-message Teams activity type: %s", activity_type
                )
                return

            for field_name in ("channelData", "from", "conversation"):
                val = activity.get(field_name)
                if val is not None and not isinstance(val, dict):
                    LOGGER.warning(
                        "Dropping malformed Teams activity: '%s' is not an object",
                        field_name,
                    )
                    return

            raw_text = activity.get("text")
            if raw_text is not None and not isinstance(raw_text, str):
                LOGGER.warning("Dropping malformed Teams activity: 'text' is not a string")
                return

            raw_service_url = activity.get("serviceUrl")
            default_service_url = os.getenv(
                "TEAMS_DEFAULT_SERVICE_URL", "https://smba.trafficmanager.net/teams/"
            )
            if raw_service_url is not None and not is_trusted_service_url(raw_service_url):
                LOGGER.warning(
                    "Rejected Teams activity with untrusted serviceUrl: %s",
                    raw_service_url,
                )
                return
            service_url = raw_service_url or default_service_url

            tenant_id = extract_activity_tenant_id(activity)

            # 1. Single-tenant check
            if not is_tenant_allowed(tenant_id, self.configured_tenant_id):
                LOGGER.warning(
                    "Rejected Teams activity from unauthorized tenant: %s (expected %s)",
                    tenant_id,
                    self.configured_tenant_id,
                )
                return

            # 2. User allowlist check (never matches mutable display name)
            sender = activity.get("from") or {}
            sender_id = str(sender.get("id") or "").strip()
            sender_aad_id = str(sender.get("aadObjectId") or "").strip() or None
            sender_name = str(sender.get("name") or "").strip() or None
            sender_email = (
                str(sender.get("userPrincipalName") or sender.get("email") or "").strip()
                or None
            )

            conv = activity.get("conversation") or {}
            conv_id = str(conv.get("id") or "").strip()

            if not is_user_allowed(
                sender_id=sender_id,
                sender_name=sender_name,
                sender_email=sender_email,
                allowed_users=self.allowed_users,
                allow_all=self.allow_all_users,
                sender_aad_id=sender_aad_id,
            ):
                LOGGER.warning(
                    "Rejected Teams activity from unauthorized user: id=%s aad=%s",
                    sender_id,
                    sender_aad_id,
                )
                if conv_id and is_trusted_service_url(service_url):
                    try:
                        await self.send(
                            conv_id,
                            "Sorry, you are not authorized to interact with this platform agent.",
                            metadata={"serviceUrl": service_url},
                        )
                    except Exception:
                        LOGGER.debug(
                            "Failed to send unauthorized notice to Teams",
                            exc_info=True,
                        )
                return

            if not conv_id:
                LOGGER.warning("Dropping Teams activity without conversation.id")
                return

            # 3. Handle message text & dispatch
            text = (raw_text or "").strip()
            activity_id = str(activity.get("id") or "").strip()

            await self.send_typing(conv_id, metadata={"serviceUrl": service_url})

            metadata = {
                "serviceUrl": service_url,
                "activityId": activity_id,
                "tenantId": tenant_id,
                "sender": sender,
            }
            if self._handler is not None:
                res = self._handler(conv_id, text, metadata)
                if asyncio.iscoroutine(res):
                    await res
                return

            source = self.build_source(
                chat_id=conv_id,
                chat_name=str(conv.get("name") or conv_id),
                chat_type=str(conv.get("conversationType") or "channel"),
                user_id=sender_aad_id or sender_id,
                user_name=sender_name or sender_id,
                thread_id=activity_id or None,
            )
            event = MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                raw_message=activity,
                message_id=activity_id,
            )
            await self.handle_message(event)

        async def _relay_loop(self) -> None:
            while self._running:
                receipt = ""
                try:
                    response = await asyncio.to_thread(
                        request, "/v1/chat/teams/events"
                    )
                    event_data = response.get("event")
                    if not event_data:
                        continue
                    receipt = str(event_data.get("receipt", ""))
                    activity = event_data.get("event")
                    await self._process_activity(
                        activity if isinstance(activity, dict) else {}
                    )
                    if receipt:
                        await asyncio.to_thread(
                            request,
                            "/v1/chat/teams/events/ack",
                            {"receipt": receipt},
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.warning("Teams relay event loop error", exc_info=True)
                    if receipt:
                        try:
                            await asyncio.to_thread(
                                request,
                                "/v1/chat/teams/events/nack",
                                {"receipt": receipt},
                            )
                        except Exception:
                            LOGGER.debug(
                                "Failed to nack receipt %s", receipt, exc_info=True
                            )
                    await asyncio.sleep(2)

    async def _standalone_send(
        pconfig: Any,
        chat_id: str,
        message: str,
        *,
        thread_id: str | None = None,
        media_files: Any = None,
        force_document: bool = False,
        caption: str | None = None,
        metadata: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Out-of-process delivery (cron); the registry expects a dict back."""
        del media_files, force_document, caption
        adapter = TeamsRelayAdapter(pconfig)
        result = await adapter.send(
            chat_id,
            message,
            thread_id=thread_id,
            metadata=metadata,
        )
        return {"success": result.success, "message_id": result.message_id}

    import gateway.platform_registry as registry_module
    from gateway.platform_registry import PlatformRegistry

    original_registry_create = PlatformRegistry.create_adapter
    if not getattr(PlatformRegistry, "_teams_relay_patched", False):

        def create_adapter(self: Any, name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "teams":
                config = (
                    kwargs.get("config")
                    if "config" in kwargs
                    else (args[0] if args else None)
                )
                return TeamsRelayAdapter(config)
            return original_registry_create(self, name, *args, **kwargs)

        PlatformRegistry.create_adapter = create_adapter
        PlatformRegistry._teams_relay_patched = True

    def patch_teams_entry(entry: Any) -> None:
        """Point a ``teams`` registry entry at the relay adapter.

        Hermes bundles its own Teams plugin (``plugins/platforms/teams``) whose
        entry wants ``TEAMS_CLIENT_ID``/``TEAMS_CLIENT_SECRET`` in the gateway,
        which the operator never renders. Its deferred registration lands in
        the profile-scoped map that ``get()`` consults first, so a second
        entry registered here would be shadowed. The Slack relay patch takes
        the same route: edit the entry the registry serves, whichever module
        registered it.
        """
        entry.adapter_factory = lambda cfg: TeamsRelayAdapter(cfg)
        entry.check_fn = lambda: True
        entry.validate_config = None
        entry.is_connected = lambda _cfg=None: True
        entry.standalone_sender_fn = _standalone_send
        entry.max_message_length = TEAMS_MAX_MESSAGE_LENGTH

    if not getattr(PlatformRegistry, "_teams_relay_entry_patched", False):
        original_registry_register = PlatformRegistry.register

        def register(self: Any, entry: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(entry, "name", None) == "teams":
                try:
                    patch_teams_entry(entry)
                except Exception:
                    LOGGER.warning("Teams relay entry patch failed", exc_info=True)
            # Forward blind: the registry's signature has grown a keyword-only
            # ``scope`` before, and a TypeError here loses every platform.
            return original_registry_register(self, entry, *args, **kwargs)

        PlatformRegistry.register = register
        PlatformRegistry._teams_relay_entry_patched = True

    # Registration order is not a contract. Sweep the live maps for an entry
    # already in place (read directly: ``get()`` would resolve the bundled
    # plugin's deferred loader and import it), and register one of our own
    # only when nothing, concrete or deferred, holds the name yet.
    singleton = registry_module.platform_registry
    maps = [getattr(singleton, "_entries", None)]
    maps.extend((getattr(singleton, "_scoped_entries", None) or {}).values())
    found = False
    for scope_entries in maps:
        if isinstance(scope_entries, dict) and scope_entries.get("teams") is not None:
            patch_teams_entry(scope_entries["teams"])
            found = True
    deferred = [getattr(singleton, "_deferred", None)]
    deferred.extend((getattr(singleton, "_scoped_deferred", None) or {}).values())
    pending = any(isinstance(d, dict) and "teams" in d for d in deferred)
    if not found and not pending:
        try:
            singleton.register(
                registry_module.PlatformEntry(
                    name="teams",
                    label="Microsoft Teams",
                    adapter_factory=lambda cfg: TeamsRelayAdapter(cfg),
                    check_fn=lambda: True,
                    is_connected=lambda _cfg=None: True,
                    standalone_sender_fn=_standalone_send,
                    max_message_length=TEAMS_MAX_MESSAGE_LENGTH,
                )
            )
        except Exception:
            # Without an entry, Platform("teams") cannot resolve and no
            # adapter is built: say so where an operator reads.
            LOGGER.warning(
                "Teams platform registration failed; the adapter is unavailable",
                exc_info=True,
            )
