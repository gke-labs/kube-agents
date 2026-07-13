"""Credential-free relay mode for Hermes' bundled Google Chat adapter."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import urllib.request
from typing import Any


LOGGER = logging.getLogger("google-chat-relay-patch")


def install() -> None:
    relay_url = os.getenv("GOOGLE_CHAT_RELAY_URL", "").rstrip("/")
    if not relay_url:
        return

    from gateway.platforms.base import SendResult

    def request(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            relay_url + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        with urllib.request.urlopen(req, timeout=35) as response:
            return json.load(response)

    async def relay_loop(self: Any) -> None:
        while not self._shutting_down:
            receipt = ""
            try:
                response = await asyncio.to_thread(request, "/v1/chat/events")
                event = response.get("event")
                if not event:
                    continue
                receipt = str(event["receipt"])
                envelope = json.loads(base64.b64decode(event["data"]))
                ce_type = (event.get("attributes") or {}).get("ce-type", "")
                extracted = self._extract_message_payload(envelope, ce_type)
                if extracted is not None:
                    message, space, _ = extracted
                    if (message.get("sender") or {}).get("type") != "BOT":
                        enriched = dict(envelope)
                        if "space" not in enriched and space:
                            enriched["space"] = space
                        if "space" not in message and space:
                            message = {**message, "space": space}
                        await self._dispatch_message(message, enriched)
                await asyncio.to_thread(
                    request, "/v1/chat/events/ack", {"receipt": receipt}
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("Google Chat relay receive failed", exc_info=True)
                if receipt:
                    try:
                        await asyncio.to_thread(
                            request, "/v1/chat/events/nack", {"receipt": receipt}
                        )
                    except Exception:
                        pass
                await asyncio.sleep(2)

    def patch_adapter_class(adapter_class: type[Any]) -> None:
        if getattr(adapter_class, "_credential_proxy_relay_patched", False):
            return
        original_connect = adapter_class.connect
        original_disconnect = adapter_class.disconnect
        original_create = adapter_class._create_message
        original_patch = adapter_class._patch_message
        original_delete = adapter_class.delete_message
        original_setup = adapter_class._handle_setup_files_command

        async def connect(self: Any, *, is_reconnect: bool = False) -> bool:
            if not os.getenv("GOOGLE_CHAT_RELAY_URL"):
                return await original_connect(self, is_reconnect=is_reconnect)
            self._loop = asyncio.get_running_loop()
            self._shutting_down = False
            self._relay_task = asyncio.create_task(relay_loop(self))
            self._mark_connected()
            LOGGER.info("Google Chat connected through credential proxy relay")
            return True

        async def disconnect(self: Any) -> None:
            if not os.getenv("GOOGLE_CHAT_RELAY_URL"):
                await original_disconnect(self)
                return
            self._shutting_down = True
            task = getattr(self, "_relay_task", None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._mark_disconnected()

        async def create_message(
            self: Any, chat_id: str, body: dict[str, Any]
        ) -> SendResult:
            if not os.getenv("GOOGLE_CHAT_RELAY_URL"):
                return await original_create(self, chat_id, body)
            response = await asyncio.to_thread(
                request,
                "/v1/chat/messages/create",
                {"parent": chat_id, "body": body},
            )
            message = response.get("message") or {}
            return SendResult(success=True, message_id=message.get("name"))

        async def patch_message(
            self: Any, name: str, body: dict[str, Any]
        ) -> SendResult:
            if not os.getenv("GOOGLE_CHAT_RELAY_URL"):
                return await original_patch(self, name, body)
            response = await asyncio.to_thread(
                request,
                "/v1/chat/messages/patch",
                {"name": name, "body": body},
            )
            message = response.get("message") or {}
            return SendResult(success=True, message_id=message.get("name", name))

        async def delete_message(self: Any, chat_id: str, message_id: str) -> bool:
            if not os.getenv("GOOGLE_CHAT_RELAY_URL"):
                return await original_delete(self, chat_id, message_id)
            await asyncio.to_thread(
                request, "/v1/chat/messages/delete", {"name": message_id}
            )
            return True

        async def setup_files(
            self: Any,
            chat_id: str,
            thread_id: str | None,
            raw_text: str,
            sender_email: str | None = None,
        ) -> bool:
            if not os.getenv("GOOGLE_CHAT_RELAY_URL"):
                return await original_setup(
                    self, chat_id, thread_id, raw_text, sender_email=sender_email
                )
            await self.send(
                chat_id,
                "File attachment setup is temporarily unavailable while OAuth storage moves to the credential proxy.",
                metadata={"thread_id": thread_id} if thread_id else None,
            )
            return True

        adapter_class.connect = connect
        adapter_class.disconnect = disconnect
        adapter_class._create_message = create_message
        adapter_class._patch_message = patch_message
        adapter_class.delete_message = delete_message
        adapter_class._handle_setup_files_command = setup_files
        adapter_class._credential_proxy_relay_patched = True

    # Patch the ordinary import path for direct adapter users.
    from plugins.platforms.google_chat.adapter import GoogleChatAdapter

    patch_adapter_class(GoogleChatAdapter)

    # Hermes loads bundled plugins under a generated hermes_plugins.* package,
    # producing a second GoogleChatAdapter class. Patch the factory result so
    # both import paths use the relay without depending on loader internals.
    from gateway.platform_registry import PlatformRegistry

    original_registry_create = PlatformRegistry.create_adapter
    if not getattr(PlatformRegistry, "_credential_proxy_relay_patched", False):

        def create_adapter(
            self: Any, name: str, config: Any
        ) -> Any:
            adapter = original_registry_create(self, name, config)
            if name == "google_chat" and adapter is not None:
                patch_adapter_class(type(adapter))
            return adapter

        PlatformRegistry.create_adapter = create_adapter
        PlatformRegistry._credential_proxy_relay_patched = True
