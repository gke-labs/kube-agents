#!/usr/bin/env python3
"""Credential proxy for restricted credentialed CLI execution."""

from __future__ import annotations

import argparse
import base64
import hmac
import http.client
import io
import json
import logging
import os
import queue
import re
import shlex
import signal
import shutil
import socketserver
import subprocess
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
SLACK_EVENT_QUEUE_MAXSIZE = 1000
SLACK_ERROR_DIAGNOSTIC_FIELDS = ("ok", "error", "needed", "provided")

# GitHub "owner/name" slug validation. Each segment is matched with a single,
# unambiguous character class rather than two adjacent "+" groups around the
# "/" separator, so the match is linear-time and cannot be forced into
# polynomial backtracking (ReDoS). The length guard bounds untrusted input as
# defense-in-depth; 256 is far above real GitHub owner/name limits, so valid
# input is never rejected.
MAX_REPOSITORY_LENGTH = 256
_REPOSITORY_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")


def is_valid_repository(repository: Any) -> bool:
    """Return True if ``repository`` is a well-formed ``owner/name`` slug."""
    if not isinstance(repository, str) or len(repository) > MAX_REPOSITORY_LENGTH:
        return False
    owner, slash, name = repository.partition("/")
    if not slash:
        return False
    return (
        _REPOSITORY_SEGMENT.fullmatch(owner) is not None
        and _REPOSITORY_SEGMENT.fullmatch(name) is not None
    )


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """HTTP server over a private Unix socket used behind Envoy."""

    daemon_threads = True


class AgentAPIProxyHandler(BaseHTTPRequestHandler):
    """Authenticate the external PlatformAgent API without sharing its key."""

    external_key: str
    upstream_key: str
    upstream_host = "127.0.0.1"
    upstream_port = 8642
    max_request_bytes = 10 * 1024 * 1024
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

    def _proxy(self) -> None:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.external_key}"
        if not hmac.compare_digest(supplied, expected):
            self.send_error(HTTPStatus.UNAUTHORIZED)
            return
        if self.headers.get("Transfer-Encoding"):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        if content_length < 0 or content_length > self.max_request_bytes:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        body = self.rfile.read(content_length) if content_length else None
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower()
            not in {
                "authorization",
                "connection",
                "content-length",
                "host",
                "proxy-authorization",
                "transfer-encoding",
                "upgrade",
            }
        }
        headers["Authorization"] = f"Bearer {self.upstream_key}"
        if body is not None:
            headers["Content-Length"] = str(len(body))

        upstream = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=300
        )
        response_started = False
        try:
            upstream.request(self.command, self.path, body=body, headers=headers)
            response = upstream.getresponse()
            self.send_response(response.status, self._sanitize_header(response.reason))
            for name, value in response.getheaders():
                if name.lower() not in {
                    "connection",
                    "keep-alive",
                    "proxy-authenticate",
                    "transfer-encoding",
                    "upgrade",
                }:
                    self.send_header(
                        self._sanitize_header(name),
                        self._sanitize_header(value),
                    )
            self.send_header("Connection", "close")
            self.end_headers()
            response_started = True
            while chunk := response.read(64 * 1024):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (ConnectionError, TimeoutError, OSError, http.client.HTTPException):
            LOGGER.warning("PlatformAgent API upstream request failed", exc_info=True)
            if not response_started:
                self.send_error(HTTPStatus.BAD_GATEWAY)
            self.close_connection = True
        finally:
            upstream.close()

    @staticmethod
    def _sanitize_header(value: str) -> str:
        """Strip CR/LF so upstream headers cannot split the response (CWE-113)."""
        return value.replace("\r", "").replace("\n", "")

    def log_message(self, message: str, *args: Any) -> None:
        LOGGER.info("agent-api " + message, *args)


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

    def api_call(
        self, resource: list[str], method: str, arguments: dict[str, Any]
    ) -> Any:
        target = self.chat
        for name in resource:
            if not isinstance(name, str) or not name or name.startswith("_"):
                raise ValueError("invalid Google Chat API resource")
            target = getattr(target, name)()
        if not method or method.startswith("_"):
            raise ValueError("invalid Google Chat API method")
        operation = getattr(target, method)(**arguments)
        return operation.execute()


def _slack_error_fields(exc: Exception) -> dict[str, Any] | None:
    """Return the whitelisted diagnostic fields a Slack API error carried.

    ``None`` means the exception carried no payload at all, which is a
    different thing from a payload holding nothing worth relaying — the caller
    distinguishes the two. Only SLACK_ERROR_DIAGNOSTIC_FIELDS cross this
    boundary: the payload is a response body from a call made with the relay's
    own credential, and this value is both logged and returned to the agent.
    """
    response = getattr(exc, "response", None)
    payload = None
    if response is not None:
        if hasattr(response, "data") and isinstance(response.data, dict):
            payload = response.data
        elif hasattr(response, "to_dict"):
            try:
                payload = response.to_dict()
            except Exception:
                payload = None
        elif isinstance(response, dict):
            payload = response
    if not isinstance(payload, dict):
        return None
    return {k: payload[k] for k in SLACK_ERROR_DIAGNOSTIC_FIELDS if k in payload}


