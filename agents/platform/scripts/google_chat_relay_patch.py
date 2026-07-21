"""Credential-free Google Chat transport for Hermes' bundled adapter."""

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

    class RelayMessage:
        """Pub/Sub-shaped message that settles an opaque proxy receipt."""

        def __init__(self, event: dict[str, Any]) -> None:
            self.data = base64.b64decode(event["data"], validate=True)
            self.attributes = event.get("attributes") or {}
            self.message_id = str(event.get("messageId", ""))
            self._receipt = str(event["receipt"])
            self._settled = False

        def _settle(self, acknowledge: bool) -> None:
            if self._settled:
                return
            path = "/v1/chat/events/ack" if acknowledge else "/v1/chat/events/nack"
            request(path, {"receipt": self._receipt})
            self._settled = True

        def ack(self) -> None:
            self._settle(True)

        def nack(self) -> None:
            self._settle(False)

    class RemoteRequest:
        def __init__(
            self, resource: list[str], method: str, arguments: dict[str, Any]
        ) -> None:
            self.resource = resource
            self.method = method
            self.arguments = arguments

        def execute(self, **_kwargs: Any) -> Any:
            response = request(
                "/v1/chat/api",
                {
                    "resource": self.resource,
                    "method": self.method,
                    "arguments": self.arguments,
                },
            )
            return response.get("response")

    class RemoteResource:
        """googleapiclient discovery-resource-shaped remote facade."""

        def __init__(self, resource: list[str] | None = None) -> None:
            self.resource = resource or []

        def __getattr__(self, name: str) -> Any:
            if name.startswith("_"):
                raise AttributeError(name)

            def invoke(**arguments: Any) -> Any:
                if arguments:
                    return RemoteRequest(self.resource, name, arguments)
                return RemoteResource([*self.resource, name])

            return invoke

    async def relay_loop(self: Any) -> None:
        while not self._shutting_down:
            message: RelayMessage | None = None
            try:
                response = await asyncio.to_thread(request, "/v1/chat/events")
                event = response.get("event")
                if not event:
                    continue
                message = RelayMessage(event)
                await asyncio.to_thread(self._on_pubsub_message, message)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("Google Chat relay receive failed", exc_info=True)
                if message is not None:
                    try:
                        await asyncio.to_thread(message.nack)
                    except Exception:
                        pass
                await asyncio.sleep(2)

    def patch_adapter_class(adapter_class: type[Any]) -> None:
        if getattr(adapter_class, "_credential_proxy_relay_patched", False):
            return
        async def connect(self: Any, *, is_reconnect: bool = False) -> bool:
            self._loop = asyncio.get_running_loop()
            self._shutting_down = False
            self._chat_api = RemoteResource()
            try:
                await asyncio.to_thread(self._thread_count_store.load)
            except Exception:
                LOGGER.warning("Google Chat thread state load failed", exc_info=True)
            self._bot_user_id = self._load_cached_bot_id()
            self._relay_task = asyncio.create_task(relay_loop(self))
            self._mark_connected()
            LOGGER.info("Google Chat connected through credential proxy relay")
            return True

        async def disconnect(self: Any) -> None:
            self._shutting_down = True
            task = getattr(self, "_relay_task", None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._chat_api = None
            self._mark_disconnected()

        def new_authed_http(self: Any) -> Any:
            return None

        async def setup_files(
            self: Any,
            chat_id: str,
            thread_id: str | None,
            raw_text: str,
            sender_email: str | None = None,
        ) -> bool:
            await self.send(
                chat_id,
                "File attachment setup is unavailable through the credential proxy.",
                metadata={"thread_id": thread_id} if thread_id else None,
            )
            return True

        adapter_class.connect = connect
        adapter_class.disconnect = disconnect
        adapter_class._new_authed_http = new_authed_http
        adapter_class._handle_setup_files_command = setup_files
        adapter_class._credential_proxy_relay_patched = True

    from gateway.platform_registry import PlatformRegistry

    original_registry_create = PlatformRegistry.create_adapter
    if not getattr(PlatformRegistry, "_credential_proxy_relay_patched", False):

        def create_adapter(self: Any, name: str, config: Any) -> Any:
            adapter = original_registry_create(self, name, config)
            if name == "google_chat" and adapter is not None:
                patch_adapter_class(type(adapter))
            return adapter

        PlatformRegistry.create_adapter = create_adapter
        PlatformRegistry._credential_proxy_relay_patched = True
