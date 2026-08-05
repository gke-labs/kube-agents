"""Credential-free Slack SDK transport for Hermes' bundled adapter."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("slack-relay-patch")
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
# The agent container starts before the credential-proxy sidecar, and the
# sidecar has been observed taking the better part of a minute to come up.
# Waiting that out has to be generous: the real bot token lives in the relay,
# so a connect that gives up early leaves the gateway with no bot credential
# on the queued config, which drops Slack from the retry queue for the life
# of the pod.
DEFAULT_RELAY_READY_TIMEOUT = 120.0


def relayed_slack_error(exc: urllib.error.HTTPError) -> dict[str, Any] | None:
    """Return the Slack error payload a relay failure carried, if it carried one.

    The credential proxy answers ``502`` for anything that went wrong behind
    it, and attaches a ``slack`` object only when the cause was Slack itself
    rejecting the call. ``None`` therefore means the relay broke rather than
    the API call, and the caller re-raises unchanged: a transport failure must
    stay distinguishable from ``channel_not_found``.
    """
    try:
        raw = exc.read()
    except Exception:
        return None
    # HTTPError is a one-shot file object, and this helper is called on the
    # path that may still re-raise it. Put the bytes back so whatever handles
    # a genuine transport failure upstream is not handed an empty body. Both
    # attributes have to move: the tempfile wrapper HTTPError inherits from
    # caches the bound ``read`` on the instance the first time it is used, so
    # replacing ``fp`` alone leaves the old one still wired up.
    exc.fp = io.BytesIO(raw)
    exc.read = exc.fp.read  # type: ignore[method-assign]
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    fields = body.get("slack")
    if not isinstance(fields, dict) or not fields:
        return None
    # ``ok`` is whitelisted through the proxy, but a payload that omitted it
    # still describes a failure — this path is only reached for one.
    return {"ok": False, **fields}


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

    from gateway.platform_registry import PlatformRegistry
    from gateway.platforms.base import cache_audio_from_bytes, cache_image_from_bytes
    import slack_bolt.app.async_app as bolt_async_app
    import slack_bolt.context.async_context as bolt_async_context
    from slack_bolt.adapter.socket_mode.async_internals import run_async_bolt_app
    from slack_sdk.errors import SlackApiError
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

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

            def __init__(
                self, token: str | None = None, team_id: str = "", **_kwargs: Any
            ) -> None:
                placeholder = token or "relay:"
                super().__init__(token=placeholder)
                # Prefer the team Bolt resolved from the inbound event. Across
                # several workspaces the token is a comma-joined list, so
                # splitting it yields every team at once rather than the one
                # this request belongs to.
                self.team_id = team_id or (
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
                supplied = {
                    key: value
                    for key, value in arguments.items()
                    if value is not None
                }
                try:
                    response = await asyncio.to_thread(
                        request,
                        "/v1/chat/slack/api",
                        {
                            "teamId": self.team_id,
                            "method": api_method,
                            "arguments": supplied,
                        },
                    )
                except urllib.error.HTTPError as exc:
                    # A Slack rejection reaches us as a relay 502, because the
                    # proxy-side client validated the response and raised. Put
                    # it back into the shape callers written against the real
                    # client expect: SlackApiError carrying a response whose
                    # ``error`` names the cause. Anything else is a genuine
                    # transport failure and propagates untouched.
                    fields = relayed_slack_error(exc)
                    if fields is None:
                        raise
                    raise SlackApiError(
                        # Word for word what slack_sdk's own BaseClient raises,
                        # so a log line from behind the relay is not a
                        # different log line.
                        message=(
                            "The request to the Slack API failed. "
                            f"(url: {api_method}, status: 200)"
                        ),
                        # That status is the Slack call's, not the relay's: the
                        # API answered 200 with ok:false, and validate() keys
                        # on that pair.
                        response=AsyncSlackResponse(
                            client=self,
                            http_verb=http_verb,
                            api_url=api_method,
                            req_args=supplied,
                            data=fields,
                            headers={},
                            status_code=200,
                        ),
                    ) from exc
                # Hand back the SDK's own response type rather than the bare
                # payload. Everything downstream is written against the real
                # client: Bolt's authorization middleware reads .headers off
                # this to pick up x-oauth-scopes, and a plain dict makes it
                # die with "'dict' object has no attribute 'headers'" before
                # any listener runs. The relay forwards the scope headers it
                # captured under "__headers".
                payload = response.get("response") or {}
                headers = {}
                if isinstance(payload, dict):
                    data = dict(payload)
                    if "__headers" in data and isinstance(data["__headers"], dict):
                        headers.update(data.pop("__headers"))
                    elif "headers" in data and isinstance(data["headers"], dict):
                        headers.update(data.get("headers") or {})
                else:
                    data = {}
                return AsyncSlackResponse(
                    client=self,
                    http_verb=http_verb,
                    api_url=api_method,
                    req_args=supplied,
                    data=data,
                    headers=headers,
                    status_code=200,
                )

        def remote_app_factory(
            *_args: Any, token: str | None = None, **kwargs: Any
        ) -> Any:
            kwargs.pop("client", None)
            kwargs["request_verification_enabled"] = False
            return real_async_app(
                client=RemoteSlackClient(token=token),
                **kwargs,
            )

        module.AsyncWebClient = RemoteSlackClient
        module.AsyncApp = remote_app_factory

        # slack_bolt >= 1.15 ignores the client passed to AsyncApp(...) when
        # dispatching events: AsyncApp._init_context builds a new plain
        # AsyncWebClient per request, and the AsyncSingleTeamAuthorization
        # middleware then calls auth.test directly against slack.com with the
        # "relay:<teamId>" placeholder token, rejecting every inbound event
        # with invalid_auth. Rebind the name those modules construct so
        # per-request clients are relay-backed too. RemoteSlackClient
        # subclasses the real AsyncWebClient, so bolt's isinstance() check on
        # AsyncApp(client=...) still passes.
        bolt_async_app.AsyncWebClient = RemoteSlackClient
        bolt_async_context.AsyncWebClient = RemoteSlackClient

        async def bootstrap_workspaces() -> list[dict[str, Any]]:
            # The credential proxy sidecar can come up tens of seconds after
            # the gateway starts connecting platforms; a connection error or
            # 503 ("Slack relay disabled") here usually means the relay is
            # not ready yet, not that Slack is unconfigured. Retry within a
            # bounded window instead of failing the whole connect on the
            # startup race.
            try:
                wait_seconds = float(
                    os.getenv(
                        "SLACK_RELAY_BOOTSTRAP_WAIT_SECONDS",
                        str(DEFAULT_RELAY_READY_TIMEOUT),
                    )
                )
            except ValueError:
                LOGGER.warning(
                    "Invalid SLACK_RELAY_BOOTSTRAP_WAIT_SECONDS; using the default"
                )
                wait_seconds = DEFAULT_RELAY_READY_TIMEOUT
            deadline = time.monotonic() + wait_seconds
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Slack relay bootstrap timed out")
                try:
                    bootstrap = await asyncio.to_thread(
                        request, "/v1/chat/slack/bootstrap", {}
                    )
                    return bootstrap.get("workspaces") or []
                except urllib.error.HTTPError as exc:
                    if exc.code != 503:
                        raise
                except (urllib.error.URLError, OSError):
                    pass
                LOGGER.info("Slack relay is not ready yet; retrying bootstrap")
                await asyncio.sleep(2)

        async def connect(self: Any, *, is_reconnect: bool = False) -> bool:
            try:
                try:
                    workspaces = await bootstrap_workspaces()
                except (urllib.error.URLError, OSError) as exc:
                    LOGGER.error(
                        "Slack credential proxy bootstrap failed type=%s",
                        type(exc).__name__,
                    )
                    return False
                if not workspaces:
                    LOGGER.error(
                        "Slack credential proxy has no authenticated workspace"
                    )
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
            finally:
                # The gateway never holds a real bot token in this deployment,
                # and Hermes permanently drops platforms whose queued config
                # has no bot credential from its reconnect queue. Keep a
                # placeholder on every exit path (including cancellation by
                # the gateway's connect timeout) so a failed connect stays
                # eligible for reconnect retries.
                if not getattr(self.config, "token", None):
                    self.config.token = "relay:"

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
                return cache_audio_from_bytes(content, ext)
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

    original_registry_create = PlatformRegistry.create_adapter
    if not getattr(PlatformRegistry, "_slack_credential_proxy_relay_patched", False):

        def create_adapter(self: Any, name: str, config: Any) -> Any:
            adapter = original_registry_create(self, name, config)
            if name == "slack" and adapter is not None:
                patch_adapter_class(type(adapter))
            return adapter

        PlatformRegistry.create_adapter = create_adapter
        PlatformRegistry._slack_credential_proxy_relay_patched = True
