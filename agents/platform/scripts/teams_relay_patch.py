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
from slack_relay_patch import is_silent_text

LOGGER = logging.getLogger("teams-relay-patch")
DEFAULT_RELAY_READY_TIMEOUT = 120.0
# The activity budget is modelled on Hermes' bundled Teams adapter's 28,000-
# character limit; Microsoft's own figure is about 100 KB of UTF-16 with 80 KB
# advised, so 28 KB of UTF-8 is under the limit in every script and can rise
# once a live post measures it. ``send`` splits a reply into ``TEAMS_MAX_MESSAGE_LENGTH``-character
# chunks (the send tool and the streaming consumer split at the same count),
# and the count is sized for four bytes per character, so a chunk always fits
# as plain text. A card adds a TextBlock per line, so ``send`` still measures
# what it is about to post, drops a card to plain text, and only cuts text
# (with a marker) that escapes to more than four bytes a character.
TEAMS_MAX_MESSAGE_LENGTH = 7000
TEAMS_MAX_ACTIVITY_BYTES = 28 * 1024
TEAMS_TRUNCATION_MARKER = "\n… (message truncated to fit Teams' size limit)"
# Where the bot posts when an activity did not say: the public Bot Framework
# endpoint. TEAMS_DEFAULT_SERVICE_URL overrides it in a hand-run gateway only.
DEFAULT_TEAMS_SERVICE_URL = "https://smba.trafficmanager.net/teams/"
# How much of a broker error body the gateway reads to name the cause.
TEAMS_RELAY_ERROR_BODY_MAX_BYTES = 4096
# One relay call can cost the broker an identity-provider round trip (20 s)
# and then the Bot Framework post (30 s), so the gateway waits longer than
# their sum; a shorter wait reports a post that then lands as a failure.
TEAMS_RELAY_REQUEST_TIMEOUT_SECONDS = 60
# A typing indicator is fire-and-forget: the base abandons it after 1.5 s of
# its 2 s tick, and a worker thread left blocking for the full relay timeout
# on a stalled broker would hold an executor slot the reply itself needs.
TEAMS_TYPING_REQUEST_TIMEOUT_SECONDS = 5
TEAMS_TRUSTED_SERVICE_HOSTS = ("smba.trafficmanager.net", "api.botframework.com")
# Bot Framework `conversation.conversationType` -> gateway `chat_type`. The
# memory providers treat only "dm" as single-person, so a Teams 1:1 chat
# (`personal`) must arrive as "dm" for personal memory to stay on.
TEAMS_CONVERSATION_TYPE_TO_CHAT_TYPE = {
    "personal": "dm",
    "groupchat": "group",
    "channel": "channel",
}
TEAMS_DEFAULT_CHAT_TYPE = "channel"


def default_service_url() -> str:
    """Where a post goes when nothing named a service URL."""
    return os.getenv("TEAMS_DEFAULT_SERVICE_URL", DEFAULT_TEAMS_SERVICE_URL)


def encodable_text(text: str) -> str:
    """``text`` with any lone surrogate replaced, so UTF-8 serialization cannot raise.

    A ``\\ud83d`` escape that survives JSON decoding of a model reply is a
    code point UTF-8 refuses; measuring or posting it would raise instead of
    delivering the rest of the message.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", errors="replace").decode("utf-8")
    return text


def relay_enabled() -> bool:
    """Return True when TEAMS_RELAY_URL is configured in this process."""
    return bool(os.getenv("TEAMS_RELAY_URL", "").strip())


def is_trusted_service_url(service_url: str) -> bool:
    """Return True if service_url uses HTTPS on an official Bot Framework host."""
    if not service_url or not isinstance(service_url, str):
        return False
    if "?" in service_url or "#" in service_url:
        return False
    parsed = urllib.parse.urlparse(service_url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    return (
        host in TEAMS_TRUSTED_SERVICE_HOSTS
        or host == "botframework.com"
        or host.endswith(".botframework.com")
    )


def parse_allowed_users(raw: str) -> set[str]:
    """Parse comma-separated allowed user IDs into a normalized set.

    Entries are matched against ``from.id``, ``from.aadObjectId`` and, if a
    producer ever adds them, ``from.userPrincipalName`` / ``from.email``. An
    inbound Bot Framework activity carries no UPN or email, so list Object IDs.
    """
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


class TeamsRelayError(RuntimeError):
    """The broker refused or Microsoft rejected a relayed call.

    Its message carries the broker's answer, so the gateway log and the
    standalone sender's result name the cause (``BotNotInConversationRoster``,
    ``ConversationNotFound``, ``invalid_client``) instead of the bare
    ``HTTP Error 502`` ``urlopen`` raises with the body unread.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def relayed_teams_error(exc: urllib.error.HTTPError) -> TeamsRelayError:
    """Read the broker's error body once and turn it into a ``TeamsRelayError``.

    The detail is the broker's ``teams`` object when Microsoft was the one
    refusing (status, code, message), else its ``error`` / ``detail``.
    """
    body: Any = {}
    try:
        raw = exc.read(TEAMS_RELAY_ERROR_BODY_MAX_BYTES)
        body = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    fields = body.get("teams") if isinstance(body.get("teams"), dict) else {}
    if not fields:
        fields = {k: body[k] for k in ("error", "detail") if body.get(k)}
    detail = " ".join(str(v) for v in fields.values() if v) or str(exc.reason)
    return TeamsRelayError(f"HTTP {exc.code}: {detail}", exc.code)


