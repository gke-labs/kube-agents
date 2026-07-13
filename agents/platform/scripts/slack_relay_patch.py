"""Credential-free relay mode for Hermes' bundled Slack adapter."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any


LOGGER = logging.getLogger("slack-relay-patch")


def install() -> None:
    relay_url = os.getenv("SLACK_RELAY_URL", "").rstrip("/")
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

    def json_value(value: Any) -> Any:
        if isinstance(value, Path):
            value = str(value)
        if isinstance(value, bytes):
            return {"__bytesBase64": base64.b64encode(value).decode("ascii")}
        if isinstance(value, str) and os.path.isfile(value):
            return {
                "__fileBase64": base64.b64encode(Path(value).read_bytes()).decode(
                    "ascii"
                ),
                "filename": Path(value).name,
            }
        if isinstance(value, dict):
            return {key: json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_value(item) for item in value]
        return value

    class RemoteSlackClient:
        """Async Slack SDK-shaped client whose calls execute in the proxy."""

        def __init__(self, team_id: str = "") -> None:
            self.team_id = team_id

        def __getattr__(self, method: str) -> Any:
            async def call(**kwargs: Any) -> dict[str, Any]:
                response = await asyncio.to_thread(
                    request,
                    "/v1/chat/slack/api",
                    {
                        "teamId": self.team_id,
                        "method": method,
                        "arguments": json_value(kwargs),
                    },
                )
                return response.get("response") or {}

            return call

    async def relay_loop(self: Any) -> None:
        while not self._shutting_down:
            receipt = ""
            try:
                response = await asyncio.to_thread(request, "/v1/chat/slack/events")
                envelope = response.get("event")
                if not envelope:
                    continue
                receipt = str(envelope["receipt"])
                payload = envelope.get("payload") or {}
                event_type = envelope.get("type", "")
                if event_type == "events_api":
                    event = payload.get("event") or {}
                    kind = event.get("type", "")
                    if kind in {"message", "app_mention"}:
                        await self._handle_slack_message(event)
                    elif kind == "file_shared":
                        await self._handle_slack_file_shared(event)
                    elif kind in {
                        "assistant_thread_started",
                        "assistant_thread_context_changed",
                    }:
                        await self._handle_assistant_thread_lifecycle_event(event)
                elif event_type == "slash_commands":
                    await self._handle_slash_command(payload)
                elif event_type == "interactive":
                    actions = payload.get("actions") or []
                    action = actions[0] if actions else {}
                    action_id = action.get("action_id", "")

                    async def ack(**_kwargs: Any) -> None:
                        return None

                    if (
                        action_id.startswith("hermes_approve_")
                        or action_id == "hermes_deny"
                    ):
                        await self._handle_approval_action(ack, payload, action)
                    elif action_id.startswith("hermes_confirm_"):
                        await self._handle_slash_confirm_action(ack, payload, action)
                await asyncio.to_thread(
                    request, "/v1/chat/slack/events/ack", {"receipt": receipt}
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("Slack relay receive failed", exc_info=True)
                if receipt:
                    try:
                        await asyncio.to_thread(
                            request,
                            "/v1/chat/slack/events/nack",
                            {"receipt": receipt},
                        )
                    except Exception:
                        pass
                await asyncio.sleep(2)

    def patch_adapter_class(adapter_class: type[Any]) -> None:
        if getattr(adapter_class, "_credential_proxy_relay_patched", False):
            return
        original_connect = adapter_class.connect
        original_disconnect = adapter_class.disconnect
        original_download = adapter_class._download_slack_file
        original_download_bytes = adapter_class._download_slack_file_bytes

        async def connect(self: Any, *, is_reconnect: bool = False) -> bool:
            if not os.getenv("SLACK_RELAY_URL"):
                return await original_connect(self, is_reconnect=is_reconnect)
            bootstrap = await asyncio.to_thread(request, "/v1/chat/slack/bootstrap", {})
            workspaces = bootstrap.get("workspaces") or []
            if not workspaces:
                LOGGER.error("Slack credential proxy has no authenticated workspace")
                return False
            self._bot_user_id = None
            self._team_clients = {}
            self._team_bot_user_ids = {}
            for workspace in workspaces:
                team_id = str(workspace.get("teamId", ""))
                bot_user_id = str(workspace.get("botUserId", ""))
                self._team_clients[team_id] = RemoteSlackClient(team_id)
                self._team_bot_user_ids[team_id] = bot_user_id
                if self._bot_user_id is None:
                    self._bot_user_id = bot_user_id
            self._app = SimpleNamespace(client=RemoteSlackClient())
            self._app_token = None
            self._handler = None
            self._shutting_down = False
            self._running = True
            self._relay_task = asyncio.create_task(relay_loop(self))
            self._mark_connected()
            LOGGER.info("Slack connected through credential proxy relay")
            return True

        async def disconnect(self: Any) -> None:
            if not os.getenv("SLACK_RELAY_URL"):
                await original_disconnect(self)
                return
            self._shutting_down = True
            self._running = False
            task = getattr(self, "_relay_task", None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._app = None
            self._team_clients = {}
            self._team_bot_user_ids = {}
            self._mark_disconnected()

        async def download(
            self: Any, url: str, ext: str, audio: bool = False, team_id: str = ""
        ) -> str:
            if not os.getenv("SLACK_RELAY_URL"):
                return await original_download(self, url, ext, audio, team_id)
            response = await asyncio.to_thread(
                request,
                "/v1/chat/slack/files/download",
                {"url": url, "teamId": team_id},
            )
            content = base64.b64decode(response["data"])
            if audio:
                from gateway.platforms.base import cache_audio_from_bytes

                return cache_audio_from_bytes(content, ext)
            from gateway.platforms.base import cache_image_from_bytes

            return cache_image_from_bytes(content, ext)

        async def download_bytes(self: Any, url: str, team_id: str = "") -> bytes:
            if not os.getenv("SLACK_RELAY_URL"):
                return await original_download_bytes(self, url, team_id)
            response = await asyncio.to_thread(
                request,
                "/v1/chat/slack/files/download",
                {"url": url, "teamId": team_id},
            )
            return base64.b64decode(response["data"])

        adapter_class.connect = connect
        adapter_class.disconnect = disconnect
        adapter_class._download_slack_file = download
        adapter_class._download_slack_file_bytes = download_bytes
        adapter_class._credential_proxy_relay_patched = True

    from plugins.platforms.slack.adapter import SlackAdapter

    patch_adapter_class(SlackAdapter)

    from gateway.platform_registry import PlatformRegistry

    original_registry_create = PlatformRegistry.create_adapter
    if not getattr(PlatformRegistry, "_slack_credential_proxy_relay_patched", False):

        def create_adapter(self: Any, name: str, config: Any) -> Any:
            adapter = original_registry_create(self, name, config)
            if name == "slack" and adapter is not None:
                patch_adapter_class(type(adapter))
            return adapter

        PlatformRegistry.create_adapter = create_adapter
        PlatformRegistry._slack_credential_proxy_relay_patched = True
