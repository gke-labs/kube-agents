"""Credential-free Slack SDK transport for Hermes' bundled adapter."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("slack-relay-patch")
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024


def read_upload(path: Path, max_file_bytes: int) -> bytes:
    """Read an upload without allowing it to grow past the relay limit."""
    if path.stat().st_size > max_file_bytes:
        raise ValueError("Slack upload exceeds relay size limit")
    with path.open("rb") as upload:
        content = upload.read(max_file_bytes + 1)
    if len(content) > max_file_bytes:
        raise ValueError("Slack upload exceeds relay size limit")
    return content


def install() -> None:
    relay_url = os.getenv("SLACK_RELAY_URL", "").rstrip("/")
    if not relay_url:
        return
    try:
        max_file_bytes = int(
            os.getenv("SLACK_RELAY_MAX_FILE_BYTES", str(DEFAULT_MAX_FILE_BYTES))
        )
    except ValueError:
        LOGGER.warning("Invalid Slack relay file limit; using the default")
        max_file_bytes = DEFAULT_MAX_FILE_BYTES

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

    def json_value(value: Any, *, file_value: bool = False) -> Any:
        if isinstance(value, bytes):
            if len(value) > max_file_bytes:
                raise ValueError("Slack upload exceeds relay size limit")
            return {"__bytesBase64": base64.b64encode(value).decode("ascii")}
        if hasattr(value, "read"):
            content = value.read(max_file_bytes + 1)
            if isinstance(content, str):
                content = content.encode("utf-8")
            if len(content) > max_file_bytes:
                raise ValueError("Slack upload exceeds relay size limit")
            return {
                "__fileBase64": base64.b64encode(content).decode("ascii"),
                "filename": Path(getattr(value, "name", "upload")).name,
            }
        if file_value and isinstance(value, (str, Path)):
            path = Path(value)
            return {
                "__fileBase64": base64.b64encode(
                    read_upload(path, max_file_bytes)
                ).decode("ascii"),
                "filename": path.name,
            }
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {
                key: json_value(item, file_value=file_value)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [json_value(item, file_value=file_value) for item in value]
        return value

    async def relay_loop(self: Any) -> None:
        from slack_bolt.adapter.socket_mode.async_internals import run_async_bolt_app
        from slack_sdk.socket_mode.request import SocketModeRequest

        while self._running:
            receipt = ""
            try:
                response = await asyncio.to_thread(request, "/v1/chat/slack/events")
                event = response.get("event")
                if not event:
                    continue
                receipt = str(event["receipt"])
                socket_request = SocketModeRequest(
                    type=str(event.get("type", "")),
                    envelope_id=receipt,
                    payload=event.get("payload") or {},
                )
                await run_async_bolt_app(self._app, socket_request)
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

        module = sys.modules[adapter_class.__module__]
        real_async_app = module.AsyncApp
        real_async_client = module.AsyncWebClient
        original_connect = adapter_class.connect
        original_disconnect = adapter_class.disconnect

        class RemoteSlackClient(real_async_client):
            """Slack SDK client whose generic API calls execute in the proxy."""

            def __init__(self, token: str | None = None, **_kwargs: Any) -> None:
                placeholder = token or "relay:"
                super().__init__(token=placeholder)
                self.team_id = (
                    placeholder.split(":", 1)[1]
                    if placeholder.startswith("relay:")
                    else ""
                )

            async def api_call(
                self,
                api_method: str,
                *,
                http_verb: str = "POST",
                files: dict[str, Any] | None = None,
                data: Any = None,
                params: dict[str, Any] | None = None,
                json: dict[str, Any] | None = None,
                headers: dict[str, Any] | None = None,
                auth: dict[str, Any] | None = None,
            ) -> Any:
                arguments = {
                    "http_verb": http_verb,
                    "files": json_value(files, file_value=True) if files else None,
                    "data": json_value(data) if data is not None else None,
                    "params": json_value(params) if params else None,
                    "json": json_value(json) if json else None,
                    "headers": json_value(headers) if headers else None,
                    "auth": json_value(auth) if auth else None,
                }
                response = await asyncio.to_thread(
                    request,
                    "/v1/chat/slack/api",
                    {
                        "teamId": self.team_id,
                        "method": api_method,
                        "arguments": {
                            key: value
                            for key, value in arguments.items()
                            if value is not None
                        },
                    },
                )
                return response.get("response") or {}

        def remote_client_factory(
            token: str | None = None, **kwargs: Any
        ) -> RemoteSlackClient:
            return RemoteSlackClient(token=token, **kwargs)

        def remote_app_factory(
            *_args: Any, token: str | None = None, **kwargs: Any
        ) -> Any:
            kwargs.pop("client", None)
            kwargs["request_verification_enabled"] = False
            return real_async_app(
                client=RemoteSlackClient(token=token),
                **kwargs,
            )

        module.AsyncWebClient = remote_client_factory
        module.AsyncApp = remote_app_factory

        async def connect(self: Any, *, is_reconnect: bool = False) -> bool:
            bootstrap = await asyncio.to_thread(
                request, "/v1/chat/slack/bootstrap", {}
            )
            workspaces = bootstrap.get("workspaces") or []
            if not workspaces:
                LOGGER.error("Slack credential proxy has no authenticated workspace")
                return False
            first_connect = not hasattr(
                self, "_credential_proxy_original_slack_token"
            )
            if first_connect:
                self._credential_proxy_original_slack_token = self.config.token
                self._credential_proxy_original_slack_app_token = os.environ.get(
                    "SLACK_APP_TOKEN"
                )
            self.config.token = ",".join(
                "relay:" + str(workspace.get("teamId", ""))
                for workspace in workspaces
            )
            os.environ["SLACK_APP_TOKEN"] = "relay"
            self._shutting_down = False
            try:
                connected = await original_connect(self, is_reconnect=is_reconnect)
            except Exception:
                if first_connect:
                    restore_slack_placeholders(self)
                raise
            if not connected and first_connect:
                restore_slack_placeholders(self)
            return connected

        async def disconnect(self: Any) -> None:
            self._shutting_down = True
            try:
                await original_disconnect(self)
            finally:
                restore_slack_placeholders(self)

        def restore_slack_placeholders(self: Any) -> None:
            if not hasattr(self, "_credential_proxy_original_slack_token"):
                return
            self.config.token = self._credential_proxy_original_slack_token
            original_app_token = self._credential_proxy_original_slack_app_token
            if original_app_token is None:
                os.environ.pop("SLACK_APP_TOKEN", None)
            else:
                os.environ["SLACK_APP_TOKEN"] = original_app_token
            del self._credential_proxy_original_slack_token
            del self._credential_proxy_original_slack_app_token

        def start_transport(self: Any) -> None:
            task = asyncio.create_task(relay_loop(self))
            self._socket_mode_task = task
            self._relay_task = task

        async def stop_transport(self: Any) -> None:
            task = getattr(self, "_relay_task", None)
            self._relay_task = None
            self._socket_mode_task = None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        def no_watchdog(self: Any) -> None:
            return None

        async def download(
            self: Any, url: str, ext: str, audio: bool = False, team_id: str = ""
        ) -> str:
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
            response = await asyncio.to_thread(
                request,
                "/v1/chat/slack/files/download",
                {"url": url, "teamId": team_id},
            )
            return base64.b64decode(response["data"])

        adapter_class.connect = connect
        adapter_class.disconnect = disconnect
        adapter_class._start_socket_mode_handler = start_transport
        adapter_class._stop_socket_mode_handler = stop_transport
        adapter_class._ensure_socket_watchdog = no_watchdog
        adapter_class._download_slack_file = download
        adapter_class._download_slack_file_bytes = download_bytes
        adapter_class._credential_proxy_relay_patched = True

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