def activity_size_bytes(payload: dict[str, Any]) -> int:
    """The bytes the broker puts on the wire for ``payload`` (UTF-8 JSON)."""
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def fit_activity_to_cap(
    message: str,
    *,
    use_adaptive_cards: bool,
    title: str | None,
    actions: list[dict[str, Any]] | None,
    extra: dict[str, Any] | None = None,
    cap: int = TEAMS_MAX_ACTIVITY_BYTES,
) -> dict[str, Any]:
    """Build the outbound activity and keep it under ``cap`` bytes.

    Order: the card as asked; plain text of the same message (a card adds a
    TextBlock per line, so short lines inflate it several times); then the
    longest prefix that fits, ending in ``TEAMS_TRUNCATION_MARKER``. Every
    candidate carries ``extra`` (``replyToId`` and the like) and is measured
    in its serialized form, the one the broker puts on the wire.
    """
    fields = dict(extra or {})

    def build(text: str, cards: bool, with_title: bool) -> dict[str, Any]:
        payload = format_teams_activity_payload(
            text,
            use_adaptive_cards=cards,
            title=title if with_title else None,
            actions=actions if with_title else None,
        )
        payload.update(fields)
        return payload

    payload = build(message, use_adaptive_cards, True)
    if activity_size_bytes(payload) <= cap:
        return payload
    if use_adaptive_cards:
        LOGGER.info("Teams card over the activity size cap; sending plain text")
        payload = build(message, False, False)
        if activity_size_bytes(payload) <= cap:
            return payload
    LOGGER.warning("Teams message over the activity size cap; truncated to fit")
    # The longest prefix that fits with the marker: serialized size grows with
    # the prefix, so a binary search over the character count lands on it.
    low, high = 0, len(message)
    while low < high:
        mid = (low + high + 1) // 2
        if activity_size_bytes(build(message[:mid] + TEAMS_TRUNCATION_MARKER, False, False)) <= cap:
            low = mid
        else:
            high = mid - 1
    return build(message[:low] + TEAMS_TRUNCATION_MARKER, False, False)


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


def _resolve_base_classes() -> tuple[type, Any, Any, Any, Any, Any]:
    """Return (BasePlatformAdapter, MessageEvent, MessageType, SendResult, Platform, PlatformConfig) from gateway."""
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )

    return (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
        Platform,
        PlatformConfig,
    )