def _slack_error_detail(exc: Exception) -> str:
    """Return Slack API error details as a JSON string or fallback text."""
    fields = _slack_error_fields(exc)
    if fields is not None:
        try:
            return json.dumps(fields, sort_keys=True)
        except Exception:
            pass
    response = getattr(exc, "response", None)
    try:
        detail = (
            response.get("error")
            if response is not None and hasattr(response, "get")
            else None
        )
    except Exception:
        detail = None
    return str(detail or "unknown")


class SlackRelay:
    """Credentialed Slack Socket Mode and Web API transport."""

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
        self.primary_client = None
        for token in tokens:
            client = WebClient(token=token)
            try:
                identity = client.auth_test()
            except Exception as exc:
                LOGGER.error(
                    "Slack bot token authentication failed type=%s error=%s",
                    type(exc).__name__,
                    _slack_error_detail(exc),
                )
                continue
            team_id = str(identity.get("team_id", ""))
            if not team_id:
                LOGGER.error("Slack bot token authentication returned no team ID")
                continue
            if self.primary_client is None:
                self.primary_client = client
            self.clients[team_id] = client
            self.workspaces.append(
                {
                    "teamId": team_id,
                    "teamName": str(identity.get("team", "")),
                    "botUserId": str(identity.get("user_id", "")),
                    "botName": str(identity.get("user", "")),
                }
            )
        if self.primary_client is None:
            raise RuntimeError("no Slack bot token could be authenticated")
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=SLACK_EVENT_QUEUE_MAXSIZE
        )
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
        event = {
            "type": str(request.type),
            "payload": request.payload,
        }
        try:
            self._events.put_nowait(event)
        except queue.Full:
            LOGGER.warning("Slack event queue is full; dropping event")

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
            event = self._receipts.get(receipt)
            if event is None:
                return False
            if not acknowledge:
                try:
                    self._events.put_nowait(event)
                except queue.Full:
                    LOGGER.warning("Slack event queue is full; cannot requeue event")
                    return False
            del self._receipts[receipt]
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
        if not method or method.startswith("_"):
            raise ValueError("Slack API method is not available through the relay")
        response = self._client(team_id).api_call(
            method, **self._decode_argument(arguments)
        )
        # SlackResponse defines no keys(), so dict() would fall back to the
        # iterator protocol and raise. The parsed payload lives on .data.
        result = dict(response.data)
        if hasattr(response, "headers") and response.headers:
            WANTED = ("x-oauth-scopes", "x-accepted-oauth-scopes")
            headers = {k: v for k, v in response.headers.items() if k.lower() in WANTED}
            if headers:
                result["__headers"] = headers
        return result

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

    def blocked_by(self, argv: list[str]) -> Rule | None:
        command = shlex.join(argv)
        return next((rule for rule in self.rules if rule.pattern.search(command)), None)


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool
    timed_out: bool


# A kubeconfig is not passive data. `users[].user.exec.command` runs a program
# here in the sidecar, next to the credentials; `clusters[].cluster.server` and
# `proxy-url` choose where the access token minted by gke-gcloud-auth-plugin is
# sent; `users[].user.tokenFile` reads a file of the author's choosing and sends
# it as the bearer token. The policy engine cannot see any of that, because every
# rule it holds matches on argv and the argv is only ever `kubectl get pods`.
#
# The agent can write anywhere in the shared workspace, so any kubeconfig it
# names is a document it controls. Rather than validate that document — a
# denylist over a format that keeps growing, and racy besides, since the file can
# be rewritten between the check and the open — the proxy reads exactly one
# string out of it and regenerates the rest. See CommandExecutor._resolve_kubeconfig.
_GKE_CONTEXT_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Enough for any real kubeconfig; the point is that this file is attacker-chosen
# and gets read into memory before anything is known about it.
MAX_KUBECONFIG_BYTES = 1 << 20


@dataclass(frozen=True)
class ClusterTarget:
    """A GKE cluster identified well enough to re-fetch credentials for it."""

    project: str
    location: str
    cluster: str

    @property
    def context_name(self) -> str:
        return f"gke_{self.project}_{self.location}_{self.cluster}"


def parse_gke_context(context: str) -> ClusterTarget | None:
    """Recover the cluster triple from a `gke_<project>_<location>_<cluster>` name.

    This is the same convention the operator builds in `buildCredentialProxyEnv`
    and the Cluster Agent preflight compares against, and it is what makes the
    regeneration below possible: the context name alone says which cluster to ask
    Google for. Underscores are the separator and none of the three components may
    contain one, so a 4-way split is unambiguous.

    Each component is held to the GKE naming rules, which is also what keeps the
    value safe to use in a filename — no separators, no dots, no traversal.
    """
    parts = context.split("_", 3)
    if len(parts) != 4 or parts[0] != "gke":
        return None
    project, location, cluster = parts[1], parts[2], parts[3]
    if not all(_GKE_CONTEXT_COMPONENT.match(part) for part in (project, location, cluster)):
        return None
    return ClusterTarget(project=project, location=location, cluster=cluster)


