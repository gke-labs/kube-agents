#!/usr/bin/env python3
"""Credential proxy for exact raw shell command execution."""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("credential-proxy")


class GoogleChatRelay:
    """Credentialed Google Chat/Pub/Sub transport for a credential-free agent."""

    SCOPES = (
        "https://www.googleapis.com/auth/chat.bot",
        "https://www.googleapis.com/auth/pubsub",
    )

    def __init__(self, project_id: str, subscription_name: str) -> None:
        import google.auth
        from google.cloud import pubsub_v1
        from googleapiclient.discovery import build

        credentials, _ = google.auth.default(scopes=self.SCOPES)
        self.subscriber = pubsub_v1.SubscriberClient(credentials=credentials)
        self.subscription_path = (
            subscription_name
            if subscription_name.startswith("projects/")
            else self.subscriber.subscription_path(project_id, subscription_name)
        )
        self.chat = build("chat", "v1", credentials=credentials, cache_discovery=False)
        self._receipts: dict[str, Any] = {}
        self._lock = threading.Lock()

    def pull(self, timeout_seconds: int = 20) -> dict[str, Any] | None:
        from google.api_core import retry
        from google.api_core.exceptions import DeadlineExceeded

        try:
            response = self.subscriber.pull(
                request={"subscription": self.subscription_path, "max_messages": 1},
                retry=retry.Retry(deadline=max(timeout_seconds, 1)),
                timeout=max(timeout_seconds, 1),
            )
        except DeadlineExceeded:
            return None
        if not response.received_messages:
            return None
        received = response.received_messages[0]
        receipt = str(uuid.uuid4())
        with self._lock:
            self._receipts[receipt] = received.ack_id
        return {
            "receipt": receipt,
            "data": base64.b64encode(received.message.data).decode("ascii"),
            "attributes": dict(received.message.attributes),
            "messageId": received.message.message_id,
        }

    def settle(self, receipt: str, acknowledge: bool) -> bool:
        with self._lock:
            ack_id = self._receipts.pop(receipt, None)
        if ack_id is None:
            return False
        if acknowledge:
            self.subscriber.acknowledge(
                request={"subscription": self.subscription_path, "ack_ids": [ack_id]}
            )
        else:
            self.subscriber.modify_ack_deadline(
                request={
                    "subscription": self.subscription_path,
                    "ack_ids": [ack_id],
                    "ack_deadline_seconds": 0,
                }
            )
        return True

    def create_message(self, parent: str, body: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"parent": parent, "body": body}
        if (body.get("thread") or {}).get("name"):
            kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        return self.chat.spaces().messages().create(**kwargs).execute()

    def patch_message(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        patch_body = {key: value for key, value in body.items() if key != "thread"}
        update_mask = ",".join(
            field for field in ("text", "cardsV2") if field in patch_body
        ) or "text"
        return (
            self.chat.spaces()
            .messages()
            .patch(name=name, updateMask=update_mask, body=patch_body)
            .execute()
        )

    def delete_message(self, name: str) -> None:
        self.chat.spaces().messages().delete(name=name).execute()


class SlackRelay:
    """Credentialed Slack Socket Mode and Web API transport."""

    API_METHOD_PREFIXES = (
        "assistant_",
        "auth_test",
        "chat_",
        "conversations_",
        "files_",
        "reactions_",
        "users_",
    )

    def __init__(
        self, bot_tokens: str, app_token: str, max_file_bytes: int = 20 * 1024 * 1024
    ) -> None:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient

        tokens = [token.strip() for token in bot_tokens.split(",") if token.strip()]
        if not tokens or not app_token:
            raise ValueError("Slack bot and app tokens are required")
        self.max_file_bytes = max_file_bytes
        self.clients: dict[str, Any] = {}
        self.workspaces: list[dict[str, str]] = []
        self.primary_client = WebClient(token=tokens[0])
        for token in tokens:
            client = WebClient(token=token)
            identity = client.auth_test()
            team_id = str(identity.get("team_id", ""))
            self.clients[team_id] = client
            self.workspaces.append(
                {
                    "teamId": team_id,
                    "teamName": str(identity.get("team", "")),
                    "botUserId": str(identity.get("user_id", "")),
                    "botName": str(identity.get("user", "")),
                }
            )
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._receipts: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.socket_client = SocketModeClient(
            app_token=app_token, web_client=self.primary_client
        )
        self.socket_client.socket_mode_request_listeners.append(self._on_event)
        self.socket_client.connect()

    def _on_event(self, client: Any, request: Any) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse

        client.send_socket_mode_response(
            SocketModeResponse(envelope_id=request.envelope_id)
        )
        self._events.put(
            {
                "type": str(request.type),
                "payload": request.payload,
            }
        )

    def pull(self, timeout_seconds: int = 20) -> dict[str, Any] | None:
        try:
            event = self._events.get(timeout=max(timeout_seconds, 1))
        except queue.Empty:
            return None
        receipt = str(uuid.uuid4())
        with self._lock:
            self._receipts[receipt] = event
        return {"receipt": receipt, **event}

    def settle(self, receipt: str, acknowledge: bool) -> bool:
        with self._lock:
            event = self._receipts.pop(receipt, None)
        if event is None:
            return False
        if not acknowledge:
            self._events.put(event)
        return True

    def bootstrap(self) -> list[dict[str, str]]:
        return self.workspaces

    def _client(self, team_id: str) -> Any:
        return self.clients.get(team_id) or self.primary_client

    def _decode_argument(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._decode_argument(item) for item in value]
        if isinstance(value, dict):
            if set(value).issubset({"__bytesBase64"}) and "__bytesBase64" in value:
                content = base64.b64decode(value["__bytesBase64"], validate=True)
                if len(content) > self.max_file_bytes:
                    raise ValueError("Slack upload exceeds relay size limit")
                return content
            if "__fileBase64" in value:
                content = base64.b64decode(value["__fileBase64"], validate=True)
                if len(content) > self.max_file_bytes:
                    raise ValueError("Slack upload exceeds relay size limit")
                stream = io.BytesIO(content)
                stream.name = str(value.get("filename", "upload"))
                return stream
            return {key: self._decode_argument(item) for key, item in value.items()}
        return value

    def api_call(
        self, team_id: str, method: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if (
            not method
            or method.startswith("_")
            or not method.startswith(self.API_METHOD_PREFIXES)
        ):
            raise ValueError("Slack API method is not available through the relay")
        client_method = getattr(self._client(team_id), method, None)
        if client_method is None or not callable(client_method):
            raise ValueError("unknown Slack API method")
        response = client_method(**self._decode_argument(arguments))
        return dict(response)

    def download(self, team_id: str, url: str) -> bytes:
        def is_slack_url(value: str) -> bool:
            parsed = urllib.parse.urlparse(value)
            hostname = (parsed.hostname or "").lower()
            return parsed.scheme == "https" and (
                hostname == "slack.com" or hostname.endswith(".slack.com")
            )

        if not is_slack_url(url):
            raise ValueError("Slack file URL must use HTTPS on a slack.com host")

        class SlackRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(
                self,
                request: Any,
                file_pointer: Any,
                code: int,
                message: str,
                headers: Any,
                new_url: str,
            ) -> Any:
                if not is_slack_url(new_url):
                    raise ValueError("Slack file redirect left slack.com")
                return super().redirect_request(
                    request, file_pointer, code, message, headers, new_url
                )

        token = self._client(team_id).token
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        opener = urllib.request.build_opener(SlackRedirectHandler())
        with opener.open(request, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/html" in content_type.lower():
                raise ValueError("Slack returned HTML instead of file content")
            content = response.read(self.max_file_bytes + 1)
        if len(content) > self.max_file_bytes:
            raise ValueError("Slack file exceeds relay size limit")
        return content


@dataclass(frozen=True)
class Rule:
    rule_id: str
    pattern: re.Pattern[str]
    message: str


class Policy:
    def __init__(self, rules: list[Rule], blocked_message: str) -> None:
        self.rules = rules
        self.blocked_message = blocked_message

    @classmethod
    def load(cls, path: str) -> "Policy":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        blocked_message = payload.get(
            "blockedMessage", "Command blocked for security reasons."
        )
        rules = []
        for item in payload.get("rules", []):
            rules.append(
                Rule(
                    rule_id=item["id"],
                    pattern=re.compile(item["pattern"], re.IGNORECASE | re.MULTILINE),
                    message=item.get("message", blocked_message),
                )
            )
        return cls(rules=rules, blocked_message=blocked_message)

    def blocked_by(self, command: str) -> Rule | None:
        return next((rule for rule in self.rules if rule.pattern.search(command)), None)


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool
    timed_out: bool


class CommandExecutor:
    def __init__(
        self, timeout_seconds: int, max_output_bytes: int, state_dir: str
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.state_dir = Path(state_dir)
        self.home_dir = self.state_dir / "home"
        self.workspace_dir = self.state_dir / "workspace"
        self.tmp_dir = self.state_dir / "tmp"
        self.context_dir = self.state_dir / "contexts"
        self._context_lock = threading.Lock()
        for path in (self.home_dir, self.workspace_dir, self.tmp_dir, self.context_dir):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _requires_kubernetes_context(command: str) -> bool:
        return re.search(r"(?:^|[;&|()`\n])\s*(?:[A-Za-z0-9_./-]+/)?kubectl(?:\s|$)", command) is not None

    @staticmethod
    def validate_kubernetes_context(context: Any) -> dict[str, str] | None:
        if context is None:
            return None
        if not isinstance(context, dict):
            raise ValueError("context.kubernetes must be an object")
        required = ("contextName", "projectId", "location", "clusterName")
        values: dict[str, str] = {}
        for key in (*required, "defaultNamespace"):
            value = context.get(key, "")
            if not isinstance(value, str):
                raise ValueError(f"context.kubernetes.{key} must be a string")
            values[key] = value.strip()
        missing = [key for key in required if not values[key]]
        if missing:
            raise ValueError(
                "Kubernetes execution context is missing " + ", ".join(missing)
            )
        return values

    def _cached_kubeconfig(self, context: dict[str, str]) -> Path:
        cache_key = "__".join(
            re.sub(r"[^A-Za-z0-9_.-]", "_", context[key])
            for key in ("projectId", "location", "clusterName", "contextName")
        )
        path = self.context_dir / f"{cache_key}.yaml"
        with self._context_lock:
            if path.exists():
                return path
            temporary = path.with_suffix(".tmp")
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(self.home_dir),
                    "KUBECONFIG": str(temporary),
                    "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
                }
            )
            result = subprocess.run(
                [
                    "/usr/bin/gcloud",
                    "container",
                    "clusters",
                    "get-credentials",
                    context["clusterName"],
                    "--location",
                    context["location"],
                    "--project",
                    context["projectId"],
                ],
                env=env,
                cwd=self.workspace_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=min(self.timeout_seconds, 60),
            )
            if result.returncode != 0:
                temporary.unlink(missing_ok=True)
                message = result.stderr.decode("utf-8", errors="replace").strip()
                raise ValueError(f"failed to prepare Kubernetes context: {message}")
            if not temporary.exists():
                raise ValueError("failed to prepare Kubernetes context: kubeconfig was not created")
            rendered = temporary.read_text(encoding="utf-8")
            if context["contextName"] not in rendered:
                temporary.unlink(missing_ok=True)
                raise ValueError(
                    "prepared kubeconfig does not contain requested context "
                    + context["contextName"]
                )
            temporary.replace(path)
        return path

    def execute(
        self,
        command: str,
        stdin: str | None = None,
        kubernetes_context: dict[str, str] | None = None,
    ) -> ExecutionResult:
        started = time.monotonic()
        timed_out = False
        env = os.environ.copy()
        env["HOME"] = str(self.home_dir)
        env["TMPDIR"] = str(self.tmp_dir)
        kubeconfig_copy: tempfile.NamedTemporaryFile[Any] | None = None
        if self._requires_kubernetes_context(command):
            if kubernetes_context is None:
                raise ValueError("kubectl commands require context.kubernetes")
            cached = self._cached_kubeconfig(kubernetes_context)
            kubeconfig_copy = tempfile.NamedTemporaryFile(
                dir=self.tmp_dir, prefix="kubeconfig-", suffix=".yaml", delete=False
            )
            kubeconfig_copy.write(cached.read_bytes())
            kubeconfig_copy.close()
            env["KUBECONFIG"] = kubeconfig_copy.name
        try:
            process = subprocess.Popen(
                ["/bin/bash", "--noprofile", "--norc", "-c", command],
                cwd=self.workspace_dir,
                env=env,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout_bytes, stderr_bytes = process.communicate(
                    input=stdin.encode("utf-8") if stdin is not None else None,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                stdout_bytes, stderr_bytes = process.communicate()
        finally:
            if kubeconfig_copy is not None:
                Path(kubeconfig_copy.name).unlink(missing_ok=True)

        stdout_bytes, stdout_truncated = self._truncate(stdout_bytes)
        stderr_bytes, stderr_truncated = self._truncate(stderr_bytes)
        duration_ms = int((time.monotonic() - started) * 1000)
        return ExecutionResult(
            exit_code=124 if timed_out else process.returncode,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
            truncated=stdout_truncated or stderr_truncated,
            timed_out=timed_out,
        )

    def _truncate(self, value: bytes) -> tuple[bytes, bool]:
        if len(value) <= self.max_output_bytes:
            return value, False
        return value[: self.max_output_bytes], True


class CredentialProxyHandler(BaseHTTPRequestHandler):
    policy: Policy
    executor: CommandExecutor
    max_request_bytes: int
    slack_max_request_bytes: int
    chat_relay: GoogleChatRelay | None = None
    slack_relay: SlackRelay | None = None

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/v1/chat/slack/events"):
            if self.slack_relay is None:
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Slack relay disabled"}
                )
                return
            try:
                self._json(HTTPStatus.OK, {"event": self.slack_relay.pull()})
            except Exception as exc:
                LOGGER.warning("Slack event pull failed: %s", type(exc).__name__)
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Slack event pull failed"}
                )
            return
        if self.path.startswith("/v1/chat/events"):
            if self.chat_relay is None:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "chat relay disabled"})
                return
            try:
                event = self.chat_relay.pull()
                self._json(HTTPStatus.OK, {"event": event})
            except Exception as exc:
                LOGGER.warning("chat event pull failed: %s", type(exc).__name__)
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "chat event pull failed"})
            return
        if self.path != "/healthz":
            self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return
        self._json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/v1/chat/slack/"):
            self._handle_slack_post()
            return
        if self.path.startswith("/v1/chat/"):
            self._handle_chat_post()
            return
        if self.path != "/v1/exec":
            self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return
        if content_length <= 0 or content_length > self.max_request_bytes:
            self._json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "command request exceeds configured size limit"},
            )
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
            command = payload["command"]
            if not isinstance(command, str) or not command:
                raise ValueError("command must be a non-empty string")
            stdin = payload.get("stdin")
            if stdin is not None and not isinstance(stdin, str):
                raise ValueError("stdin must be a string")
            request_context = payload.get("context")
            if request_context is not None and not isinstance(request_context, dict):
                raise ValueError("context must be an object")
            kubernetes_context = self.executor.validate_kubernetes_context(
                (request_context or {}).get("kubernetes")
            )
            if self.executor._requires_kubernetes_context(command) and kubernetes_context is None:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "status": "rejected",
                        "code": "EXECUTION_CONTEXT_REQUIRED",
                        "message": "kubectl commands require context.kubernetes",
                    },
                )
                return
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        request_id = str(payload.get("requestId", ""))
        rule = self.policy.blocked_by(command)
        if rule is not None:
            LOGGER.warning(
                "command blocked request_id=%s rule=%s", request_id, rule.rule_id
            )
            self._json(
                HTTPStatus.FORBIDDEN,
                {
                    "status": "blocked",
                    "code": "SECURITY_POLICY_BLOCKED",
                    "rule": rule.rule_id,
                    "message": rule.message,
                },
            )
            return

        try:
            result = self.executor.execute(
                command, stdin=stdin, kubernetes_context=kubernetes_context
            )
        except ValueError as exc:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {
                    "status": "rejected",
                    "code": "EXECUTION_CONTEXT_INVALID",
                    "message": str(exc),
                },
            )
            return
        LOGGER.info(
            "command complete request_id=%s exit_code=%d duration_ms=%d truncated=%s",
            request_id,
            result.exit_code,
            result.duration_ms,
            result.truncated,
        )
        self._json(
            HTTPStatus.OK,
            {
                "status": "completed",
                "exitCode": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "durationMs": result.duration_ms,
                "truncated": result.truncated,
                "timedOut": result.timed_out,
                "effectiveContext": {"kubernetes": kubernetes_context}
                if kubernetes_context is not None
                else {},
            },
        )

    def _read_json_body(self, max_bytes: int | None = None) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > (
            max_bytes or self.max_request_bytes
        ):
            raise ValueError("request exceeds configured size limit")
        payload = json.loads(self.rfile.read(content_length))
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        return payload

    def _handle_chat_post(self) -> None:
        if self.chat_relay is None:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "chat relay disabled"})
            return
        try:
            payload = self._read_json_body()
            if self.path == "/v1/chat/events/ack":
                ok = self.chat_relay.settle(str(payload.get("receipt", "")), True)
                self._json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"settled": ok})
                return
            if self.path == "/v1/chat/events/nack":
                ok = self.chat_relay.settle(str(payload.get("receipt", "")), False)
                self._json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"settled": ok})
                return
            if self.path == "/v1/chat/messages/create":
                result = self.chat_relay.create_message(str(payload["parent"]), payload["body"])
                self._json(HTTPStatus.OK, {"message": result})
                return
            if self.path == "/v1/chat/messages/patch":
                result = self.chat_relay.patch_message(str(payload["name"]), payload["body"])
                self._json(HTTPStatus.OK, {"message": result})
                return
            if self.path == "/v1/chat/messages/delete":
                self.chat_relay.delete_message(str(payload["name"]))
                self._json(HTTPStatus.OK, {"deleted": True})
                return
            self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            LOGGER.warning("chat relay operation failed path=%s type=%s", self.path, type(exc).__name__)
            self._json(HTTPStatus.BAD_GATEWAY, {"error": "Google Chat operation failed"})

    def _handle_slack_post(self) -> None:
        if self.slack_relay is None:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Slack relay disabled"}
            )
            return
        try:
            payload = self._read_json_body(self.slack_max_request_bytes)
            if self.path == "/v1/chat/slack/bootstrap":
                self._json(
                    HTTPStatus.OK,
                    {"workspaces": self.slack_relay.bootstrap()},
                )
                return
            if self.path == "/v1/chat/slack/events/ack":
                ok = self.slack_relay.settle(str(payload.get("receipt", "")), True)
                self._json(
                    HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"settled": ok}
                )
                return
            if self.path == "/v1/chat/slack/events/nack":
                ok = self.slack_relay.settle(str(payload.get("receipt", "")), False)
                self._json(
                    HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"settled": ok}
                )
                return
            if self.path == "/v1/chat/slack/api":
                arguments = payload.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be an object")
                result = self.slack_relay.api_call(
                    str(payload.get("teamId", "")),
                    str(payload.get("method", "")),
                    arguments,
                )
                self._json(HTTPStatus.OK, {"response": result})
                return
            if self.path == "/v1/chat/slack/files/download":
                content = self.slack_relay.download(
                    str(payload.get("teamId", "")), str(payload["url"])
                )
                self._json(
                    HTTPStatus.OK,
                    {"data": base64.b64encode(content).decode("ascii")},
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            LOGGER.warning(
                "Slack relay operation failed path=%s type=%s",
                self.path,
                type(exc).__name__,
            )
            self._json(HTTPStatus.BAD_GATEWAY, {"error": "Slack operation failed"})

    def log_message(self, message: str, *args: Any) -> None:
        LOGGER.info("http " + message, *args)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(args: argparse.Namespace) -> None:
    CredentialProxyHandler.policy = Policy.load(args.policy)
    CredentialProxyHandler.executor = CommandExecutor(
        timeout_seconds=args.timeout_seconds,
        max_output_bytes=args.max_output_bytes,
        state_dir=args.state_dir,
    )
    CredentialProxyHandler.max_request_bytes = args.max_request_bytes
    CredentialProxyHandler.slack_max_request_bytes = int(
        os.getenv("SLACK_RELAY_MAX_REQUEST_BYTES", str(28 * 1024 * 1024))
    )
    chat_project = os.getenv("GOOGLE_CHAT_PROJECT_ID", "").strip()
    chat_subscription = os.getenv("GOOGLE_CHAT_SUBSCRIPTION_NAME", "").strip()
    if chat_project and chat_subscription:
        CredentialProxyHandler.chat_relay = GoogleChatRelay(
            chat_project, chat_subscription
        )
        LOGGER.info("Google Chat relay enabled project=%s subscription=<redacted>", chat_project)
    slack_bot_tokens = os.getenv("SLACK_BOT_TOKEN", "").strip()
    slack_app_token = os.getenv("SLACK_APP_TOKEN", "").strip()
    if slack_bot_tokens and slack_app_token:
        CredentialProxyHandler.slack_relay = SlackRelay(
            slack_bot_tokens,
            slack_app_token,
            max_file_bytes=int(
                os.getenv("SLACK_RELAY_MAX_FILE_BYTES", str(20 * 1024 * 1024))
            ),
        )
        LOGGER.info(
            "Slack relay enabled workspaces=%d",
            len(CredentialProxyHandler.slack_relay.bootstrap()),
        )
    server = ThreadingHTTPServer((args.host, args.port), CredentialProxyHandler)
    LOGGER.info("credential proxy listening on %s:%d", args.host, args.port)
    server.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--policy",
        default=os.getenv(
            "CREDENTIAL_PROXY_POLICY", "/etc/credential-proxy/policy.json"
        ),
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("CREDENTIAL_PROXY_PORT", "8765"))
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("CREDENTIAL_PROXY_TIMEOUT_SECONDS", "300")),
    )
    parser.add_argument(
        "--max-request-bytes",
        type=int,
        default=int(os.getenv("CREDENTIAL_PROXY_MAX_REQUEST_BYTES", "1048576")),
    )
    parser.add_argument(
        "--max-output-bytes",
        type=int,
        default=int(os.getenv("CREDENTIAL_PROXY_MAX_OUTPUT_BYTES", "4194304")),
    )
    parser.add_argument(
        "--state-dir",
        default=os.getenv("CREDENTIAL_PROXY_STATE_DIR", "/var/lib/credential-proxy"),
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    serve(parse_args())