def _build_teams_adapter_and_sender(
    relay_url: str,
) -> tuple[type, Callable[..., Any]]:
    """Build TeamsRelayAdapter and _standalone_send bound to relay_url."""
    (
        base_adapter_cls,
        message_event_cls,
        message_type_cls,
        send_result_cls,
        platform_factory,
        platform_config_cls,
    ) = _resolve_base_classes()

    def request(
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = TEAMS_RELAY_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", **authorization_headers()}
        req = urllib.request.Request(
            relay_url + path,
            data=body,
            headers=headers,
            method="GET" if body is None else "POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise relayed_teams_error(exc) from exc

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
            except TeamsRelayError as exc:
                if exc.status == 404:
                    # 404 from /events/ack means TeamsRelay is initialized and answered.
                    return
                if exc.status != 503:
                    raise
            except (urllib.error.URLError, OSError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("Teams relay bootstrap timed out")
            LOGGER.info("Teams relay is not ready yet; retrying bootstrap")
            await asyncio.sleep(2)

    class TeamsRelayAdapter(base_adapter_cls):  # type: ignore[misc,valid-type]
        """Hermes platform adapter connecting to Microsoft Teams via credential proxy."""

        # The streaming consumer splits at this; the base default is 4096. The
        # final-text path does not split at all, so ``send`` does (as Hermes'
        # bundled Teams adapter does) and says so with ``splits_long_messages``.
        MAX_MESSAGE_LENGTH = TEAMS_MAX_MESSAGE_LENGTH
        splits_long_messages = True

        def __init__(self, config: Any = None) -> None:
            plat = platform_factory("teams")
            resolved_config = (
                config if config is not None else platform_config_cls(enabled=True)
            )
            super().__init__(resolved_config, plat)
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
            # Conversation -> the monotonic deadline of its typing post in
            # flight (see send_typing).
            self._typing_in_flight: dict[str, float] = {}

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
            message: str = "",
            reply_to: str | None = None,
            metadata: dict[str, Any] | None = None,
            *,
            content: str | None = None,
            thread_id: str | None = None,
            **kwargs: Any,
        ) -> Any:
            del kwargs
            text = content if content is not None else message
            meta = metadata if isinstance(metadata, dict) else {}
            fallback_url = default_service_url()
            candidate_url = meta.get("serviceUrl") or fallback_url
            service_url = (
                candidate_url if is_trusted_service_url(candidate_url) else fallback_url
            )
            conversation_id = str(chat_id or "").strip()

            effective_thread = (
                thread_id
                or reply_to
                or (
                    str(meta.get("thread_id")).strip()
                    if meta.get("thread_id")
                    else None
                )
            )
            extra = {"replyToId": effective_thread} if effective_thread else None
            endpoint = f"v3/conversations/{conversation_id}/activities"
            # The final-text path hands the whole reply over unsplit; chunk it
            # here as the bundled adapter does, then size each chunk in bytes.
            text = encodable_text(text)
            chunks = self.truncate_message(text, TEAMS_MAX_MESSAGE_LENGTH) or [text]
            message_id = None
            raw_dict: dict[str, Any] = {}
            for chunk in chunks:
                payload = fit_activity_to_cap(
                    chunk,
                    use_adaptive_cards=self.adaptive_cards,
                    title=meta.get("title"),
                    actions=meta.get("actions"),
                    extra=extra,
                )
                try:
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
                except (TeamsRelayError, urllib.error.URLError, OSError, ValueError) as error:
                    # A broker refusal arrives as TeamsRelayError with the cause
                    # read from its body. The gateway's send and standalone paths
                    # expect a failed SendResult, not an exception.
                    LOGGER.warning("Teams send to %r failed: %s", conversation_id, error)
                    return send_result_cls(success=False, error=str(error))
                raw_dict = raw if isinstance(raw, dict) else {}
                inner = (
                    raw_dict.get("response")
                    if isinstance(raw_dict.get("response"), dict)
                    else raw_dict
                )
                message_id = (
                    str(inner.get("id"))
                    if isinstance(inner, dict) and inner.get("id")
                    else None
                )
            return send_result_cls(
                success=True,
                message_id=message_id,
                raw_response=raw_dict,
            )

        async def send_typing(
            self, chat_id: str, metadata: dict[str, Any] | None = None
        ) -> None:
            """Send a typing indicator activity to Teams (the base's signature)."""
            meta = metadata if isinstance(metadata, dict) else {}
            service_url = meta.get("serviceUrl") or default_service_url()
            if not is_trusted_service_url(service_url):
                return
            # The base refreshes the indicator every 2 s and cancels each call
            # after 1.5 s, which does not stop the worker thread. A tick that
            # lands while the previous post is still running is skipped, so a
            # stalled broker holds one worker per conversation, not one per
            # tick. The worker clears the mark when its post ends; the mark
            # also expires with the post's timeout, so a tick cancelled before
            # its worker ever ran (queued behind a busy executor) cannot leave
            # the conversation without an indicator.
            now = time.monotonic()
            for stale in [c for c, d in self._typing_in_flight.items() if d <= now]:
                self._typing_in_flight.pop(stale, None)
            if chat_id in self._typing_in_flight:
                return
            deadline = now + TEAMS_TYPING_REQUEST_TIMEOUT_SECONDS
            self._typing_in_flight[chat_id] = deadline
            endpoint = f"v3/conversations/{chat_id}/activities"
            payload = {"type": "typing"}

            def post_typing() -> None:
                try:
                    request(
                        "/v1/chat/teams/api",
                        {
                            "serviceUrl": service_url,
                            "endpoint": endpoint,
                            "method": "POST",
                            "payload": payload,
                        },
                        timeout=TEAMS_TYPING_REQUEST_TIMEOUT_SECONDS,
                    )
                finally:
                    # Only this tick's own mark: a post that outlives its
                    # deadline must not clear the mark a later tick set.
                    if self._typing_in_flight.get(chat_id) == deadline:
                        self._typing_in_flight.pop(chat_id, None)

            try:
                await asyncio.to_thread(post_typing)
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
            if raw_service_url is not None and not is_trusted_service_url(raw_service_url):
                LOGGER.warning(
                    "Rejected Teams activity with untrusted serviceUrl: %r",
                    raw_service_url,
                )
                return
            service_url = raw_service_url or default_service_url()

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
                chat_type=TEAMS_CONVERSATION_TYPE_TO_CHAT_TYPE.get(
                    str(conv.get("conversationType") or "").strip().lower(),
                    TEAMS_DEFAULT_CHAT_TYPE,
                ),
                user_id=sender_aad_id or sender_id,
                user_name=sender_name or sender_id,
            )
            event = message_event_cls(
                text=text,
                message_type=message_type_cls.TEXT,
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
        """The registry's standalone sender: a ``success`` or ``error`` dict, never a raise.

        ``hermes send`` and the send tool call this out of process; the cron
        lane would too, but only for an entry that names a cron home-channel
        variable, which neither Teams registration does. Same contract as the
        Slack sender: a silent tick (the dressed ``[SILENT]`` marker upstream
        lets through) is a logged success with nothing posted; a failure
        returns the cause. A file cannot ride the relay: the send tool already
        warns that media is omitted for Teams and still hands the files over
        with the last chunk, so they are dropped here and the text is posted.
        """
        del force_document, caption
        if media_files:
            LOGGER.info("teams relay: %d media file(s) omitted; the relay carries text only", len(media_files))
        if is_silent_text(message):
            LOGGER.info(
                "teams relay: dropped a %d-character message as a silent tick",
                len(str(message or "")),
            )
            skipped: dict[str, Any] = {"success": True, "platform": "teams", "skipped": "empty_text"}
            if media_files:
                skipped["media_omitted"] = len(media_files)
            return skipped
        try:
            adapter = TeamsRelayAdapter(pconfig)
            result = await adapter.send(
                chat_id,
                message,
                thread_id=thread_id,
                metadata=metadata,
            )
        except Exception as exc:
            LOGGER.warning("teams relay: standalone send failed: %s", exc)
            return {"error": f"Teams send failed: {exc}"}
        if not result.success:
            LOGGER.warning("teams relay: standalone send failed: %s", result.error)
            return {"error": f"Teams send failed: {result.error}"}
        answer: dict[str, Any] = {"success": True, "message_id": result.message_id}
        if media_files:
            answer["media_omitted"] = len(media_files)
        return answer

    return TeamsRelayAdapter, _standalone_send


def register_plugin(ctx: Any) -> None:
    """Register the Microsoft Teams platform plugin through PluginContext."""
    relay_url = os.getenv("TEAMS_RELAY_URL", "").rstrip("/")

    def _adapter_factory(config: Any) -> Any:
        url = os.getenv("TEAMS_RELAY_URL", "").rstrip("/") or relay_url
        adapter_cls, _ = _build_teams_adapter_and_sender(url)
        return adapter_cls(config)

    async def _standalone_sender(
        pconfig: Any, chat_id: str, message: str, **kwargs: Any
    ) -> Any:
        url = os.getenv("TEAMS_RELAY_URL", "").rstrip("/") or relay_url
        _, sender_fn = _build_teams_adapter_and_sender(url)
        return await sender_fn(pconfig, chat_id, message, **kwargs)

    ctx.register_platform(
        name="teams",
        label="Microsoft Teams",
        adapter_factory=_adapter_factory,
        check_fn=relay_enabled,
        is_connected=lambda _config=None: relay_enabled(),
        install_hint="",
        standalone_sender_fn=_standalone_sender,
        max_message_length=TEAMS_MAX_MESSAGE_LENGTH,
    )


def install() -> None:
    """Install the Teams relay patch onto Hermes PlatformRegistry."""
    relay_url = os.getenv("TEAMS_RELAY_URL", "").rstrip("/")
    if not relay_url:
        return

    teams_adapter_cls, standalone_send = _build_teams_adapter_and_sender(relay_url)

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
                return teams_adapter_cls(config)
            return original_registry_create(self, name, *args, **kwargs)

        PlatformRegistry.create_adapter = create_adapter
        PlatformRegistry._teams_relay_patched = True

    platform_entry_cls = getattr(registry_module, "PlatformEntry", None)
    singleton = getattr(registry_module, "platform_registry", None)
    if platform_entry_cls is not None and singleton is not None:
        try:
            existing = (
                singleton.get("teams")
                if callable(getattr(singleton, "get", None))
                else None
            )
            if existing is None:
                entry_fields = {
                    "name": "teams",
                    "label": "Microsoft Teams",
                    "adapter_factory": lambda cfg: teams_adapter_cls(cfg),
                    "check_fn": lambda: True,
                    "is_connected": lambda _cfg=None: True,
                    "standalone_sender_fn": standalone_send,
                }
                # Match the plugin's register_platform entry so the limit does
                # not depend on which of the two registrations Hermes keeps.
                if "max_message_length" in getattr(
                    platform_entry_cls, "__dataclass_fields__", {}
                ):
                    entry_fields["max_message_length"] = TEAMS_MAX_MESSAGE_LENGTH
                entry = platform_entry_cls(**entry_fields)
                singleton.register(entry)
        except Exception:
            LOGGER.warning("Teams PlatformEntry registration failed", exc_info=True)