def _is_get_credentials(argv: list[str]) -> bool:
    """Is this the one command that legitimately authors a kubeconfig?

    Matched on the subcommand sequence rather than on position, so global flags
    may appear anywhere ahead of it.
    """
    if not argv or argv[0] != "gcloud":
        return False
    try:
        index = argv.index("container")
    except ValueError:
        return False
    return argv[index + 1 : index + 3] == ["clusters", "get-credentials"]


def read_current_context(text: str) -> str | None:
    """Read `current-context` out of a kubeconfig the way kubectl would.

    `yaml.safe_load`, deliberately, and never `yaml.CSafeLoader`. The C loader
    recurses in C: a deeply nested document takes the whole sidecar down with
    SIGSEGV, where the pure-Python loader raises a catchable `RecursionError`.
    This input is chosen by the agent, so that is the difference between one
    rejected request and a dead credential proxy. `safe_load` picks the Python
    loader on its own; the point of saying so is that switching it would be a
    denial-of-service, not an optimisation.

    Alias expansion is not a concern here. PyYAML resolves every reference to an
    anchor to the same node and caches the object built from it, so a
    billion-laughs document costs memory proportional to its own size rather
    than to its nominal expansion.

    Anything else — a syntax error, several documents, a top level that is not a
    mapping, a non-string `current-context` — reads as absent, and the caller
    turns that into a rejection.
    """
    import yaml  # lazy: keeps the module importable without pyyaml, as elsewhere in this directory

    try:
        document = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    context = document.get("current-context")
    if not isinstance(context, str):
        return None
    return context.strip() or None


# Identity stamped on commits the proxy makes on the agent's behalf. `git commit`
# exits 128 — "Please tell me who you are" — with no identity configured, and the
# commit runs here rather than in the agent container, so a .gitconfig over there
# would never be read. The address uses the reserved `.invalid` TLD (RFC 2606) so
# an automated commit can never be attributed to a real mailbox that happens to
# exist. Both are overridable per deployment.
DEFAULT_GIT_AUTHOR_NAME = "kube-agents platform agent"
DEFAULT_GIT_AUTHOR_EMAIL = "platform-agent@kube-agents.invalid"

# The marker `gitops_workspace` drops in a leased workspace. The two names must
# agree: renaming one without the other locks every skill out of git.
GIT_LEASE_MARKER = ".lease"

# git subcommands that write a working tree or a remote ref. Anything here is
# refused unless it runs inside a leased workspace, because the pod runs many
# agents against one shared volume and these are the verbs with which one agent
# destroys another's work — the incident that prompted the rule was
# `submit-suggestion` running `checkout -b` and `push -f` inside the clone a
# fleet audit was midway through.
#
# A denylist rather than a read-only allowlist, deliberately. The set of verbs
# that can mutate a tree is closed and well known; the set of read verbs is not,
# and a new one silently failing closed would be a worse outcome than the race
# this closes. `clone` is absent on purpose: it runs at the lease root, one
# directory above the tree it is about to create, and it cannot damage a tree
# that does not exist yet. `fetch` is absent for the same reason it is safe —
# it writes remote-tracking refs and nothing in the working tree. `config`,
# `remote` and every read verb are likewise untouched.
#
# `pull`, `submodule` and `sparse-checkout` are here because each one is a
# working-tree write wearing another word: `pull` is `fetch` plus the `merge`
# or `rebase` two lines up, `submodule update` checks out whole directories,
# and `sparse-checkout set` adds and removes files across the entire tree. All
# three were reachable in a clone another agent was midway through.
GIT_MUTATING_SUBCOMMANDS = frozenset(
    {
        "add", "am", "apply", "branch", "checkout", "cherry-pick", "clean",
        "commit", "merge", "mv", "pull", "push", "rebase", "reset", "restore",
        "revert", "rm", "sparse-checkout", "stash", "submodule", "switch",
        "tag", "update-ref", "worktree",
    }
)

# git's own global options, split by whether they consume the next argument.
# Needed to find the subcommand in `git --literal-pathspecs add …` (which
# audit_report issues) without mistaking a flag for a verb.
_GIT_GLOBAL_WITH_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
)


def _git_plan(argv: list[str]) -> tuple[str | None, list[str]]:
    """The subcommand in `argv`, plus every directory its `-C` flags select.

    `-C` is returned rather than ignored because git applies it cumulatively
    before running the subcommand: `git -C /elsewhere commit` executes nowhere
    near the working directory the caller reported, so a containment check that
    only looked at `cwd` would be checking the wrong path.
    """
    directories: list[str] = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-"):
            return token, directories
        name, sep, inline = token.partition("=")
        if name == "-C":
            if sep:
                directories.append(inline)
            elif index + 1 < len(argv):
                directories.append(argv[index + 1])
        if name in _GIT_GLOBAL_WITH_VALUE and not sep:
            index += 1
        index += 1
    return None, directories


