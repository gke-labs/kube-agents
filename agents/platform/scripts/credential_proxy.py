#!/usr/bin/env python3
"""Credential proxy for restricted credentialed CLI execution."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
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
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import command_policy

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


# Two shapes, because two are what the GitHub refresh helper handles: the
# installation token Minty returns, and the Google OIDC identity token sent to
# authenticate the request to it.
_CREDENTIAL_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
)


def redact_credentials(text: str) -> str:
    """Blank out token-shaped substrings so a subprocess's output can be logged.

    This container is the one place holding credentials and everything it writes
    to stdout leaves the cluster, so the rule is that none of it may be
    credential material (`concepts/observability.md`). Tracing the GitHub
    refresh helper says its stderr already satisfies that -- it never formats
    either token into a message, and the installation token reaches `gh` over
    stdin rather than argv, so it cannot surface in a `CalledProcessError`. The
    gap that argument does not close is the broker's own error body, which this
    repository does not own: a Minty that echoed the request's `X-OIDC-Token`
    header back in a 4xx would put a credential in a string we are about to log.
    Match on shape so that stops being an argument about someone else's service.

    Deliberately not a general-purpose redactor. Two others already exist
    (`AuditRedactor`, and `redact_secrets` in the fleet-audit skill) and both
    cover more shapes; neither belongs in this container, which must not import
    from the Hermes plugin tree. Consolidating the three is separate work.
    """
    return _CREDENTIAL_SHAPES.sub("[REDACTED]", text)


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """HTTP server over a private Unix socket used behind Envoy."""

    daemon_threads = True


# ---------------------------------------------------------------------------
# Who is calling
#
# For as long as the broker ran as a sidecar, nothing on this path
# authenticated anything.  What kept the credentials safe was geometry: Envoy
# bound 127.0.0.1, so only the Pod could reach it, and the socket behind Envoy
# was 0600 in an emptyDir that only this container mounted.  Both of those
# properties are properties of *sharing a network namespace*, and both
# evaporate the moment the broker becomes its own Pod.  So a split needs an
# answer to "who is calling", and this is it.
#
# The answer is a Kubernetes ServiceAccount token, projected into the caller
# with a dedicated audience, presented as a bearer token, and verified here
# with a TokenReview against the API server.  Three reasons for that shape
# rather than mTLS or a Unix socket per caller:
#
#   * It needs no PKI.  This repository has no cert-manager for workloads, no
#     service mesh and no SPIFFE; mTLS would mean standing all of that up, or
#     minting certificates in the operator, before a single request could be
#     authenticated.  The projected token already exists in the cluster.
#   * It needs no Envoy filter.  The Envoy config is baked into the image and
#     loaded by absolute path, so an ext_authz or JWT filter is an image
#     rebuild that cannot vary per agent.  A bearer header rides through the
#     router filter untouched and is checked here, in code the operator can
#     configure with an environment variable.
#   * It forecloses nothing.  mTLS is a transport underneath this, not a
#     replacement for it: adding a client certificate later leaves the request
#     shape, the handler and this verifier intact, and gives ``Principal`` a
#     second, stronger source for the same field.  gRPC carries bearer
#     credentials in exactly the same ``authorization`` metadata key, so a
#     later move to gRPC ports the identity model verbatim.  What it does
#     foreclose is a Unix socket per caller — but a Unix socket needs a shared
#     filesystem, which is the one thing splitting the Pods takes away.
#
# What it is honestly *not*: encryption.  The token crosses the cluster
# network in cleartext, exactly as the github-token-minter call already does
# (see github_token_refresh.py).  Anyone who can observe pod-to-pod traffic in
# the namespace can replay it until it expires.  mTLS closes that, and the
# NetworkPolicy work in the next task narrows who can open the connection at
# all.  Neither is done here.
# ---------------------------------------------------------------------------

DEFAULT_CREDENTIAL_PROXY_AUDIENCE = "kubeagents-credential-proxy"


class AuthenticationError(Exception):
    """The caller could not be identified.

    The message is for this process's log. It is deliberately never returned
    to the client, which gets an undifferentiated 401 — telling an unidentified
    caller *why* it failed tells it how to succeed.
    """


@dataclass(frozen=True)
class Principal:
    """Who the broker believes is on the other end of a request.

    ``workload`` is what the transport can prove today: the Kubernetes identity
    of the ServiceAccount whose projected token authenticated the connection.

    Read that literally — it is **per-ServiceAccount**, and weaker than
    per-Pod. The agent Pod and the broker Pod run as the same ServiceAccount,
    because the Workload Identity IAM binding names it and giving the agent one
    of its own would take the broker's cloud credentials with it. So this field
    excludes every other workload in the cluster and nothing finer: it cannot
    distinguish the agent Pod from the broker Pod, let alone one session inside
    the agent Pod from another. It answers "which ServiceAccount", not "which
    Pod" and not "on whose behalf".

    ``caller`` is where a per-caller identity would go, and it is deliberately
    a field on the object rather than a second parameter threaded through the
    handler. When that model is settled, the agent obtains a capability token
    scoped to one session — *attenuating*, so it can never name more authority
    than the workload token it was exchanged for — and sends it alongside the
    workload token. This class grows one more verification step that populates
    ``caller`` from it, ``authenticate`` keeps its signature, and the policy
    layer downstream reads ``principal.caller`` where it reads
    ``principal.workload`` today. Nothing about the request shape, the
    handler, or the operator's rendering has to change again.
    ``caller`` stays None until then. What must hold in the meantime is that
    neither field is ever derived from the request body — from ``argv``, from
    ``cwd``, from anything a model produced. Both come from a token the API
    server verified.
    """

    workload: str
    uid: str = ""
    groups: tuple[str, ...] = ()
    caller: str | None = None

    def describe(self) -> str:
        if self.caller:
            return f"{self.workload} (caller {self.caller})"
        return self.workload


class NullAuthenticator:
    """Accept every caller. Only sound behind a private Unix socket.

    ``serve`` refuses to start this on a TCP listener, because on a TCP
    listener "no authentication" means "the credentials belong to whoever
    reaches the port".
    """

    authenticates = False

    def authenticate(self, headers: Any) -> Principal:  # noqa: ARG002
        return Principal(workload="unauthenticated")


@dataclass
class _CacheEntry:
    expires_at: float
    principal: Principal


class ServiceAccountAuthenticator:
    """Verify a projected ServiceAccount token with a Kubernetes TokenReview.

    The audience is the whole point. A token projected with audience
    ``kubeagents-credential-proxy`` is rejected by every other API-server-aware
    service in the cluster, and the API server refuses to authenticate it here
    unless the audience matches — so a token stolen from the agent cannot be
    replayed against the Kubernetes API, and a token minted for anything else
    cannot be replayed against the broker.
    """

    authenticates = True

    def __init__(
        self,
        audience: str,
        allowed_callers: frozenset[str],
        api_host: str,
        api_port: str,
        ca_file: str,
        token_file: str,
        timeout_seconds: float = 10.0,
        cache_seconds: float = 60.0,
    ) -> None:
        if not audience:
            raise ValueError("an audience is required to authenticate callers")
        if not allowed_callers:
            raise ValueError("at least one allowed caller is required")
        if not api_host:
            raise ValueError("the Kubernetes API server address is not configured")
        self.audience = audience
        self.allowed_callers = allowed_callers
        self.api_host = api_host
        self.api_port = api_port
        self.ca_file = ca_file
        self.token_file = token_file
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_lock = threading.Lock()

    def authenticate(self, headers: Any) -> Principal:
        header = headers.get("Authorization", "") or ""
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError("no bearer token was presented")
        token = token.strip()

        cache_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        principal = self._review(token)
        self._remember(cache_key, principal)
        return principal

    def _cached(self, key: str) -> Principal | None:
        now = time.monotonic()
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                # Expired entries are dropped rather than served, so revoking a
                # ServiceAccount takes effect within cache_seconds rather than
                # for the lifetime of the process.
                del self._cache[key]
                return None
            return entry.principal

    def _remember(self, key: str, principal: Principal) -> None:
        now = time.monotonic()
        with self._cache_lock:
            # Only successful reviews are cached, so a rejected token costs the
            # API server one round trip every time it is retried.
            self._cache = {
                cached_key: entry
                for cached_key, entry in self._cache.items()
                if entry.expires_at > now
            }
            self._cache[key] = _CacheEntry(now + self.cache_seconds, principal)

    def _own_token(self) -> str:
        try:
            return Path(self.token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AuthenticationError(
                f"this pod's own API server token is unreadable: {type(exc).__name__}"
            ) from exc

    def _review(self, token: str) -> Principal:
        body = json.dumps(
            {
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {"token": token, "audiences": [self.audience]},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"https://{self.api_host}:{self.api_port}/apis/authentication.k8s.io/v1/tokenreviews",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self._own_token()}",
            },
            method="POST",
        )
        try:
            # Inside the try: a missing or unreadable ca.crt raises
            # FileNotFoundError here, and an OSError escaping this method is
            # not an AuthenticationError — it would reach
            # socketserver.handle_error as a traceback and a dropped
            # connection, where the caller deserves a 401.
            context = ssl.create_default_context(cafile=self.ca_file or None)
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds, context=context
            ) as response:
                review = json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # A TokenReview that cannot be completed is a rejection, not an
            # allow. An API server outage must not turn into an open broker.
            raise AuthenticationError(
                f"TokenReview could not be completed: {type(exc).__name__}"
            ) from exc
        return self._principal_from(review)

    def _principal_from(self, review: Any) -> Principal:
        status = review.get("status") if isinstance(review, dict) else None
        if not isinstance(status, dict):
            raise AuthenticationError("TokenReview returned no status")
        if status.get("error"):
            raise AuthenticationError("TokenReview reported an error")
        if status.get("authenticated") is not True:
            raise AuthenticationError("the presented token is not authenticated")
        audiences = status.get("audiences") or []
        if self.audience not in audiences:
            # The API server echoes the audiences it actually validated. A token
            # it authenticated for some other audience is not for us.
            raise AuthenticationError("the presented token is for another audience")
        user = status.get("user") or {}
        username = user.get("username") or ""
        if username not in self.allowed_callers:
            raise AuthenticationError("the authenticated caller is not permitted")
        groups = user.get("groups") or []
        return Principal(
            workload=username,
            uid=str(user.get("uid") or ""),
            groups=tuple(str(group) for group in groups if isinstance(group, str)),
        )


def build_authenticator() -> NullAuthenticator | ServiceAccountAuthenticator:
    """Build the caller authenticator the environment asks for.

    ``none`` is the default so that the sidecar deployment, where the socket
    and the loopback listener are the access control, is unchanged. ``serve``
    is what makes that default safe: it refuses to serve on TCP with it.
    """
    mode = os.getenv("CREDENTIAL_PROXY_AUTH_MODE", "none").strip().lower()
    if mode in {"", "none"}:
        return NullAuthenticator()
    if mode != "serviceaccount":
        raise RuntimeError(
            f"unsupported CREDENTIAL_PROXY_AUTH_MODE {mode!r}; expected 'none' or 'serviceaccount'"
        )
    allowed = frozenset(
        caller.strip()
        for caller in os.getenv("CREDENTIAL_PROXY_ALLOWED_CALLERS", "").split(",")
        if caller.strip()
    )
    if not allowed:
        raise RuntimeError(
            "CREDENTIAL_PROXY_AUTH_MODE=serviceaccount requires "
            "CREDENTIAL_PROXY_ALLOWED_CALLERS to name at least one ServiceAccount"
        )
    return ServiceAccountAuthenticator(
        audience=os.getenv(
            "CREDENTIAL_PROXY_AUDIENCE", DEFAULT_CREDENTIAL_PROXY_AUDIENCE
        ).strip(),
        allowed_callers=allowed,
        api_host=os.getenv("KUBERNETES_SERVICE_HOST", "").strip(),
        api_port=os.getenv("KUBERNETES_SERVICE_PORT", "443").strip() or "443",
        ca_file=os.getenv(
            "CREDENTIAL_PROXY_KUBE_CA_FILE",
            "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        ).strip(),
        token_file=os.getenv(
            "CREDENTIAL_PROXY_KUBE_TOKEN_FILE",
            "/var/run/secrets/kubernetes.io/serviceaccount/token",
        ).strip(),
    )


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
        # BaseHTTPRequestHandler hands the raw request line through here, so
        # every argument is caller text and it is logged before any
        # authentication runs. See CredentialProxyHandler.log_message.
        LOGGER.info("agent-api " + message, *_sanitized_log_args(args))


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
        self._credentials = credentials
        # build() hands the discovery resource a single AuthorizedHttp, and so
        # a single httplib2.Http holding a single TLS socket. httplib2 is not
        # thread safe, and this proxy serves a thread per connection: two
        # concurrent api_call threads interleaving records on that one socket
        # surface as ssl.SSLError, which the handler answers 502. Each call
        # therefore checks out its own transport. A pool rather than a
        # thread-local because request threads are per-connection and the
        # agent-side client opens a connection per call, so thread-locals would
        # mean a fresh TLS handshake to chat.googleapis.com every time.
        self._http_pool: queue.LifoQueue = queue.LifoQueue()
        self._http_pool_size = int(os.getenv("GOOGLE_CHAT_HTTP_POOL_SIZE", "8"))
        self.num_retries = int(os.getenv("GOOGLE_CHAT_API_NUM_RETRIES", "3"))
        self._receipts: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _build_http(self) -> Any:
        import google_auth_httplib2
        from googleapiclient.http import build_http

        return google_auth_httplib2.AuthorizedHttp(
            self._credentials, http=build_http()
        )

    @contextlib.contextmanager
    def _checkout_http(self) -> Any:
        """Lend one authorized transport to a single caller at a time."""
        try:
            http = self._http_pool.get_nowait()
        except queue.Empty:
            http = self._build_http()
        yield http
        # Deliberately not a finally: a transport whose call raised may have
        # failed mid-record, and handing that socket to the next caller would
        # spread one failure across every call after it. It is dropped, and the
        # next checkout builds a clean one.
        if self._http_pool.qsize() < self._http_pool_size:
            self._http_pool.put(http)

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
        # num_retries opts into googleapiclient's own jittered backoff, which
        # covers ssl.SSLError, socket timeouts and 5xx. Left at its default of
        # 0 the library attempts the call exactly once. Every Chat method is
        # retried, messages.create included: a duplicate message is a better
        # outcome than a reply the user never sees, and the window in which a
        # retried create duplicates is narrow (the request reached Google and
        # the failure landed on the response).
        with self._checkout_http() as http:
            return operation.execute(http=http, num_retries=self.num_retries)


def _chat_error_fields(exc: Exception) -> dict[str, Any] | None:
    """Return the whitelisted diagnostics a Google Chat API error carried.

    ``None`` means the failure was not an API rejection at all — a transport
    fault, most often — and the caller has nothing to relay beyond the
    exception type. Only the status line crosses this boundary. An HttpError
    stringifies to a message embedding the request URI, and that URI names the
    space and carries the query the relay's own credential authorized, so it is
    never logged nor returned to the agent.
    """
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    try:
        fields: dict[str, Any] = {"status": int(status)}  # type: ignore[arg-type]
    except (TypeError, ValueError):
        # No parseable status: this runs inside an exception handler, so a
        # second exception here would mask the first.
        return None
    reason = getattr(response, "reason", None)
    if reason:
        fields["reason"] = str(reason)
    return fields


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
        # Normalised once, not once per rule. This was inside the generator, so
        # a thirteen-rule policy rebuilt the match text thirteen times for every
        # brokered command -- pre-existing rather than anything the cluster fix
        # introduced, but it multiplied that fix's worst case by thirteen, which
        # is how it was noticed.
        match_text = policy_match_text(argv)
        return next(
            (rule for rule in self.rules if rule.pattern.search(match_text)),
            None,
        )


# Flags whose value is prose the agent wrote, not part of the command. Their
# values are dropped before the rules see the argv.
#
# Every rule in the shipped policy is a word search across the whole joined
# command -- `\bgh\b(?:\s+\S+)*?\s+pr\b(?:\s+\S+)*?\s+merge\b` and its
# siblings -- and shlex.join leaves the spaces inside a quoted argument as real
# spaces. A body is therefore searched exactly like a subcommand path. The
# submit-suggestion skill instructs the agent to close every pull request body
# with "Please review the code diffs and merge this PR to trigger the GitOps
# CI/CD rollout!", so `gh pr create --body "<that>"` contained a `pr` token and
# a later `merge` token and was refused by github.merge: the product's own
# GitOps suggestion, blocked at the broker. The same shape reaches the older
# rules -- a body mentioning `gh auth token` trips github.token-disclosure --
# so this is a defect in how matching works rather than in the new rules.
#
# Values only. The flag names stay, because a rule may legitimately key on the
# presence of one.
_FREE_TEXT_FLAGS = frozenset(
    {
        "--body", "-b", "--title", "-t", "--notes", "--message", "-m",
        "--description", "--comment",
    }
)


# The single-dash shorthands a shipped rule keys on, split by whether the rule
# needs a value beside the flag. Only these need a dash kept when they are
# buried in a cluster, so this is the whole table rather than pflag's arity for
# four upstream CLIs.
#
# `github.api-mutation` is the only rule that reads a value: `-X PUT` has to be
# adjacent. Everything else keys on the flag being present at all, which is why
# the split is worth making -- see `_cluster_readings`, where it is the
# difference between one remainder and one per letter.
#
# It is a copy of something that lives in the operator, so it is pinned:
# `test_every_shorthand_a_rule_keys_on_is_covered` reads the shipped policy and
# fails if a rule keys on a shorthand missing here. Add the rule, run the
# tests, and that test tells you to come back.
_VALUE_TAKING_SHORTHANDS = frozenset({"-X", "-f", "-F"})
_KEYED_SHORTHANDS = _VALUE_TAKING_SHORTHANDS | frozenset({"-t", "-a"})


def _cluster_readings(token: str) -> list[str]:
    """The keyed shorthands buried inside a single-dash cluster, re-dashed.

    pflag accepts a boolean shorthand and a value-taking one in the same token:
    `gh api -iX PUT` is `--include --method PUT`, because `parseSingleShortArg`
    consumes `-i`, sets the remainder as the shorts still to read, and re-enters
    the loop. The splitter above only ever takes the *first* shorthand off, so
    that argv reaches the rules as `-i X ...` -- with the `-X` that
    `github.api-mutation` matches on reduced to a bare letter. The merge went
    through. `gh auth status -at` is the same shape against
    `github.token-disclosure`, and that one returns the installation token to
    the agent.

    So each subsequent letter that a rule keys on is re-emitted with its dash,
    followed by whatever is left of the token, which is where pflag would take
    that shorthand's value from.

    The walk stops at the first non-letter, because a cluster of shorthands is
    letters by definition and everything from a non-letter on is somebody's
    value: without that, `-nkube-system` would emit a `-t` off `system` and a
    `gh auth status` somewhere in the same command would be refused for it.

    Two bounds, because the argv is chosen by the sandbox and the sidecar holds
    every agent's credentials. A keyed flag is emitted **once**, since the rules
    ask whether it is present and a millionth `-a` answers nothing a first one
    did not. And a remainder is emitted **once at most**, at the first
    value-taking shorthand, which is also where pflag stops reading the cluster.
    Emitting a fresh copy of the suffix per keyed letter made this quadratic:
    `["gh", "-" + "a" * 1000000]` fits inside `max_request_bytes`, reaches here
    because `gh` is an allowed executable, and exhausted the container's 2Gi on
    a single request. This walk allocates one slice, at the break.
    """
    readings: list[str] = []
    seen: set[str] = set()
    # From 2: `token[0]` is the dash and `token[1]` is the shorthand the caller
    # has already split off. Indexed rather than sliced -- a slice per letter is
    # the quadratic this function was rewritten to lose.
    for position in range(2, len(token)):
        letter = token[position]
        if not letter.isalpha():
            break
        flag = f"-{letter}"
        if flag in _FREE_TEXT_FLAGS:
            # Prose from here on, dropped as the detached spelling drops it.
            if flag not in seen:
                readings.append(flag)
            break
        if flag not in _KEYED_SHORTHANDS:
            continue
        if flag not in seen:
            seen.add(flag)
            readings.append(flag)
        if flag in _VALUE_TAKING_SHORTHANDS:
            remainder = token[position + 1 :].lstrip("=")
            if remainder:
                readings.append(remainder)
            break
    return readings


def policy_match_text(argv: list[str]) -> str:
    """The command as the policy rules should read it.

    Two normalisations, both of which the rules would otherwise get wrong in
    opposite directions.

    Free-text flag values are dropped, so prose the agent wrote is not searched
    for command tokens. Without this the denylist refuses the agent's own pull
    requests -- a false positive that takes the product down rather than an
    attacker.

    Attached shorthand values are split apart. gh, kubectl and gcloud are all
    Cobra/pflag, which accepts a shorthand's value with no separator, so
    `gh api -XPUT repos/o/r/pulls/1/merge` is `-X PUT` and performs the merge
    that `github.api-mutation` exists to refuse -- while matching neither
    branch of it, because there is no whitespace or `=` after `-X`. Splitting
    `-XPUT` into `-X PUT` closes that without the rule having to enumerate
    spellings. `-fmerge_method=squash` becomes `-f merge_method=squash` for the
    same reason.

    Splitting the first shorthand off is deliberately unconditional rather than
    gated on a table of value-taking shorthands: emitting `-A w` for the
    boolean cluster `-Aw` costs nothing, since no rule keys on a bare letter,
    and a table would be one more thing to keep in step with four upstream
    CLIs.

    That reasoning holds for a cluster of booleans and fails for a cluster
    whose *later* member is the one a rule keys on, which is why
    `_KEYED_SHORTHANDS` exists -- see `_cluster_readings`.
    """
    tokens: list[str] = []
    skip_next = False
    for index, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        name, separator, _ = token.partition("=")
        if name in _FREE_TEXT_FLAGS:
            # `--body=<prose>` carries its value in the same token; `--body
            # <prose>` in the next one.
            #
            # Never swallow a token that looks like a flag. This set is applied
            # without knowing which subcommand is running, and a name in it is
            # not always value-taking: `--comment` takes prose on `gh issue
            # close` and is a boolean on `gh pr review`, where the next token is
            # the next flag. Swallowing it there would drop `--approve` out of
            # `gh pr review --comment --approve 1` and hide it from
            # github.assent. gh happens to refuse that particular argv itself
            # ("need exactly one of --approve, --request-changes, or
            # --comment"), so it is not an escape today -- but it is one flag's
            # arity away from being one, and the guard costs nothing. The only
            # thing it gives up is prose beginning with a dash, which stays in
            # the match text and can at worst cause a visible refusal.
            following = argv[index + 1] if index + 1 < len(argv) else ""
            skip_next = not separator and not following.startswith("-")
            tokens.append(name)
            continue
        if (
            len(token) > 2
            and token.startswith("-")
            and not token.startswith("--")
        ):
            # An attached free-text shorthand carries prose in the same token:
            # `-bPlease merge this PR` would otherwise be re-emitted as match
            # text by the splitter below and trip github.merge, which is the
            # false refusal this whole function exists to stop. Drop the value
            # and keep the flag, as the detached spelling does.
            if token[:2] in _FREE_TEXT_FLAGS:
                tokens.append(token[:2])
                continue
            tokens.extend([token[:2], token[2:].lstrip("=")])
            tokens.extend(_cluster_readings(token))
            continue
        tokens.append(token)
    return shlex.join(tokens)


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

# Directory `core.hooksPath` is pinned to. It lives under the state dir, which
# is a sidecar-only emptyDir, and is created empty and mode 0500 at startup.
# A hook only runs if git finds an executable file of the right name in the
# hooks directory, so an empty directory the agent cannot write is a hook
# directory that can never fire. Pinning to a *nonexistent* path also works
# today, but it would rest on that path staying absent, which is a weaker
# claim than "exists, empty, and not writable by the agent".
GIT_HOOKS_DISABLED_DIR = "git-hooks-disabled"

# Broker-owned git trees, under the state dir rather than the shared workspace.
CONTENT_WORKSPACE_DIR = "content-workspaces"

# Config keys forced onto every git invocation, as the `GIT_CONFIG_COUNT`
# layer. That layer outranks system, global and repo-local config, which is
# the point: the agent owns the working tree, so `.git/config` is a file it
# can write, and every key below turns a string in that file into a command
# the credential holder executes.
#
#   core.hooksPath   `.git/hooks/pre-commit` is executed by `git commit`, and
#                    `git commit` is a verb the skills issue nine times.
#                    Pinning the path also neutralises hooks installed into a
#                    fresh clone through `init.templateDir`.
#   core.fsmonitor   run by `git status`, i.e. by a read verb.
#
# **This list is known-incomplete and is not a boundary.** `filter.<name>.smudge`
# and `alias.<name>` reach the same place and cannot be pinned, because the key
# contains an arbitrary name and there is nothing to enumerate. They are open,
# they are recorded as open, and the fix for the class is to stop executing
# subprocesses in a directory the agent controls at all (content-passing).
# What this buys is blast radius, not closure — do not read it as more.
#
# Only keys whose "off" value is a *working* value belong here. `diff.external`
# was pinned to "" in an earlier revision and reverted: git does not read an
# empty value as "no external diff", it tries to execute the empty string, so
# every `git diff` died with `fatal: external diff died` — a read verb broken
# by the hardening, reported in a way that reads as a broken image rather than
# a refusal. There is no value that turns it off, and since anyone who can
# write the `.git/config` that sets it can equally use the two unpinnable keys
# above, the pin cost a working verb and removed no capability from an
# attacker who has that write.
#   commit.gpgsign   turns `git commit -m` — the argv the skills already send —
#   gpg.program      into a run of whatever `gpg.program` names. Signing is a
#   tag.gpgSign      program git executes, and both halves are settable from
#                    `.git/config`. Note the failure shape: the payload runs and
#                    *then* git exits 128, so a test asserting only a non-zero
#                    exit would have called this working.
#   help.autocorrect Without it the subcommand refusal list below is not a
#                    control at all: set it in `.git/config` and `git bisct run`
#                    resolves to `bisect run`, matching nothing on a list that
#                    compares whole tokens. Pinned to 0 — never autocorrect —
#                    which is also git's own default.
GIT_FORCED_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.fsmonitor", "false"),
    ("commit.gpgsign", "false"),
    ("tag.gpgSign", "false"),
    ("gpg.program", "false"),
    # `gpg.program` only covers the openpgp format. `gpg.format` is settable
    # from the repository's own config, and each format reads its own program
    # key, so `[gpg] format = ssh` walks straight past the pin above. Measured
    # against git 2.55 under this environment: with `gpg.format=ssh` and
    # `gpg.ssh.program=<payload>` set repository-locally, `git commit -S` and
    # `git tag -s` both execute the payload. `gpg.ssh.defaultKeyCommand` does
    # the same with no `user.signingkey` at all, and `x509` has its own
    # `gpg.x509.program`. `-S`/`-s` are not refused in argv and there is no
    # reason to refuse them, so the pin is the control.
    #
    # Unlike the unpinnable keys, this set is closed: git defines exactly three
    # signature formats and each names its program in a fixed key. Verified
    # that the pins close all four spellings and that an unsigned
    # `git commit -m` is untouched. Nothing under `agents/`, `k8s-operator/`
    # or `scripts/` signs anything.
    #
    # The trigger is the `-S`/`-s` flag, and `commit` and `tag` are both
    # lease-gated -- which is a speed bump rather than a barrier, since the
    # agent creates its own leases. There is no lease-free read route in:
    # `log --show-signature`, `show --show-signature`, `verify-commit` and the
    # `%G?`/`%GS` formats were all tried against a commit carrying a crafted
    # SSH signature header and none of them ran the configured program.
    ("gpg.ssh.program", "false"),
    ("gpg.ssh.defaultKeyCommand", "false"),
    ("gpg.x509.program", "false"),
    ("help.autocorrect", "0"),
)


def _git_forced_config_environment(pairs: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """Render config pins as the `GIT_CONFIG_COUNT` environment layer.

    git reads `GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>` for n in
    `[0, GIT_CONFIG_COUNT)`. The two failure directions are not symmetric,
    which is why the count is derived rather than written down: a count higher
    than the pairs supplied is a hard failure on every git command (`error:
    missing config key GIT_CONFIG_KEY_1`, exit 128), and a count *lower*
    silently ignores the tail, disarming the last pin with nothing to see.
    Building both from one sequence is what keeps them in step.
    """
    environment = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for index, (key, value) in enumerate(pairs):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


# git global options that override something the proxy decided, refused in
# argv. Each value says which control the flag defeats, because a refusal that
# does not say what it protected gets read as an arbitrary restriction and
# argued away.
#
# The first three are config and code injection, and they are the backup to the
# environment hardening — except for hooks, where `-c` beats the
# `GIT_CONFIG_COUNT` layer outright and this is the only control there is.
#
# The last two are containment, not configuration. `_execute` refuses a `cwd`
# outside the shared workspace and `git_lease_violation` resolves `cwd` plus
# every `-C`, but neither looks at `--git-dir`, so `git --git-dir=<elsewhere>
# --work-tree=<elsewhere> commit` runs against a repository on the sidecar's
# own filesystem from inside a perfectly valid lease. Verified: it reads and it
# writes. `-C` stays allowed — the containment check already follows it, and
# the skills use it.
_GIT_REFUSED_ARGUMENTS = {
    "-c": "sets configuration that outranks the proxy's own",
    "--config-env": "sets configuration that outranks the proxy's own",
    "--exec-path": "chooses where git looks for the program to run",
    "--git-dir": "points git at a repository outside the shared workspace",
    "--work-tree": "points git at a tree outside the shared workspace",
    # `git config --global` writes the very file GIT_CONFIG_GLOBAL pins, and
    # `config` is not a mutating verb so it needs no lease. Demonstrated: the
    # agent writes `alias.zz = !<payload>` into the broker's own global config
    # through the proxy, then runs `git zz`. Everything the forced-config layer
    # does not pin is agent-settable at global scope this way, from outside any
    # repository. Shipped code sets identity repository-locally and says so
    # (`gitops_workspace.py:594`); `gh auth setup-git` writes the global file
    # from inside the broker, which does not pass through this gate.
    "--global": "writes git configuration the proxy pins for every command",
    "--system": "writes git configuration the proxy pins for every command",
    # `git config --file <path>` writes the same file `--global` names, just
    # spelled explicitly, and the path is not a secret — `git config --list
    # --show-origin` prints it. Refusing `--global` without this closed the
    # front door and left the side one open. It is also an arbitrary INI write
    # to any path: the containment check inspects `cwd`, not this.
    "--file": "writes a git configuration file the proxy does not control",
    # Flags that name a command directly, on a subcommand that is otherwise
    # ordinary. These are the same category as the refused subcommands below —
    # git running a string the caller chose — but they hide on verbs the
    # product has no reason to refuse outright, so the flag is what gets
    # refused rather than the verb.
    #
    #   --exec/-x   `git rebase -x <cmd> HEAD~1` runs <cmd> once per commit.
    #               `rebase` *is* in GIT_MUTATING_SUBCOMMANDS, so it needs a
    #               lease — which is not a barrier, since the agent creates its
    #               own leases. Demonstrated through the executor, exit 0.
    #   -O          `git grep -O<cmd>` runs <cmd> as the pager over the
    #               matches. `grep` is a *read* verb, so unlike rebase this one
    #               needs no lease and no file on the volume: one call, and the
    #               value is attached to the flag rather than separated, which
    #               is why the matcher below has to handle the attached form.
    #   --trailer   `git commit -m msg --trailer <name>:<value>` runs
    #               `trailer.<name>.cmd` to compute the value, so the payload
    #               lands on `commit` — the argv the skills already send, and
    #               the one the design doc calls reachable with no unusual
    #               argument at all. The key's arbitrary name puts it out of
    #               reach of the pins. Measured under the pinned environment
    #               against git 2.55: `git config trailer.zz.cmd 'id #'` then
    #               `git commit -m msg --trailer zz:v` writes the credential
    #               container's `uid=` into the commit message. It has no short
    #               form on either subcommand that accepts it.
    #   --help      `git <any-verb> --help` is not a usage message: it is
    #               dispatched to the same viewer `git help` uses, so it runs
    #               `man.<man.viewer>.cmd` through a shell. Refusing the `help`
    #               subcommand does not touch it, because the verb in argv is
    #               `status`. Measured under the pinned environment against git
    #               2.55, with `man.viewer`/`man.evil.cmd` set repository-locally:
    #               `git commit --help`, `git status --help`, `git version --help`
    #               and `git log --help` all execute the configured command. The
    #               `status` spelling is the cheapest path in this file — a read
    #               verb, so no lease is taken anywhere in the sequence, and
    #               `status` is squarely on the shipped path.
    #
    #               `-h` is NOT refused and must not be: git answers it from the
    #               subcommand's own option table and prints usage without
    #               dispatching to a viewer. Verified — `git status -h` prints
    #               `usage: git status ...` with the payload configured.
    #               `--help` also takes no abbreviation (`git status --hel` is
    #               `error: unknown option`), so this one literal entry is the
    #               whole closure.
    #
    # `-x` and `-O` are refused wherever they appear, so `git clean -x` and
    # `git cherry-pick -x` are refused too. Neither is in shipped code.
    "--exec": "runs a command the caller names, once per commit",
    "-x": "runs a command the caller names, once per commit",
    "--open-files-in-pager": "runs a command the caller names over the matches",
    "-O": "runs a command the caller names over the matches",
    "--help": "runs the caller-named viewer git help would run",
    "--trailer": "runs a command the caller names to compute a trailer value",
    # Programs git runs on the far side of a transport. Blocked today only by
    # GIT_ALLOW_PROTOCOL refusing `file` — the paired control fires as soon as
    # the allowlist is widened — so these are here to make that widening safe
    # rather than because they are reachable now.
    # Their short forms are NOT here and this is the one deliberate gap in the
    # list. `-u` is `--upload-pack` on `git clone` only; on other verbs the
    # same two characters mean `--set-upstream` (`push`), `--update` (`add`)
    # and `--update-head-ok` (`fetch`). No shipped skill issues any of them
    # today — the pushes on file are `-f` and `--force-with-lease` — but this
    # list is matched across the whole argv, so refusing `-u` would refuse all
    # four spellings on every verb, to close a vector the protocol allowlist
    # already holds shut. That trade is not worth making blind. The
    # consequence is precise — widen GIT_ALLOW_PROTOCOL to `file` and `clone -u`
    # is arbitrary code execution again even though `--upload-pack` is refused.
    # Do not widen it without revisiting this.
    "--upload-pack": "names a program git runs for the remote end of a fetch",
    "--receive-pack": "names a program git runs for the remote end of a push",
}

# Refused short options, matched anywhere inside a single-dash token. git lets
# a short option carry its value attached (`-O/opt/data/payload`) and lets
# several cluster into one argument (`-iO/opt/data/payload`, `-fx<cmd>`), so
# matching the whole token against `-O` catches only the tidiest spelling of
# the attack — `git grep -iO<cmd>` is one byte longer and was demonstrated
# executing past a matcher that only handled the attached form.
#
# Any single-dash token containing one of these letters is refused, without
# working out which letter consumes the value. Working that out means knowing
# each subcommand's option table, and this file has already been wrong once
# about agreeing with git's parser. The over-refusal is real but empty: the
# only clustered short option in shipped git argv is `clean -fdq`
# (`gitops_workspace.py:548`), and no shipped call attaches a value to a short
# one. Checked against the tree, not against another comment — the first draft
# of this note also claimed `git rm -rf`, which nothing issues.
_GIT_REFUSED_SHORT = frozenset("cxO")

# Short options whose meaning depends on the subcommand, refused only when that
# subcommand appears in the argv. `git config -f <path>` is `--file`, but `-f`
# on every other verb is `--force`, which the skills issue (`clean -fdq`,
# `push -f`). Scoping by "the subcommand token is present anywhere" is coarse
# on purpose — it does not require deciding where the options end, only that a
# `git clean -f` whose pathspec happens to be the word `config` is refused.
_GIT_REFUSED_SHORT_FOR_SUBCOMMAND = {
    "config": (frozenset("f"), "writes a git configuration file the proxy does not control"),
}

# Subcommands whose entire purpose is to run a command the caller names. None
# needs a config file, a shared-volume write or a lease, and none is in
# `GIT_MUTATING_SUBCOMMANDS`. Demonstrated through the proxy from inside a
# valid lease: `git bisect start HEAD HEAD~1` then `git bisect run <payload>`
# executes <payload> in the credential container, as do
# `filter-branch --tree-filter` and `send-email --smtp-server=<path>`.
#
# **This is a denylist over a set that is not closed, and it is the weakest
# thing in this file.** git keeps a command in configuration for `difftool`,
# `mergetool`, `web--browse`, `instaweb`, `help`, and the `p4`/`svn`
# bridges, and a new one can arrive in any release. The structurally correct
# fix is to allowlist the ~20 subcommands the product actually issues and fail
# closed on the rest, which is a change to the denylist-not-allowlist decision
# recorded above `GIT_MUTATING_SUBCOMMANDS` — that decision weighed an
# unknown *read* verb failing closed against a concurrency race, and was not
# weighing it against arbitrary code execution. Revisit it with that evidence
# rather than treating this list as sufficient.
_GIT_REFUSED_SUBCOMMANDS = {
    "bisect": "runs a command the caller names (`bisect run`)",
    "difftool": "runs a command the caller names (`--extcmd`)",
    "mergetool": "runs a command the caller names",
    "filter-branch": "runs a command the caller names (`--tree-filter`)",
    "send-email": "runs a command the caller names (`--smtp-server`)",
    "instaweb": "starts a caller-named HTTP daemon",
    # Directly invocable, and it does run the configured command: with
    # `browser.evilb.cmd` set repository-locally, both
    # `git web--browse --browser=evilb <url>` and `git web--browse -b evilb <url>`
    # execute it. It is NOT here to cover `git help -w`, which reaches this code
    # path internally without the token ever appearing in argv — that route is
    # closed by the `help` entry and by `--help` in `_GIT_REFUSED_ARGUMENTS`.
    "web--browse": "runs a caller-named browser command",
    # `git help -m <page>` runs `man.<man.viewer>.cmd` through
    # `execl(SHELL_PATH, "-c", "<cmd> <page>")`, and `git help -w` does the same
    # through `web.browser` and `browser.<tool>.cmd`. Both keys carry an
    # arbitrary name, so neither can be pinned in `GIT_FORCED_CONFIG` — the same
    # shape as `filter.<name>.smudge`. Measured under this file's own pinned
    # environment against git 2.55: `git config man.viewer evil`, `git config
    # man.evil.cmd 'id #'`, `git help -m git` prints the credential container's
    # `uid=`. All three are repository-local `config` writes and a read verb, so
    # no lease is taken anywhere in the sequence.
    #
    # This entry is half the closure. The other half is `--help` in
    # `_GIT_REFUSED_ARGUMENTS`, because `git status --help` reaches the same
    # viewer with `status` in the subcommand slot — refusing this token alone
    # left that open, and the first cut of this change shipped exactly that gap.
    #
    # **The cost is a collision with ordinary text.** `help` is matched against
    # every token in the argv, so `git commit -m help` and `git checkout -b help`
    # are refused, with a message that says `git help` is refused. Only an
    # argument that is *exactly* the word survives the comparison — `git commit
    # -m "help me"` is one token and passes. Nothing shipped issues a git argv
    # containing a bare `help` (checked across `agents/`, `k8s-operator/` and
    # `scripts/`), and the refusal is loud and names the rule.
    #
    # Matching the subcommand *slot* instead would remove the collision and was
    # considered. It is not done, and the reason is measurable: git has
    # value-taking global options this file does not know about, so resolving
    # the slot is a guess about git's parser. `git --attr-source HEAD help -m
    # git` executes the payload, while `_git_plan` reports the subcommand as
    # `HEAD` — a position-aware check would allow it. Scanning every token
    # cannot disagree with git about where the subcommand is, and over-refusing
    # a commit message is the direction this is meant to fail in.
    "help": "runs a caller-named viewer command (`help -m`, `help -w`)",
    "p4": "bridges to a caller-named external tool",
    "svn": "bridges to a caller-named external tool",
    "fast-import": "runs caller-supplied stream commands",
    # `trailer.<name>.cmd` is run to produce a trailer's value, and the key's
    # arbitrary name puts it out of reach of the pins. `--trailer` below is the
    # trigger and refusing the flag is what closes the vector; this entry
    # refuses the subcommand whose whole job is that mechanism, so a future git
    # that grows a second trigger does not reopen it. Measured: without
    # `--trailer` the configured command does not run, even when the token is
    # already present in the input.
    "interpret-trailers": "applies trailer configuration that can name a command",
    # `git submodule foreach <cmd>` runs <cmd> in each initialised submodule.
    # Demonstrated through the executor at exit 0 with a submodule present.
    # `submodule` itself stays allowed — `submodule update` is a working-tree
    # write the product does — so the refused token is the inner verb. It is
    # matched wherever it appears, which also refuses a commit message that is
    # the bare word `foreach`; that is the same trade the rest of this file
    # makes.
    "foreach": "runs a command the caller names in each submodule",
}


# The long options above, for the abbreviation match in `_git_refused_name`.
_GIT_REFUSED_LONG = tuple(
    name for name in _GIT_REFUSED_ARGUMENTS if name.startswith("--")
)


def _git_refused_name(argument: str) -> str:
    """The refused option `argument` spells, or `argument` itself.

    Three spellings beyond the plain one have to collapse to the same name,
    because git accepts all of them, and a checker that recognises fewer
    spellings than the executor accepts is a parser differential — the one
    kind of bug this policy layer keeps producing.

    1. `--flag=value`, handled by splitting on the first `=`.
    2. `-Ovalue` and `-iOvalue`, the attached and clustered short forms,
       handled by `_GIT_REFUSED_SHORT` against every letter in the token.
    3. **`--fl`, an abbreviation.** git's *subcommand* options go through
       parse-options, which accepts any unambiguous prefix, so `git rebase
       --exe <cmd>` and `git config --glo alias.zz '!<cmd>'` both run. Both
       were demonstrated executing against a checker that matched the full
       spelling only, the second of them reinstating a vector this file had
       already closed. Note the asymmetry that makes this easy to miss: git's
       *own* options — `--git-dir`, `--exec-path`, `--config-env` — are parsed
       by hand in git.c with exact comparisons and are **not** abbreviable, so
       testing only those spellings suggests the problem does not exist.

    An argument is refused when it is a prefix of a refused option, which is
    strictly more conservative than git: git takes a prefix only when it is
    unambiguous among the options that subcommand defines, and this does not
    know the subcommand. Deliberately so — deciding ambiguity here would mean
    reimplementing parse-options and agreeing with it forever. The cost is
    refusing `--g`, `--ex` and the like as literal arguments, which nothing
    sends. Note the direction: `--oneline` is *not* refused, because it is not
    a prefix of anything on the list; only `--o` and `--op` would be.
    """
    if argument.startswith("-") and not argument.startswith("--"):
        refused = _GIT_REFUSED_SHORT.intersection(argument[1:])
        if refused:
            return f"-{sorted(refused)[0]}"
    name = argument.split("=", 1)[0]
    if name in _GIT_REFUSED_ARGUMENTS or not name.startswith("--"):
        return name
    if name == "--":
        # The end-of-options separator, not an abbreviation of anything. It is
        # a prefix of every long option, so without this it matches the first
        # entry on the list and refuses `git add -- clusters/prod`, which the
        # fleet-audit skill issues. Caught by the over-refusal test below it.
        return name
    return next(
        (full for full in _GIT_REFUSED_LONG if full.startswith(name)), name
    )


def git_argument_violation(argv: list[str]) -> str | None:
    """Why this git argv may not run, or None if it may.

    Matched across the whole argv rather than only the global-option region
    before the subcommand, which is the only place git honours these. That is
    deliberate: a check that has to agree with git about where the options end
    is a *guess* about git's parser, and every serious defect found in this
    policy layer so far was a checker and an executor parsing the same input
    differently. Scanning everything cannot disagree with git about scope.

    The cost is refusing a git command with a literal `-c` somewhere in its
    arguments — a commit message, a pathspec. Nothing shipped does that, and
    refusing something harmless is the direction this is meant to fail in.
    """
    if not argv or Path(argv[0]).name != "git":
        return None
    rest = argv[1:]
    scoped: dict[str, str] = {}
    for subcommand, (letters, why) in _GIT_REFUSED_SHORT_FOR_SUBCOMMAND.items():
        if subcommand in rest:
            scoped.update({f"-{letter}": why for letter in letters})
    for argument in rest:
        name = _git_refused_name(argument)
        if name not in _GIT_REFUSED_ARGUMENTS and scoped:
            # Same cluster rule as `_GIT_REFUSED_SHORT`, for the letters that
            # are only refused because of the subcommand in this argv.
            if argument.startswith("-") and not argument.startswith("--"):
                name = next(
                    (flag for flag in scoped if flag[1] in argument[1:]), name
                )
        reason = (
            _GIT_REFUSED_ARGUMENTS.get(name)
            or scoped.get(name)
            or _GIT_REFUSED_SUBCOMMANDS.get(argument)
        )
        if reason is not None:
            return (
                f"`git {name}` is refused: it {reason}. The proxy runs git with "
                "its transport allowlist, configuration files and hooks "
                "directory pinned, because git takes both its transport and its "
                "helper programs from configuration that lives on the volume "
                "the agent writes — `-c protocol.ext.allow=always` re-enables "
                "the `ext::` transport's arbitrary command execution, and `-c "
                "core.hooksPath=` re-enables hooks. No skill needs any of these: "
                "use `-C` to choose a directory inside a leased workspace, and "
                "ask an operator for anything that has to change the proxy's own "
                "configuration."
            )
    return None


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


def _within(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def content_workspace_enabled() -> bool:
    """Is broker-owned, content-passed git armed?

    Off by default, and it stays off until the skills are migrated in a reviewed
    change. Both halves run side by side in the meantime: `/v1/exec` keeps
    accepting a directory from the agent exactly as it does today, so turning
    this on adds a door rather than moving one. That is deliberate — the
    mechanism lands, the migration is a separate diff, and neither has to be
    reverted to fix the other.
    """
    return os.getenv("CREDENTIAL_PROXY_CONTENT_WORKSPACE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


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
        self.git_hooks_dir = self.state_dir / GIT_HOOKS_DISABLED_DIR
        # Where broker-owned git trees live when content-passing is armed.
        # Under the state dir, never under `workspace_dir`: the state dir is the
        # broker's own emptyDir and the workspace is the volume the agent
        # writes. `ContentWorkspaceStore` re-proves that separation at
        # construction and refuses to start if a future mount layout collapses
        # it — see `content_workspace.assert_disjoint_roots`. None when the
        # feature is off, which is what makes `execute_workspace_git`
        # unreachable rather than merely unused.
        # Resolved, like `workspace_dir` and unlike the other state paths: it is
        # compared against a resolved `cwd` in `_execute`, and on a filesystem
        # with a symlinked prefix an unresolved root never matches — the
        # containment check would refuse every legitimate call and the feature
        # would look broken rather than closed.
        self.content_workspace_root = (
            (self.state_dir / CONTENT_WORKSPACE_DIR).resolve()
            if content_workspace_enabled()
            else None
        )
        # git reads its global config from $HOME/.gitconfig, and $HOME is the
        # sidecar-only state dir, so the agent cannot open the file directly.
        # It can still *write* it through the proxy unless `git config
        # --global` is refused, which is why that flag is on the refusal list —
        # the mount geometry is not on its own a reason to trust this file.
        # Naming the path explicitly means the location stays fixed if the
        # mounts are ever rearranged — the same argument the KUBECTL_KUBERC
        # line below makes.
        # It is deliberately not /dev/null: `gh auth setup-git` writes the
        # GitHub credential helper into *this* file via `git config --global`,
        # so pointing it at /dev/null does not harden anything, it just severs
        # authenticated push and fetch.
        self.git_config_global = self.home_dir / ".gitconfig"
        for path in (
            self.home_dir,
            self.workspace_dir,
            self.tmp_dir,
            self.config_dir,
            self.cache_dir,
            self.local_state_dir,
            self.kube_dir,
            self.kubeconfig_dir,
            self.git_hooks_dir,
            *(
                (self.content_workspace_root,)
                if self.content_workspace_root is not None
                else ()
            ),
        ):
            path.mkdir(parents=True, exist_ok=True)
        # Re-applied on every start rather than only at creation: the state dir
        # is an emptyDir, but the mode is the whole control, so it is cheaper to
        # assert it than to reason about who else may have touched it.
        try:
            self.git_hooks_dir.chmod(0o500)
        except OSError:
            LOGGER.warning("could not restrict %s", self.git_hooks_dir)
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
            # kuberc carries per-command default options, including `as`, and it
            # is on by default in kubectl v1.36.3. command_policy refuses the
            # `--kuberc` flag, but kubectl also reads `$HOME/.kube/kuberc` with
            # no flag at all -- verified to set Impersonate-User on an argv that
            # contains nothing to refuse. That path is out of the agent's reach
            # only because HOME points at the sidecar-only state dir rather than
            # the shared PVC, which is deployment geometry and not a control.
            # This turns the feature off outright so the property survives
            # someone rearranging the mounts. Nothing here needs kuberc.
            "KUBECTL_KUBERC": "false",
            # git is the one allowed executable that takes both its transport
            # and its hook programs from configuration, and two of the three
            # config layers it reads are files the agent can write. Verified
            # against git 2.55: `git -c protocol.ext.allow=always clone
            # "ext::<cmd>"` executes <cmd> here, in the container holding the
            # cloud credentials, and a `.git/hooks/pre-commit` in a leased
            # workspace does the same on the next `git commit` with no unusual
            # argv at all.
            #
            # GIT_ALLOW_PROTOCOL is the interesting one. It is not a default:
            # when it is set, it outranks `protocol.<name>.allow` from every
            # config layer *including* `-c` on the command line, which is what
            # makes the environment the boundary here and leaves argv
            # inspection as the backup check rather than the control.
            #
            # It is a colon-separated list, and the empty string is not
            # "allow all" — it is a list containing one empty protocol name,
            # so it allows nothing and breaks every clone. The value must stay
            # non-empty. `https` alone is correct today because every URL the
            # skills clone, fetch or push is https (gitops_workspace builds
            # them from a fixed https prefix).
            #
            # It also refuses the `file` protocol, and that is load-bearing
            # rather than incidental: `--upload-pack=<cmd>` and
            # `--receive-pack=<cmd>` name a program git runs for a local-path
            # remote, and the paired control says this variable is the only
            # thing stopping them — widen it to `https:file` for a local-path
            # clone and both become arbitrary code execution again. They are on
            # the argv refusal list below so that widening is survivable, but
            # anyone reaching for `https:file` should read that list first.
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.git_config_global),
            # An editor is a command git runs, and `core.editor` is settable
            # from the `.git/config` the agent can write. `git commit` with no
            # `-m` and `git tag -a` with no `-m` both launch it — argv the
            # skills nearly send already. These two variables outrank
            # `core.editor`/`sequence.editor` from every config layer including
            # `-c`, verified the same way GIT_ALLOW_PROTOCOL was, so this is a
            # boundary rather than a pin. `false` rather than empty: git treats
            # an unset editor as "fall back to vi", and an editor that exits
            # non-zero is how a non-interactive container should fail. Nothing
            # is lost — there is no terminal here, so a commit that needs an
            # editor could never have succeeded.
            "GIT_EDITOR": "false",
            "GIT_SEQUENCE_EDITOR": "false",
            **_git_forced_config_environment(
                (("core.hooksPath", str(self.git_hooks_dir)), *GIT_FORCED_CONFIG)
            ),
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

    def execute_workspace_git(self, argv: list[str], cwd: Path) -> ExecutionResult:
        """git the broker issues on its own behalf, in a tree the agent cannot name.

        A separate door from `/v1/exec`, and separate on purpose. The point of
        content-passing is that the agent no longer spells `git` at all; if the
        broker's own plumbing went through the agent-facing path, every
        subcommand that plumbing needs would have to be permitted to the agent
        too, and the agent-facing git allowlist would land at eighteen entries
        instead of none. Keeping the two apart is what makes the agent-facing
        answer "git is not reachable" rather than "git is reachable, narrowly".

        Three things are enforced here rather than assumed:

        * the subcommand is one of the eleven this product issues, checked
          against the argv as parsed rather than as composed, so a later edit
          that threads a caller's string into one of these vectors is refused
          instead of run;
        * `-C` is refused outright — it is a working-directory redirect, and the
          containment below is the only reason this path is safe;
        * the working directory is inside the *content workspace* root, which
          `assert_disjoint_roots` has already proven is not inside the volume
          the agent writes to.
        """
        from content_workspace import WORKSPACE_GIT_SUBCOMMANDS

        if self.content_workspace_root is None:
            raise RuntimeError("content workspace support is not enabled")
        if not argv or argv[0] != "git":
            raise ValueError("only git runs on the workspace path")
        executable_path = self.executables.get("git")
        if not executable_path:
            raise RuntimeError("supported executable is unavailable: git")
        subcommand, redirects = _git_plan(argv)
        if redirects:
            raise ValueError("`-C` is not accepted on the workspace path")
        if subcommand not in WORKSPACE_GIT_SUBCOMMANDS:
            raise ValueError(
                f"`git {subcommand}` is not one of the subcommands the broker "
                "issues on its own behalf"
            )
        return self._execute(
            [executable_path, *argv[1:]],
            cwd=str(cwd),
            containment_root=self.content_workspace_root,
        )

    def _within_workspace(self, candidate: Path) -> bool:
        return _within(self.workspace_dir, candidate)

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

    def _dns_endpoint_args(self, gcloud: str, target: ClusterTarget) -> list[str]:
        """Decide whether this cluster's credentials must name its DNS endpoint.

        The decision itself lives in `gke_endpoint`, shared with the two callers in
        the agent container. What is local to the sidecar is *how* gcloud runs: the
        binary is the resolved executable rather than whatever is on PATH, and it
        goes through `_execute` so the describe is subject to the same timeout,
        output cap, and working directory as every other command here.

        Imported lazily, as pyyaml is above. This module is otherwise stdlib-only
        and has to stay importable on its own; a sibling that failed to load would
        take the whole credential proxy down, where losing the flag only costs the
        behaviour that shipped before it existed.
        """
        try:
            from gke_endpoint import dns_endpoint_args
        except ImportError as error:
            logging.warning(
                "gke_endpoint is unavailable (%s); falling back to the IP endpoint for %s",
                error,
                target.context_name,
            )
            return []

        def run(argv: list[str]) -> tuple[int, str]:
            result = self._execute([gcloud, *argv[1:]])
            return result.exit_code, result.stdout

        return dns_endpoint_args(target.project, target.cluster, target.location, run=run)

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
                        *self._dns_endpoint_args(gcloud, target),
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
        containment_root: Path | None = None,
    ) -> ExecutionResult:
        """Run a command. `kubeconfig_path` is already resolved and trusted.

        Callers hand this an absolute path the proxy itself owns; containment and
        regeneration happen in `execute` so that nothing reaching this point is
        still caller-controlled.

        `containment_root` names which root the working directory must be inside
        of. It defaults to the agent-shared workspace, which is every existing
        caller. `execute_workspace_git` passes the broker-owned content
        workspace root instead — the two roots are proven disjoint at startup,
        so widening the check here cannot widen the other path.
        """
        started = time.monotonic()
        timed_out = False
        root = containment_root or self.workspace_dir
        command_cwd = root
        if cwd:
            requested_cwd = Path(cwd).resolve()
            if not _within(root, requested_cwd):
                # Name the root that was actually checked. With one message for
                # both, a refusal on the workspace path reads as though the
                # agent-shared containment fired, which sends whoever is
                # debugging it to the wrong control.
                raise ValueError(
                    "working directory is outside the shared workspace"
                    if root == self.workspace_dir
                    else "working directory is outside the content workspace"
                )
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


def build_workspace_store(executor: CommandExecutor):
    """The content-passing store, or None when the feature is off.

    Returning None rather than an inert object is deliberate: the handler tests
    `workspaces is None` to decide whether the routes exist at all, so "off"
    means the endpoints are absent, not present-and-refusing. An absent endpoint
    cannot be reached by a bug in a refusal.

    A failure to construct — which today means only `assert_disjoint_roots`
    refusing overlapping roots — is fatal rather than a downgrade to off. An
    operator who asked for content-passing and silently got the directory path
    back would believe they had a property they do not have.
    """
    if executor.content_workspace_root is None:
        return None
    from content_workspace import ContentWorkspaceStore

    store = ContentWorkspaceStore(
        executor.content_workspace_root,
        executor.workspace_dir,
        executor.execute_workspace_git,
    )
    LOGGER.info("content workspace enabled root=%s", executor.content_workspace_root)
    return store


def read_only_enforced() -> bool:
    """Is the read-only gate armed?

    Defaults to on, and anything that is not exactly "false" leaves it on. A
    typo in a ConfigMap should not quietly hand an agent write access.

    This switch is deliberately not documented in the customer-facing reference.
    It is global, unscoped and has no expiry: setting it disables the read-only
    posture for every command, every agent and every cluster in the Pod, and
    today there is no impersonation layer underneath to catch what gets through
    (see command_policy's module docstring).

    **On an operator-managed install there is no supported way to set it, by
    design.** The operator reserves the name: a `spec.deployment.env` entry is
    rejected by the validating webhook and dropped by mergeCredentialProxyEnv,
    no ConfigMap carries it, and a hand edit to the generated Deployment is
    reverted on the next reconcile. Whoever can edit the PlatformAgent is
    frequently who the policy is meant to constrain, so the switch is not
    theirs. An earlier version of this docstring offered it as the way to
    "recover from a bad allowlist without waiting on an image build"; that
    route did not exist -- the ConfigMap it named has only ever carried
    policy.json -- and following it during an outage costs an operator a CR
    patch that changes nothing and explains nothing.

    The remedy for a command the allowlist should have permitted is to add it
    to command_policy.KUBECTL_READ_VERBS or GCLOUD_READ_COMMANDS and ship the
    image, which is what the customer-facing reference already tells the
    reader to do (docs/site/.../reference/credential-isolation.md).

    What remains is the process environment, which is how the tests arm and
    disarm the gate and how the proxy behaves when run outside the operator --
    a standalone or local invocation, where the person setting it is the
    person running the process.
    """
    return os.getenv("CREDENTIAL_PROXY_ENFORCE_READ_ONLY", "true").strip().lower() != "false"


def _sanitize_for_logging(s: str, max_length: int = 64) -> str:
    """Strip control characters to prevent log forgery, with a length cap.

    Removes C0/C1 control characters, line/paragraph separators (Unicode), and
    all characters that could be interpreted as line boundaries by consumers
    (Python splitlines, JS /m, JSON parsers, etc). Also caps length to prevent
    unbounded agent-controlled hint expansion.

    ``max_length`` is raised only for a value the agent does not control. A
    ServiceAccount username is
    ``system:serviceaccount:<namespace>:<name>``, which reaches 65 characters
    at ordinary lengths and truncated at 64 exactly where the discriminating
    part of the name is -- observed on the dev install, where the principal
    logged as ``...:kubeagents-platform-agen``. Namespace and name are each
    bounded at 253 by the API server, so the value cannot grow without bound
    either way.
    """
    import unicodedata

    # Cc (control), Cf (format), Zl (line sep) and Zp (para sep) forge log
    # lines in text-mode consumers.
    #
    # Cs is here for the opposite reason: a lone surrogate does not forge a
    # record, it deletes one. json.loads turns "\\ud800" into a real lone
    # surrogate, which no UTF-8 encoder will accept, so the handler raises
    # UnicodeEncodeError, logging prints "--- Logging error ---" to stderr and
    # drops the record - while the request it was supposed to describe carries
    # on and succeeds. An authenticated caller could execute a command and
    # leave no exec line behind. Verified against a byte-encoding handler; a
    # StringIO one does not reproduce it, which is why the unit tests below
    # write through a real UTF-8 encoder.
    filtered = ''.join(
        c for c in s if unicodedata.category(c) not in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp')
    )
    return filtered[:max_length]


def _sanitized_log_args(args: tuple[Any, ...], max_length: int = 512) -> tuple[Any, ...]:
    """Sanitize the string arguments of a log record, leaving the rest alone.

    For the BaseHTTPRequestHandler log hooks, where the format string is the
    stdlib's and every argument is caller-controlled. Non-strings (status
    codes, sizes) are passed through so the format specifiers still match.
    """
    return tuple(
        _sanitize_for_logging(arg, max_length=max_length) if isinstance(arg, str) else arg
        for arg in args
    )


def read_only_refusal(argv: list[str]) -> tuple[dict[str, str], str | None] | None:
    """The blocked-response body for `argv`, or None if it may run.

    Returns (response_dict, log_hint) for logging, or None if allowed.
    log_hint is either verb_tuple or offending_flag, safe to log.
    Split out from the handler so the decision is testable without standing up
    a socket, and so the gate reads the class attribute rather than the
    environment on every request.
    """
    if not CredentialProxyHandler.enforce_read_only:
        return None
    decision = command_policy.evaluate(argv)
    if decision.allowed:
        return None

    # Choose what to log: resolved verb/command path, or the offending flag
    log_hint = None
    if decision.verb_tuple:
        log_hint = ".".join(decision.verb_tuple)
    elif decision.offending_flag:
        log_hint = decision.offending_flag

    return (
        {
            "status": "blocked",
            "code": "SECURITY_POLICY_BLOCKED",
            "rule": decision.rule_id,
            "message": decision.message,
        },
        log_hint,
    )


class CredentialProxyHandler(BaseHTTPRequestHandler):
    policy: Policy
    executor: CommandExecutor
    max_request_bytes: int
    slack_max_request_bytes: int
    enforce_read_only: bool = True
    chat_relay: GoogleChatRelay | None = None
    slack_relay: SlackRelay | None = None
    # None unless CREDENTIAL_PROXY_CONTENT_WORKSPACE is on. While it is None the
    # /v1/workspace/* routes answer 404 — the same answer an older broker gives,
    # which is what lets a migrating client detect support by asking rather than
    # by version-sniffing.
    workspaces: object | None = None
    # Replaced by serve(). The default keeps the sidecar deployment, where the
    # Unix socket is the access control, behaving as it did before there was an
    # authenticator at all.
    authenticator: NullAuthenticator | ServiceAccountAuthenticator = NullAuthenticator()
    # Set per request once the caller is identified; read by the policy layer.
    principal: Principal | None = None

    def _authenticated(self) -> Principal | None:
        """Identify the caller, or answer 401 and return None.

        Everything but /healthz goes through here. /healthz is the readiness
        probe and reveals nothing, and the probe runs before any token would be
        available; every other route on this listener either runs a
        credentialed command or relays through a credentialed client.

        Binding ``self.principal`` is this method's job rather than each
        route's. The chat relays and the GitHub refresh spend the broker's
        credentials just as ``/v1/exec`` does, so a seam that were populated on
        only one of them would be a seam the next change has to fix before it
        can use it: whoever adds a per-caller check would find the value
        present on the route they tested and None on the two they did not.
        """
        try:
            self.principal = self.authenticator.authenticate(self.headers)
            return self.principal
        except AuthenticationError as exc:
            LOGGER.warning(
                "rejected an unauthenticated request path=%s reason=%s",
                _sanitize_for_logging(self.path),
                exc,
            )
            self._json(
                HTTPStatus.UNAUTHORIZED, {"error": "caller could not be authenticated"}
            )
            return None

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz" and self._authenticated() is None:
            return
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
        principal = self._authenticated()
        if principal is None:
            return
        if self.path.startswith("/v1/chat/slack/"):
            self._handle_slack_post()
            return
        if self.path.startswith("/v1/chat/"):
            self._handle_chat_post()
            return
        if self.path == "/v1/github/refresh":
            self._handle_github_refresh()
            return
        if self.path.startswith("/v1/workspace/"):
            self._handle_workspace_post()
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

        # Sanitized here rather than at each of the eight log sites below, and
        # sanitized at all because it is caller-supplied text going into a
        # line-oriented formatter. A newline in it ends the record and starts a
        # new one, so an unsanitized requestId lets the caller write a whole
        # forged entry into the audit trail - including one naming a
        # ServiceAccount that made no request. It is never echoed back to the
        # client, so narrowing it costs nothing.
        #
        # This is one route into the log, not all of them. The access line goes
        # through log_message above, which had the same defect from an
        # unauthenticated caller; both are fixed, and any new log site taking
        # caller text needs the same treatment.
        request_id = _sanitize_for_logging(str(payload.get("requestId", "")))
        # The principal reaches the decision point, rather than being checked at
        # the door and thrown away. Every policy refusal below is a judgement
        # about *what* was asked. A per-caller model is what would let them
        # become judgements about who asked, and self.principal — bound for
        # this route and for every other authenticated one by _authenticated —
        # is the value they would read. Today it is what the audit trail
        # records and nothing else.
        LOGGER.info(
            "exec request_id=%s principal=%s executable=%s",
            request_id,
            # 512 rather than the default 64: this value comes from the
            # TokenReview, not from the request, and a truncated identity is
            # an audit line that names the wrong ServiceAccount.
            _sanitize_for_logging(principal.describe(), max_length=512),
            # Logged before the allowlist check below, so at this point it is
            # arbitrary caller text and gets the same treatment as request_id.
            _sanitize_for_logging(argv[0]),
        )
        if argv[0] not in CommandExecutor.ALLOWED_EXECUTABLES:
            LOGGER.warning(
                "executable blocked request_id=%s executable=%s",
                request_id,
                _sanitize_for_logging(argv[0]),
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

        # Backup check only. The boundary for the `ext::` transport is
        # GIT_ALLOW_PROTOCOL in the executor's environment, which git honours
        # over anything argv can say; this refuses the flags that would
        # otherwise re-enable git's hook execution, and it refuses them before
        # the lease check because it does not depend on the working directory.
        violation = git_argument_violation(argv)
        if violation is not None:
            LOGGER.warning("git argument refused request_id=%s", request_id)
            self._json(
                HTTPStatus.FORBIDDEN,
                {
                    "status": "blocked",
                    "code": "SECURITY_POLICY_BLOCKED",
                    "rule": "git.argument.refused",
                    "message": violation,
                },
            )
            return

        # Not a policy rule: the policy matches on argv alone, and this refusal
        # turns on the working directory as well.
        violation = self.executor.git_lease_violation(argv, cwd)
        if violation is not None:
            LOGGER.warning(
                "git lease refused request_id=%s cwd=%s",
                request_id,
                _sanitize_for_logging(cwd or "", max_length=256),
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

        # Runs after the credential denylist above, so rules like
        # `kubernetes.token-disclosure` keep their own ids and messages rather
        # than being reported as read-only refusals. For example, `kubectl create
        # token sa` is on the denylist as `kubernetes.token-disclosure` and will
        # be refused by the denylist with that rule id. If the gate ran first, it
        # would refuse as `kubernetes.read-only`, losing the specific rule.
        refusal_result = read_only_refusal(argv)
        if refusal_result is not None:
            refusal, log_hint = refusal_result
            safe_hint = _sanitize_for_logging(log_hint) if log_hint else "unknown"
            LOGGER.warning(
                "command refused request_id=%s rule=%s hint=%s", request_id, refusal["rule"], safe_hint
            )
            self._json(HTTPStatus.FORBIDDEN, refusal)
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
                # The message embeds the caller's own cwd or kubeconfig path.
                "command rejected request_id=%s reason=%s",
                request_id,
                _sanitize_for_logging(str(exc), max_length=256),
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

    def _handle_workspace_post(self) -> None:
        """The content-passing routes: bytes in, bytes out, never a path.

        Every response here is content or a name. Nothing returns a filesystem
        path, because a path handed back is a directory the agent can be told to
        `cd` into — which is precisely the arrangement this replaces. The
        `handle` is a broker-minted opaque token, not a location. That holds for
        the error responses too: `ContentWorkspaceStore._redact` takes every
        absolute path, plus the handle, back out of git's stderr before it goes
        on the wire. That is the only reason the sentence above is a property
        rather than an intention, and it scrubs by shape rather than by a list
        of known paths -- the leak nobody predicted is the failure mode here.

        These routes deliberately do **not** go through `Policy.blocked_by`,
        `git_argument_violation` or `git_lease_violation`. Those three inspect an
        argv the caller composed; here the caller composes no argv at all. The
        equivalent controls are structural: `content_workspace.repo_relative`
        decides what a path may name, `CommandExecutor.execute_workspace_git`
        decides which git may run, and neither reads a caller string into a
        command position.
        """
        import content_workspace

        if self.workspaces is None:
            self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return
        route = self.path[len("/v1/workspace/") :]
        try:
            payload = self._read_json_body(
                max_bytes=max(
                    self.max_request_bytes,
                    content_workspace.max_total_bytes() * 2,
                )
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        try:
            body = self._workspace_route(route, payload)
        except content_workspace.ContentWorkspaceError as exc:
            LOGGER.warning(
                "workspace request refused route=%s code=%s", route, exc.code
            )
            self._json(
                HTTPStatus(exc.status),
                {"status": "blocked", "code": exc.code, "message": str(exc)},
            )
            return
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:
            LOGGER.exception("workspace request failed route=%s type=%s", route, type(exc).__name__)
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "credential proxy workspace operation failed"},
            )
            return
        if body is None:
            self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return
        self._json(HTTPStatus.OK, body)

    def _workspace_route(self, route: str, payload: dict) -> dict | None:
        import content_workspace

        store = self.workspaces
        if route == "open":
            workspace = store.open(payload.get("repo"), payload.get("base") or None)
            return {
                "handle": workspace.handle,
                "repo": workspace.repo,
                "base": workspace.base,
                "baseSha": workspace.base_sha,
            }
        if route == "read":
            content = store.read(payload.get("handle"), payload.get("path"))
            return {
                "path": payload.get("path"),
                "contentBase64": base64.b64encode(content).decode("ascii"),
                "size": len(content),
            }
        if route == "list":
            return {
                "entries": store.list(payload.get("handle"), payload.get("prefix") or None)
            }
        if route == "commit":
            changes = content_workspace.parse_changes(payload.get("changes"))
            return store.commit(
                payload.get("handle"),
                payload.get("branch"),
                payload.get("message"),
                changes,
                expected_base_sha=payload.get("expectedBaseSha") or None,
            )
        if route == "push":
            return store.push(payload.get("handle"), payload.get("branch"))
        if route == "close":
            store.close(payload.get("handle"))
            return {"closed": True}
        return None

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
            # The helper's stderr is the only place the broker's actual refusal
            # exists: github_token_refresh raises `Minty returned error (HTTP
            # <code>): <body>` and its main() logs that line. Without this the
            # caller's reason code -- GITHUB_TOKEN_REFRESH_FAILED, which the
            # resolver renders into a chat room -- is the whole diagnosis, and
            # an operator has nothing to read during a broker outage.
            #
            # Same split as the shell bootstrap above: the detail must not
            # travel in the response, which crosses back into the agent sandbox,
            # so it is logged here where only an operator reading the sidecar's
            # own logs sees it and the reply stays output-free. Bounded because
            # `_execute` caps output at CREDENTIAL_PROXY_MAX_OUTPUT_BYTES (4 MiB
            # by default), which is not a log line, and this path can fire on
            # every cron tick. Redacted before it is bounded, so that a token cut
            # in half by the slice is not what survives.
            detail = redact_credentials(result.stderr.strip())
            LOGGER.warning(
                "GitHub credential refresh exited %d%s",
                result.exit_code,
                f": {detail[:1000]}" if detail else "",
            )
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
            # Carry the status line of a Google Chat rejection back to the
            # agent and into this log. Without it a transport fault, a 404 for
            # an unknown space and a 403 for a missing scope are one
            # indistinguishable "operation failed", and the retries inside
            # api_call have already absorbed everything genuinely transient —
            # so what reaches here is usually worth naming.
            fields = _chat_error_fields(exc)
            LOGGER.warning(
                "chat relay operation failed path=%s type=%s status=%s",
                self.path,
                type(exc).__name__,
                (fields or {}).get("status", "none"),
            )
            body: dict[str, Any] = {"error": "Google Chat operation failed"}
            if fields:
                body["chat"] = fields
            self._json(HTTPStatus.BAD_GATEWAY, body)

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
        # BaseHTTPRequestHandler.log_request passes self.requestline through
        # here verbatim, and this runs on every response - including the 401 an
        # unauthenticated caller gets. A vertical tab in the request line is
        # enough to end the record and start another, so an unauthenticated
        # caller could write a whole audit-shaped line of its own. The request
        # line's own tokenizer stops at whitespace, which limits the shape of
        # the forgery and does not prevent it.
        LOGGER.info("http " + message, *_sanitized_log_args(args))

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_agent_api_proxy() -> ThreadingHTTPServer:
    """Bind the authenticated front door for the agent's own API.

    This runs wherever the agent's API server is reachable on loopback. In the
    sidecar deployment that is this same container; when the broker is split
    into its own Pod it is a container in the *agent's* Pod, because 8642 binds
    127.0.0.1 and is guarded by a fixed non-secret sentinel key. Moving this
    across a network boundary would mean exposing that port and that sentinel
    to the cluster network, so it does not move.
    """
    AgentAPIProxyHandler.external_key = os.getenv("API_SERVER_EXTERNAL_KEY", "").strip()
    if not AgentAPIProxyHandler.external_key:
        raise RuntimeError("API_SERVER_EXTERNAL_KEY must be configured")
    AgentAPIProxyHandler.upstream_key = os.getenv(
        "AGENT_API_UPSTREAM_KEY", "cluster-internal-trusted"
    )
    port = int(os.getenv("AGENT_API_PROXY_PORT", "8643"))
    server = ThreadingHTTPServer(("0.0.0.0", port), AgentAPIProxyHandler)
    LOGGER.info("authenticated PlatformAgent API proxy listening on port %d", port)
    return server


def reachable_off_pod(args: argparse.Namespace) -> bool:
    """Can something outside this Pod open a connection to the broker?

    Two ways in. The Python server can bind a TCP port itself, which is the
    branch `--unix-socket` normally avoids. Or Envoy, which fronts the Unix
    socket, can be told to listen on the Pod IP rather than loopback — and
    then the Unix socket's 0600 mode protects nothing, because the connection
    arrives through Envoy as Envoy's own user.
    """
    if not args.unix_socket:
        return True
    envoy_address = os.getenv("CREDENTIAL_PROXY_ENVOY_ADDRESS", "").strip()
    return bool(envoy_address) and envoy_address not in {"127.0.0.1", "::1", "localhost"}


def resolve_role() -> str:
    """Which halves of this process to run.

    ``combined`` is the sidecar deployment and the default: one container is
    both the credential broker and the agent-API front door, because both ends
    are on the same loopback. Splitting the broker into its own Pod splits
    those two roles across two containers in two Pods.
    """
    role = os.getenv("CREDENTIAL_PROXY_ROLE", "combined").strip().lower() or "combined"
    if role not in {"combined", "broker", "api-proxy"}:
        raise RuntimeError(
            f"unsupported CREDENTIAL_PROXY_ROLE {role!r}; "
            "expected 'combined', 'broker' or 'api-proxy'"
        )
    return role


def serve(args: argparse.Namespace) -> None:
    role = resolve_role()
    if role == "api-proxy":
        start_agent_api_proxy().serve_forever()
        return

    # Decided before anything credentialed starts, so a misconfigured
    # deployment fails at boot rather than on the first request.
    CredentialProxyHandler.authenticator = build_authenticator()
    if reachable_off_pod(args) and not CredentialProxyHandler.authenticator.authenticates:
        # A listener the cluster can reach, with no authentication, hands the
        # credentials to whoever reaches the port. The sidecar deployment gets
        # away without an authenticator because loopback plus a 0600 socket is
        # the control; a reachable listener has no such fallback.
        raise RuntimeError(
            "refusing to serve the credential broker on a listener reachable from "
            "outside this Pod with CREDENTIAL_PROXY_AUTH_MODE=none; set "
            "CREDENTIAL_PROXY_AUTH_MODE=serviceaccount, or keep Envoy on loopback "
            "and the runtime on a Unix socket"
        )
    LOGGER.info(
        "caller authentication mode=%s",
        "serviceaccount" if CredentialProxyHandler.authenticator.authenticates else "none",
    )

    CredentialProxyHandler.policy = Policy.load(args.policy)
    executor = CommandExecutor(
        timeout_seconds=args.timeout_seconds,
        max_output_bytes=args.max_output_bytes,
        state_dir=args.state_dir,
    )
    executor.bootstrap(os.getenv("CREDENTIAL_PROXY_BOOTSTRAP_COMMAND", ""))
    CredentialProxyHandler.executor = executor
    CredentialProxyHandler.workspaces = build_workspace_store(executor)
    CredentialProxyHandler.max_request_bytes = args.max_request_bytes
    CredentialProxyHandler.enforce_read_only = read_only_enforced()
    LOGGER.info("read-only enforcement enabled=%s", CredentialProxyHandler.enforce_read_only)
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
    if role == "combined":
        api_server = start_agent_api_proxy()
        threading.Thread(target=api_server.serve_forever, daemon=True).start()
    if args.unix_socket:
        socket_path = Path(args.unix_socket)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        # Nothing behind this socket authenticates its callers: reaching it is
        # reaching the credentials, past Envoy and past the whole command policy.
        # The mount keeps it in this container, and the mode is the second lock —
        # 0600, so it stays connectable only by this container's own user however
        # wide the sidecar's umask is set for the shared workspace. Applied as a
        # umask rather than a chmod after the fact so there is no window in which
        # the bound socket is more permissive than this.
        previous_umask = os.umask(0o177)
        try:
            server = ThreadingUnixHTTPServer(str(socket_path), CredentialProxyHandler)
        finally:
            os.umask(previous_umask)
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
