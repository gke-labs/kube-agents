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


def relay_without_token() -> bool:
    """Is Slack reachable from this process only through the relay?

    The credential proxy holds the deployment's only bot token, so a process
    that has a relay URL and no token of its own is not "Slack is not
    configured" — it is "Slack is configured somewhere else". Hermes' plugin
    probe has no way to know that and answers the narrower question by looking
    for a token; this answers the one it meant to ask.
    """
    if not os.getenv("SLACK_RELAY_URL", "").strip():
        return False
    return not os.getenv("SLACK_BOT_TOKEN", "").strip()


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

    def sender_module_of(sender: Any) -> Any:
        """The adapter module a standalone sender came from.

        Resolved from the function object rather than by name. The plugin is
        importable under two module paths that are not the same object, so
        hardcoding either one finds a module whose ``SlackAdapter`` is not the
        class this sender was defined beside — and silently formats nothing.
        """
        return sys.modules.get(getattr(sender, "__module__", "") or "")

    def local_slack_token(module: Any, pconfig: Any) -> bool:
        """Mirror ``_standalone_send``'s own token lookup, in its own order."""
        if getattr(pconfig, "token", None):
            return True
        get_secret = getattr(module, "get_secret", None)
        if get_secret is None:
            return False
        try:
            return bool(get_secret("SLACK_BOT_TOKEN", ""))
        except Exception:
            LOGGER.debug("Slack secret lookup failed", exc_info=True)
            return False

    def format_mrkdwn(module: Any, text: Any) -> Any:
        """Apply the adapter's own mrkdwn conversion, the way it does itself."""
        adapter_class = getattr(module, "SlackAdapter", None)
        if not text or adapter_class is None:
            return text
        try:
            formatter = adapter_class.__new__(adapter_class)
            return formatter.format_message(text)
        except Exception:
            LOGGER.debug("Slack mrkdwn formatting failed", exc_info=True)
            return text

    async def relay_api_call(
        api_method: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        # ``request`` is a blocking urlopen, and cron delivery runs under
        # asyncio.run inside a thread pool; calling it directly stalls the loop.
        response = await asyncio.to_thread(
            request,
            "/v1/chat/slack/api",
            # An empty team falls through to the proxy's primary client. This
            # deployment installs one workspace; a second would need the team
            # resolved per call, as the inbound-event path already does.
            {"teamId": "", "method": api_method, "arguments": arguments},
        )
        payload = response.get("response")
        return payload if isinstance(payload, dict) else {}

    def build_standalone_sender(original_sender: Any) -> Any:
        module = sender_module_of(original_sender)

        async def relay_standalone_send(
            pconfig: Any,
            chat_id: Any,
            message: Any,
            *,
            thread_id: Any = None,
            media_files: Any = None,
            force_document: bool = False,
            caption: Any = None,
        ) -> dict[str, Any]:
            """Deliver out-of-process Slack messages with no local bot token.

            Hermes' standalone sender resolves a token and gives up without
            one. Nothing outside the credential proxy ever holds that token
            here, so every cron brief ended at "SLACK_BOT_TOKEN not
            configured" — the second wall behind the delivery target, and the
            reason briefs still did not arrive after ``/sethome`` fixed the
            first. Relay the two Web API calls the text path actually needs.

            Deliberately does not chunk. ``message`` arrives already split:
            ``tools/send_message_tool.py`` smart-chunks against the registry
            entry's ``max_message_length`` and then calls
            ``standalone_sender_fn`` -- this function -- once per chunk. The
            Slack entry declares 39000, under Slack's 40000-character ``text``
            limit, so a chunk is bounded before it gets here. Adding a second
            chunker would re-split an already-split message and interleave the
            pieces across the thread. If this ever grows a caller that is not
            ``send_message_tool``, that caller owns the splitting.
            """
            if original_sender is not None and local_slack_token(module, pconfig):
                # A deployment that does hold a token keeps the stock path,
                # byte for byte.
                return await original_sender(
                    pconfig,
                    chat_id,
                    message,
                    thread_id=thread_id,
                    media_files=media_files,
                    force_document=force_document,
                    caption=caption,
                )

            del force_document  # signature parity, exactly as upstream

            if media_files:
                # ``files_upload_v2`` is a slack_sdk helper rather than a Web
                # API method, and its second leg POSTs straight to
                # files.slack.com, so ``api_call`` behind the relay cannot
                # carry it. Say so: an attachment that disappears silently is
                # worse than one that reports why it did not arrive.
                return {
                    "error": (
                        "Slack media delivery needs a local bot token: "
                        "files_upload_v2 is an SDK helper, not a Web API "
                        "method, so the credential relay cannot carry it"
                    )
                }

            # Silent cron runs deliver empty text every minute. Upstream calls
            # that a success; so must this, or the relay starts reporting a
            # failure a minute for jobs that are working as intended. Tested
            # before formatting as well as after: a formatter that decorates
            # its input turns whitespace into a message worth sending, and
            # upstream's own guard — which only sees the formatted text —
            # would let that through.
            blank = {"success": True, "platform": "slack", "skipped": "empty_text"}
            if not message or not str(message).strip():
                return blank
            formatted = format_mrkdwn(module, message)
            if not formatted or not str(formatted).strip():
                return blank

            target = str(chat_id or "")
            try:
                if target[:1] in ("U", "W"):
                    # chat.postMessage rejects a bare user id; open the DM.
                    opened = await relay_api_call(
                        "conversations.open", {"json": {"users": target}}
                    )
                    channel = opened.get("channel")
                    resolved = (
                        channel.get("id") if isinstance(channel, dict) else None
                    )
                    if not resolved:
                        return {
                            "error": (
                                "Slack user ID resolution failed for "
                                f"{target} (conversations.open — check the "
                                "bot's im:write scope)"
                            )
                        }
                    target = str(resolved)

                body: dict[str, Any] = {
                    "channel": target,
                    "text": formatted,
                    "mrkdwn": True,
                }
                if thread_id:
                    body["thread_ts"] = thread_id
                data = await relay_api_call("chat.postMessage", {"json": body})
            except urllib.error.HTTPError as exc:
                # The proxy-side client validates, so a Slack rejection
                # arrives as a relay 502 carrying the cause rather than as an
                # ok:false payload. Keep the two distinguishable.
                fields = relayed_slack_error(exc)
                if fields is None:
                    return {"error": f"Slack send failed: relay error {exc.code}"}
                return {"error": f"Slack API error: {fields.get('error', 'unknown')}"}
            except Exception as exc:
                return {"error": f"Slack send failed: {exc}"}

            if not data.get("ok"):
                return {"error": f"Slack API error: {data.get('error', 'unknown')}"}
            return {
                "success": True,
                "platform": "slack",
                "chat_id": target,
                "message_id": data.get("ts"),
            }

        relay_standalone_send._credential_proxy_relay_patched = True
        return relay_standalone_send

    def patch_slack_entry(entry: Any) -> None:
        """Give a registered Slack entry a relay-backed sender and status."""
        original_sender = getattr(entry, "standalone_sender_fn", None)
        if not getattr(original_sender, "_credential_proxy_relay_patched", False):
            entry.standalone_sender_fn = build_standalone_sender(original_sender)

        original_is_connected = getattr(entry, "is_connected", None)
        if not getattr(original_is_connected, "_credential_proxy_relay_patched", False):

            def is_connected(config: Any) -> bool:
                # Hermes consults this only for a platform it has not already
                # been told about, so in the gateway — where the root
                # config.yaml enables Slack outright — it never runs. It runs
                # in the named profiles a cron tick uses, which is precisely
                # where the answer was wrong.
                if relay_without_token():
                    return True
                if original_is_connected is None:
                    return False
                return bool(original_is_connected(config))

            is_connected._credential_proxy_relay_patched = True
            entry.is_connected = is_connected

    original_registry_create = PlatformRegistry.create_adapter
    if not getattr(PlatformRegistry, "_slack_credential_proxy_relay_patched", False):

        def create_adapter(self: Any, name: str, config: Any) -> Any:
            adapter = original_registry_create(self, name, config)
            if name == "slack" and adapter is not None:
                patch_adapter_class(type(adapter))
            return adapter

        PlatformRegistry.create_adapter = create_adapter
        PlatformRegistry._slack_credential_proxy_relay_patched = True

    # ``create_adapter`` above only ever fires where a live adapter is built.
    # A cron tick has no adapter and no event loop, so the standalone sender
    # and the enablement probe are the only Slack surfaces it touches, and
    # both hang off the registry entry rather than off an adapter instance.
    original_registry_register = getattr(PlatformRegistry, "register", None)
    if original_registry_register is not None and not getattr(
        PlatformRegistry, "_slack_standalone_relay_patched", False
    ):

        def register(self: Any, entry: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(entry, "name", None) == "slack":
                try:
                    patch_slack_entry(entry)
                except Exception:
                    # sitecustomize latches its trigger before calling
                    # install(), and the gateway folds an exception from that
                    # import into a debug line. Raising here would disable the
                    # relay for the life of the process, and say nothing.
                    LOGGER.warning("Slack relay entry patch failed", exc_info=True)
            # Forward everything after ``entry`` blind rather than restating
            # today's signature. This wrapper sits on the class, so every
            # platform registers through it and one TypeError here takes Slack,
            # Google Chat and A2A down together -- the gateway comes up with the
            # built-in adapters missing and the pod answers no chat at all.
            # v2026.8.13 added a keyword-only ``scope`` and did exactly that
            # (#718 bumped the base image without touching this line); spelling
            # the arguments out again would re-arm the same failure on the next
            # bump.
            #
            # One residual difference from calling the original directly, and it
            # predates this wrapper's argument handling: when ``scope`` is
            # omitted the registry infers it from the caller's frame at a fixed
            # depth, and this frame occupies the slot it reads. The inferred
            # scope is lost rather than wrong -- register() falls back to the
            # entry's adapter_factory and then its check_fn, both
            # frame-independent -- so an entry can land in the global scope
            # instead of a plugin one. Nothing in the runtime relies on that
            # inference: the plugin loader passes ``scope`` explicitly, and the
            # built-in relay path sets source="builtin", which skips it.
            return original_registry_register(self, entry, *args, **kwargs)

        PlatformRegistry.register = register
        PlatformRegistry._slack_standalone_relay_patched = True

        # Registration order is not a contract. If the plugin got there first
        # the entry is already in place, and no future register() call will
        # reach it. Read the live entries directly rather than through get(),
        # which resolves deferred loaders and would pay the plugin import cost
        # this patch is deliberately deferring.
        try:
            registry_module = sys.modules.get("gateway.platform_registry")
            singleton = getattr(registry_module, "platform_registry", None)
            entries = getattr(singleton, "_entries", None)
            if isinstance(entries, dict):
                if "slack" in entries:
                    patch_slack_entry(entries["slack"])
            elif singleton is not None:
                # The registry is up but its entries are not where this reads
                # them. Normally that means nothing -- the register() wrapper
                # above is the path that actually fires, and this fallback is
                # for the ordering where the plugin got in first. But `_entries`
                # is a private name: if upstream renames it, the two cases stop
                # being distinguishable from here, and the one that matters
                # fails by silently not relaying. Warn rather than debug, so a
                # base-image bump that moves it leaves a trace.
                LOGGER.warning(
                    "Slack relay: platform registry is loaded but its entries "
                    "are not introspectable (_entries is %s); a Slack entry "
                    "registered before this patch installed will not be "
                    "patched",
                    type(entries).__name__,
                )
        except Exception:
            LOGGER.debug("No pre-registered Slack entry to patch", exc_info=True)