class CommandExecutor:
    ALLOWED_EXECUTABLES = ("gcloud", "kubectl", "gh", "git")

    def __init__(
        self, timeout_seconds: int, max_output_bytes: int, state_dir: str
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.state_dir = Path(state_dir)
        self.home_dir = self.state_dir / "home"
        self.workspace_dir = Path(
            os.getenv("CREDENTIAL_PROXY_WORKSPACE_ROOT", str(self.state_dir / "workspace"))
        ).resolve()
        # On by default; the escape hatch exists so an operator can unblock a
        # skill that has not been migrated to leases yet without shipping a new
        # image. See `git_lease_violation`.
        self.require_git_lease = os.getenv(
            "CREDENTIAL_PROXY_REQUIRE_GIT_LEASE", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
        self.tmp_dir = self.state_dir / "tmp"
        self.config_dir = self.home_dir / ".config"
        self.cache_dir = self.home_dir / ".cache"
        self.local_state_dir = self.home_dir / ".local" / "state"
        self.kube_dir = self.home_dir / ".kube"
        # Every kubeconfig any agent-selected command actually reads lives here.
        # It has to be under the state dir: that is a sidecar-only emptyDir
        # (`credential-proxy-state` in platformagent_manifests.go), whereas the
        # workspace is the PVC the agent writes to. Keeping the file out of the
        # agent's reach is what removes the rewrite-after-check race — there is
        # no window in which the document can change between validation and use,
        # because the agent never had a handle on the document at all.
        self.kubeconfig_dir = self.state_dir / "kubeconfigs"
        for path in (
            self.home_dir,
            self.workspace_dir,
            self.tmp_dir,
            self.config_dir,
            self.cache_dir,
            self.local_state_dir,
            self.kube_dir,
            self.kubeconfig_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        # Serialises the `get-credentials` that fills a cache miss. Generation is
        # rare and the server is threaded, so a single lock is cheaper than the
        # bookkeeping needed to make it per-cluster.
        self._kubeconfig_lock = threading.Lock()
        trusted_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        self.executables = {
            name: shutil.which(name, path=trusted_path)
            for name in self.ALLOWED_EXECUTABLES
        }
        self.environment = {
            "PATH": trusted_path,
            "HOME": str(self.home_dir),
            "TMPDIR": str(self.tmp_dir),
            "XDG_CONFIG_HOME": str(self.config_dir),
            "XDG_CACHE_HOME": str(self.cache_dir),
            "XDG_STATE_HOME": str(self.local_state_dir),
            "CLOUDSDK_CONFIG": str(self.config_dir / "gcloud"),
            "GH_CONFIG_DIR": str(self.config_dir / "gh"),
            "KUBECONFIG": str(self.home_dir / ".kube" / "config"),
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
        }
        # Forward only variables required by supported credential clients. Chat
        # tokens and proxy control variables must never enter an agent-selected
        # subprocess, even though that subprocess runs in the sidecar.
        for name in (
            "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "LANG",
            "LC_ALL",
            "TOKEN_BROKER_URL",
            "KSA_TOKEN_FILE",
        ):
            if name in os.environ:
                self.environment[name] = os.environ[name]
        # Applied per invocation in `_execute`, and only to git, rather than
        # written once to ~/.gitconfig: the identity then stays scoped to the
        # proxied commands that need it and leaves no ambient state in the
        # sidecar's home for anything else to pick up. An operator who sets the
        # override to an empty string means "unset", not "commit with no name",
        # so an empty value falls back rather than reinstating the exit 128.
        author_name = (
            os.getenv("CREDENTIAL_PROXY_GIT_AUTHOR_NAME", "").strip() or DEFAULT_GIT_AUTHOR_NAME
        )
        author_email = (
            os.getenv("CREDENTIAL_PROXY_GIT_AUTHOR_EMAIL", "").strip() or DEFAULT_GIT_AUTHOR_EMAIL
        )
        self.git_identity = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }

    def bootstrap(self, command: str) -> None:
        """Prepare the trusted shell profile without interpreting later commands."""
        if not command.strip():
            return
        bootstrap_environment = self.environment.copy()
        for name in (
            "GKE_PROJECT_ID",
            "GKE_CLUSTER_NAME",
            "GKE_LOCATION",
            "KUBE_CONTEXT_NAME",
            "KUBE_DEFAULT_NAMESPACE",
        ):
            if name in os.environ:
                bootstrap_environment[name] = os.environ[name]
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", command],
            cwd=self.workspace_dir,
            env=bootstrap_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(self.timeout_seconds, 120),
        )
        if result.returncode != 0:
            # The command's output is the only useful diagnostic when the
            # bootstrap fails, but it must not travel with the exception, which
            # can surface outside the sidecar. Log it here instead, where only an
            # operator reading the sidecar's own logs sees it, and leave the
            # message itself output-free.
            stdout_bytes, stdout_truncated = self._truncate(result.stdout)
            stderr_bytes, stderr_truncated = self._truncate(result.stderr)
            LOGGER.error(
                "credential proxy shell bootstrap failed with exit code %s\n"
                "bootstrap stdout%s:\n%s\nbootstrap stderr%s:\n%s",
                result.returncode,
                " (truncated)" if stdout_truncated else "",
                stdout_bytes.decode("utf-8", errors="replace").strip(),
                " (truncated)" if stderr_truncated else "",
                stderr_bytes.decode("utf-8", errors="replace").strip(),
            )
            raise RuntimeError(
                f"credential proxy shell bootstrap failed with exit code {result.returncode}"
            )

    def execute(
        self,
        argv: list[str],
        stdin: str | None = None,
        cwd: str | None = None,
        kubeconfig: str | None = None,
    ) -> ExecutionResult:
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(argument, str) for argument in argv)
        ):
            raise ValueError("argv must be a non-empty list of strings")
        executable = argv[0]
        if executable not in self.ALLOWED_EXECUTABLES:
            raise ValueError("executable is not supported by the credential proxy")
        executable_path = self.executables.get(executable)
        if not executable_path:
            raise RuntimeError(f"supported executable is unavailable: {executable}")
        command = [executable_path, *argv[1:]]

        # `get-credentials` is the one command that legitimately authors a
        # kubeconfig, so it is handled separately: it writes, everything else
        # reads.
        if _is_get_credentials(argv):
            return self._execute_get_credentials(command, stdin, cwd, kubeconfig)

        # Two ways in, and both have to be covered or the other is a bypass.
        # `--kubeconfig` predates the KUBECONFIG forward and takes precedence
        # over it in kubectl, so closing only the environment would leave the
        # flag as an open door.
        command = self._reroute_kubeconfig_flags(command)
        kubeconfig_path = self._resolve_kubeconfig(kubeconfig) if kubeconfig else None
        return self._execute(
            command,
            stdin=stdin,
            cwd=cwd,
            kubeconfig_path=kubeconfig_path,
        )

    def execute_internal(
        self, argv: list[str], cwd: str | None = None
    ) -> ExecutionResult:
        """Run a trusted, operator-defined helper that is not agent selectable."""
        return self._execute(argv, cwd=cwd)

    def _within_workspace(self, candidate: Path) -> bool:
        return candidate == self.workspace_dir or self.workspace_dir in candidate.parents

    def _lease_holder(self, candidate: Path) -> Path | None:
        """The nearest ancestor of `candidate` that holds a lease marker."""
        for directory in (candidate, *candidate.parents):
            if not self._within_workspace(directory):
                break
            try:
                if (directory / GIT_LEASE_MARKER).is_file():
                    return directory
            except OSError:
                break
        return None

    def git_lease_violation(self, argv: list[str], cwd: str | None) -> str | None:
        """Why this git command may not run here, or None if it may.

        The pod runs many agents against one PersistentVolumeClaim. Containment
        to `/opt/data` keeps them off the sidecar's filesystem but says nothing
        about keeping them off *each other*, and the shared clone that used to
        sit at the workspace root was a directory every agent wrote in at once.
        Skills now take a lease and get a private clone under it; this is the
        floor that stops a skill which does not from mutating a tree anyway.

        It is a floor and not an ownership check. The client sends argv and a
        working directory — never a caller identity — so the proxy can tell that
        a push is happening inside *some* lease but not whose. Ownership is
        checked by the skill (`gitops_workspace.assert_lease_owner`), which is
        the only layer that knows which lease it holds.
        """
        if not self.require_git_lease:
            return None
        if not argv or Path(argv[0]).name != "git":
            return None
        subcommand, redirects = _git_plan(argv)
        if subcommand not in GIT_MUTATING_SUBCOMMANDS:
            return None

        candidate = Path(cwd).resolve() if cwd else self.workspace_dir
        # `-C` is applied the way git applies it: each one relative to the last.
        for redirect in redirects:
            candidate = (candidate / redirect).resolve()

        if not self._within_workspace(candidate):
            return (
                f"`git {subcommand}` would run in {candidate}, outside the shared "
                "workspace."
            )
        if self._lease_holder(candidate) is None:
            return (
                f"`git {subcommand}` is only allowed inside a leased GitOps "
                f"workspace, and {candidate} is not one (no {GIT_LEASE_MARKER} in "
                "it or any directory above it). Other agents share this volume: "
                "run the skill's workspace step — `audit_report.py start` for a "
                "fleet audit, `submit_suggestion.py prepare` for a suggestion — "
                "and work in the directory it prints."
            )
        return None

    def _workspace_kubeconfig(self, kubeconfig: str) -> Path:
        """Hold a caller-supplied kubeconfig path to the shared workspace.

        Cluster Agent profiles pin themselves to one cluster through this path,
        but the client cannot simply forward its environment: the command
        executes in the sidecar, where the agent must not be able to reach
        credential material. The path is therefore held to the same containment
        rule as `cwd`. Paths elsewhere in the sidecar filesystem are rejected
        rather than silently ignored, so a mistake surfaces as an error instead
        of a command that quietly talks to the wrong cluster.

        A `path1:path2` merge list is refused outright. kubectl would flatten it
        into one view, and there is no sound way to regenerate a merge of
        documents whose contents are never trusted in the first place.
        """
        entries = [entry.strip() for entry in kubeconfig.split(os.pathsep) if entry.strip()]
        if not entries:
            raise ValueError("kubeconfig must not be empty")
        if len(entries) > 1:
            raise ValueError(
                "kubeconfig must name a single file; merged KUBECONFIG lists are not supported"
            )
        candidate = Path(entries[0]).resolve()
        if not self._within_workspace(candidate):
            raise ValueError("kubeconfig is outside the shared workspace")
        return candidate

    def _resolve_kubeconfig(self, kubeconfig: str) -> Path:
        """Turn a caller's kubeconfig path into one the proxy wrote itself.

        The caller's file is treated as a *name*, not as content. Exactly one
        string is taken from it — `current-context` — and that string is only
        accepted if it is a well-formed GKE context name, which is enough to say
        which cluster is wanted. The kubeconfig the command then runs against is
        regenerated by `gcloud container clusters get-credentials` against the
        live GKE API and kept in a directory the agent cannot write.

        So every field that made a caller-supplied kubeconfig dangerous — the
        `exec` stanza, `auth-provider`, `server`, `proxy-url`, `tokenFile`,
        `insecure-skip-tls-verify` — is now written by gcloud rather than by the
        agent. There is no allowlist to keep current and no document to re-check
        at open time, because nothing the agent authored is ever opened.

        What the caller keeps is the ability to *name* a cluster. That is not new
        authority: `get-credentials` is bound by the same IAM the proxy already
        runs under, so it can only name clusters this identity could reach anyway.
        """
        requested = self._workspace_kubeconfig(kubeconfig)
        return self._ensure_managed_kubeconfig(self._target_of(requested))

    def _reroute_kubeconfig_flags(self, command: list[str]) -> list[str]:
        """Point any `--kubeconfig` in argv at the regenerated file.

        kubectl prefers this flag over the environment, and it reaches the
        sidecar untouched — the policy engine matches on argv but has no rule for
        it, and the workspace PVC is mounted here. Left alone it would be the
        simplest way around everything `_resolve_kubeconfig` does.
        """
        rewritten = list(command)
        index = 1
        while index < len(rewritten):
            argument = rewritten[index]
            if argument == "--kubeconfig" and index + 1 < len(rewritten):
                rewritten[index + 1] = str(self._resolve_kubeconfig(rewritten[index + 1]))
                index += 2
                continue
            if argument.startswith("--kubeconfig="):
                resolved = self._resolve_kubeconfig(argument.split("=", 1)[1])
                rewritten[index] = f"--kubeconfig={resolved}"
            index += 1
        return rewritten

    def _target_of(self, requested: Path) -> ClusterTarget:
        """Read the wanted cluster out of the caller's kubeconfig."""
        try:
            if requested.stat().st_size > MAX_KUBECONFIG_BYTES:
                raise ValueError(f"kubeconfig is implausibly large: {requested}")
            text = requested.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise ValueError(f"kubeconfig is unreadable: {requested}") from error
        context = read_current_context(text)
        if not context:
            raise ValueError(f"kubeconfig names no current-context: {requested}")
        target = parse_gke_context(context)
        if target is None:
            raise ValueError(
                f"current-context {context!r} is not a GKE context name"
                " (expected gke_<project>_<location>_<cluster>)"
            )
        return target

    def _managed_kubeconfig(self, target: ClusterTarget) -> Path:
        return self.kubeconfig_dir / f"{target.context_name}.yaml"

    def _ensure_managed_kubeconfig(self, target: ClusterTarget) -> Path:
        """Return the proxy-authored kubeconfig for a cluster, fetching on a miss.

        A miss costs one `get-credentials`. In practice the common paths warm the
        cache themselves: both `cluster_agent_profile.py` and the Platform Agent's
        `switch_kube_context` reach a cluster by running that command first, and
        `_execute_get_credentials` files the result here. This is the cold path —
        a restart, since the state dir is an emptyDir, or a kubeconfig that was
        pinned by some earlier process.
        """
        managed = self._managed_kubeconfig(target)
        with self._kubeconfig_lock:
            if managed.is_file() and managed.stat().st_size > 0:
                return managed
            gcloud = self.executables.get("gcloud")
            if not gcloud:
                raise RuntimeError("gcloud is unavailable; cannot materialise a kubeconfig")
            scratch = self.kubeconfig_dir / f".pending-{uuid.uuid4().hex}.yaml"
            try:
                result = self._execute(
                    [
                        gcloud,
                        "container",
                        "clusters",
                        "get-credentials",
                        target.cluster,
                        f"--location={target.location}",
                        f"--project={target.project}",
                    ],
                    kubeconfig_path=scratch,
                )
                if result.exit_code != 0 or not scratch.is_file():
                    detail = result.stderr.strip() or f"gcloud exited {result.exit_code}"
                    raise ValueError(
                        f"could not obtain credentials for {target.context_name}: {detail[:400]}"
                    )
                os.replace(scratch, managed)
            finally:
                scratch.unlink(missing_ok=True)
        return managed

    def _execute_get_credentials(
        self,
        command: list[str],
        stdin: str | None,
        cwd: str | None,
        kubeconfig: str | None,
    ) -> ExecutionResult:
        """Run the one command that is allowed to author a kubeconfig.

        gcloud writes into the proxy's own directory, never straight to the path
        the caller asked for. The generated file is then filed under the context
        it selects — that read is trustworthy because gcloud, not the agent, just
        wrote it — and copied out to the caller so the workspace still holds the
        visible pin that `cluster_agent_profile.py` records and the Cluster Agent
        preflight stats. That copy is an artefact for the agent to look at; it is
        never what a later command runs against.
        """
        if not kubeconfig:
            # No destination asked for, so gcloud updates the sidecar's own
            # config as it always has. Nothing agent-authored is involved.
            return self._execute(command, stdin=stdin, cwd=cwd)

        requested = self._workspace_kubeconfig(kubeconfig)
        scratch = self.kubeconfig_dir / f".pending-{uuid.uuid4().hex}.yaml"
        try:
            result = self._execute(command, stdin=stdin, cwd=cwd, kubeconfig_path=scratch)
            if result.exit_code == 0 and scratch.is_file():
                generated = scratch.read_text(encoding="utf-8")
                context = read_current_context(generated)
                target = parse_gke_context(context) if context else None
                if target is not None:
                    # Deliberately outside `_kubeconfig_lock`: `os.replace` is
                    # atomic, so a concurrent cache miss for the same cluster
                    # either sees the old file or this one, and at worst does one
                    # redundant fetch. Taking the lock here would serialise every
                    # scaffold behind every cold read for no benefit.
                    os.replace(scratch, self._managed_kubeconfig(target))
                requested.parent.mkdir(parents=True, exist_ok=True)
                requested.write_text(generated, encoding="utf-8")
            return result
        finally:
            scratch.unlink(missing_ok=True)

    def _execute(
        self,
        argv: list[str],
        stdin: str | None = None,
        cwd: str | None = None,
        kubeconfig_path: Path | None = None,
    ) -> ExecutionResult:
        """Run a command. `kubeconfig_path` is already resolved and trusted.

        Callers hand this an absolute path the proxy itself owns; containment and
        regeneration happen in `execute` so that nothing reaching this point is
        still caller-controlled.
        """
        started = time.monotonic()
        timed_out = False
        command_cwd = self.workspace_dir
        if cwd:
            requested_cwd = Path(cwd).resolve()
            if not self._within_workspace(requested_cwd):
                raise ValueError("working directory is outside the shared workspace")
            command_cwd = requested_cwd
        command_environment = self.environment.copy()
        if argv and Path(argv[0]).name == "git":
            command_environment.update(self.git_identity)
        if kubeconfig_path is not None:
            command_environment["KUBECONFIG"] = str(kubeconfig_path)
        process = subprocess.Popen(
            argv,
            cwd=command_cwd,
            env=command_environment,
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
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            stdout_bytes, stderr_bytes = process.communicate()

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
        if self.path == "/v1/github/refresh":
            self._handle_github_refresh()
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
            argv = payload["argv"]
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(argument, str) for argument in argv)
            ):
                raise ValueError("argv must be a non-empty list of strings")
            stdin = payload.get("stdin")
            if stdin is not None and not isinstance(stdin, str):
                raise ValueError("stdin must be a string")
            cwd = payload.get("cwd")
            if cwd is not None and not isinstance(cwd, str):
                raise ValueError("cwd must be a string")
            kubeconfig = payload.get("kubeconfig")
            if kubeconfig is not None and not isinstance(kubeconfig, str):
                raise ValueError("kubeconfig must be a string")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        request_id = str(payload.get("requestId", ""))
        if argv[0] not in CommandExecutor.ALLOWED_EXECUTABLES:
            LOGGER.warning(
                "executable blocked request_id=%s executable=%s",
                request_id,
                argv[0],
            )
            self._json(
                HTTPStatus.FORBIDDEN,
                {
                    "status": "blocked",
                    "code": "SECURITY_POLICY_BLOCKED",
                    "rule": "executable.allowlist",
                    "message": "Executable is not supported by the credential proxy.",
                },
            )
            return
        rule = self.policy.blocked_by(argv)
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

        # Not a policy rule: the policy matches on argv alone, and this refusal
        # turns on the working directory as well.
        violation = self.executor.git_lease_violation(argv, cwd)
        if violation is not None:
            LOGGER.warning(
                "git lease refused request_id=%s cwd=%s", request_id, cwd
            )
            self._json(
                HTTPStatus.FORBIDDEN,
                {
                    "status": "blocked",
                    "code": "SECURITY_POLICY_BLOCKED",
                    "rule": "git.workspace.lease",
                    "message": violation,
                },
            )
            return

        try:
            result = self.executor.execute(
                argv, stdin=stdin, cwd=cwd, kubeconfig=kubeconfig
            )
        except ValueError as exc:
            # Containment rejections (cwd or kubeconfig outside the workspace)
            # are caller errors, not proxy faults. Returning the reason keeps
            # them from reading as an unexplained proxy outage — the agent can
            # correct the path instead of guessing.
            LOGGER.warning(
                "command rejected request_id=%s reason=%s", request_id, exc
            )
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:
            LOGGER.exception(
                "command failed request_id=%s type=%s",
                request_id,
                type(exc).__name__,
            )
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "credential proxy command execution failed"},
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
            },
        )

    def _handle_github_refresh(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > self.max_request_bytes:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(content_length))
            repository = payload["repository"]
            if not is_valid_repository(repository):
                raise ValueError("repository must be owner/name")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        try:
            result = self.executor.execute_internal(
                ["/opt/defaults/scripts/github_token_refresh.py", repository]
            )
        except Exception as exc:
            LOGGER.warning("GitHub credential refresh failed: %s", type(exc).__name__)
            self._json(
                HTTPStatus.BAD_GATEWAY, {"error": "GitHub credential refresh failed"}
            )
            return
        if result.exit_code != 0:
            LOGGER.warning("GitHub credential refresh exited %d", result.exit_code)
            self._json(
                HTTPStatus.BAD_GATEWAY, {"error": "GitHub credential refresh failed"}
            )
            return
        self._json(HTTPStatus.OK, {"status": "refreshed"})

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
            if self.path == "/v1/chat/api":
                resource = payload.get("resource", [])
                arguments = payload.get("arguments", {})
                if not isinstance(resource, list) or not isinstance(arguments, dict):
                    raise ValueError("resource must be a list and arguments an object")
                result = self.chat_relay.api_call(
                    resource,
                    str(payload.get("method", "")),
                    arguments,
                )
                self._json(HTTPStatus.OK, {"response": result})
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
                "Slack relay operation failed path=%s type=%s error=%s",
                self.path,
                type(exc).__name__,
                _slack_error_detail(exc),
            )
            # Carry the whitelisted diagnostic fields back to the agent, not
            # just to this log. slack_sdk raises SlackApiError for an
            # ``ok: false``, so without this the specific cause —
            # channel_not_found, not_in_channel, missing_scope — dies here and
            # the caller sees an indistinguishable "Slack operation failed"
            # for every one of them. slack_relay_patch turns the ``slack`` key
            # back into the SlackApiError the real client would have raised.
            body: dict[str, Any] = {"error": "Slack operation failed"}
            fields = _slack_error_fields(exc)
            if fields:
                body["slack"] = fields
            self._json(HTTPStatus.BAD_GATEWAY, body)

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
    executor = CommandExecutor(
        timeout_seconds=args.timeout_seconds,
        max_output_bytes=args.max_output_bytes,
        state_dir=args.state_dir,
    )
    executor.bootstrap(os.getenv("CREDENTIAL_PROXY_BOOTSTRAP_COMMAND", ""))
    CredentialProxyHandler.executor = executor
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
        def initialize_slack_relay() -> None:
            while CredentialProxyHandler.slack_relay is None:
                try:
                    relay = SlackRelay(
                        slack_bot_tokens,
                        slack_app_token,
                        max_file_bytes=int(
                            os.getenv(
                                "SLACK_RELAY_MAX_FILE_BYTES", str(20 * 1024 * 1024)
                            )
                        ),
                    )
                except Exception as exc:
                    LOGGER.error(
                        "Slack relay initialization failed; retrying type=%s",
                        type(exc).__name__,
                    )
                    time.sleep(30)
                else:
                    CredentialProxyHandler.slack_relay = relay
                    LOGGER.info(
                        "Slack relay enabled workspaces=%d",
                        len(relay.bootstrap()),
                    )

        threading.Thread(target=initialize_slack_relay, daemon=True).start()
    AgentAPIProxyHandler.external_key = os.getenv("API_SERVER_EXTERNAL_KEY", "").strip()
    if not AgentAPIProxyHandler.external_key:
        raise RuntimeError("API_SERVER_EXTERNAL_KEY must be configured")
    AgentAPIProxyHandler.upstream_key = os.getenv(
        "AGENT_API_UPSTREAM_KEY", "cluster-internal-trusted"
    )
    api_server = ThreadingHTTPServer(
        ("0.0.0.0", int(os.getenv("AGENT_API_PROXY_PORT", "8643"))),
        AgentAPIProxyHandler,
    )
    threading.Thread(target=api_server.serve_forever, daemon=True).start()
    LOGGER.info("authenticated PlatformAgent API proxy listening on port 8643")
    if args.unix_socket:
        socket_path = Path(args.unix_socket)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        server = ThreadingUnixHTTPServer(str(socket_path), CredentialProxyHandler)
        LOGGER.info("credential proxy listening on unix socket %s", socket_path)
    else:
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
        "--unix-socket", default=os.getenv("CREDENTIAL_PROXY_UNIX_SOCKET", "")
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
